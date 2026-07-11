# VIX1D Same-Day Volatility Module — Implementation Spec

Target: existing 0DTE SPX engine (Databento OPRA chain access, GEX regime
layer, pre-market filters, entry/exit ruleset, journaling). Goal: add a
same-day expected-volatility signal (`Vol1DState`) derived from VIX1D that
(a) sizes expected daily range, (b) classifies a fade-vs-trend regime, and
(c) cross-checks the GEX read. This is a signal/filter module, not an order
router. It should never take a trade on its own — it gates and sizes.

## 0. Design decisions to confirm before coding

1. **Data source for VIX1D.** Databento does not publish normalized index
   values (VIX/VIX1D spot). The official value comes from the Cboe Global
   Indices Feed (CGIF), which on Databento is raw PCAP only (~$750/mo —
   overkill here). Recommendation: compute a VIX1D proxy in-house from the
   OPRA SPXW options already in the pipeline. Optionally reconcile against
   an official delayed print (Cboe site / cheaper REST vendor) once a day
   for drift QA.
2. **Proxy vs official.** The proxy will not match the official print
   tick-for-tick. That's fine — we care about regime and relative change,
   not the exact number. Calibrate the proxy to the official close daily
   and store the residual.
3. **Term structure.** VIX1D alone is a level. The regime edge is sharper
   with a short-end curve (VIX1D vs VIX9D vs VIX). Those have the same
   sourcing problem; either compute proxies or skip term structure in v1
   and add in v2.

## 1. VIX1D construction (proxy)

Replicate the Cboe generalized variance-swap formula on the two nearest
SPXW strips.

- Universe: PM-settled SPXW options.
- Near-term (T1): options expiring today (0DTE).
- Next-term (T2): the next SPXW expiry after today (typically 1DTE).

Time is measured in **business days / business years, not calendar time**
(this is the key VIX1D-specific deviation from standard VIX). Define
`T = business_time_to_expiry / (252 business-day year)` per the Cboe VIX1D
methodology.

Per-term variance (standard VIX math, run for each term k ∈ {1,2}):

```
sigma_k^2 = (2 / T_k) * Σ_i [ ΔK_i / K_i^2 * e^(R_k * T_k) * Q(K_i) ]
          - (1 / T_k) * ( F_k / K0_k - 1 )^2
```

where:

- `F_k` = forward index level for term k
  = `K_atm + e^(R_k T_k) * (C_atm − P_atm)` (strike where call/put mid diff
  is smallest).
- `K0_k` = first strike below `F_k`.
- `Q(K_i)` = midpoint price of the OTM option at strike `K_i` (puts below
  K0, calls above, average of both at K0).
- Strike inclusion: walk OTM outward from K0 until you hit **two
  consecutive strikes with no bid**, then stop (drop zero-bid strikes).
- `ΔK_i` = half the distance between neighboring strikes (asymmetric
  handling at the edges).
- `R_k` = risk-free rate to expiry (CMT / Treasury curve; a flat short rate
  is acceptable for v1 given the 1-day horizon — sensitivity is tiny).

Time-weight to a constant 1-day horizon (handles the 0DTE strip decaying to
zero through the session):

```
VIX1D = 100 * sqrt(
    ( w1 * T1 * sigma_1^2 + w2 * T2 * sigma_2^2 ) * (N_365 / N_1day)
)
```

Use the Cboe near/next weighting so that as T1 → 0 through the day, weight
rolls from the 0DTE strip toward the 1DTE strip. Match the official rolling
convention (near-term weight → 0 at its expiry). Keep the exact weights in
config so they can be tuned against the official print.

**Implementation note:** don't hand-roll the numerical edge cases from
scratch first. Stand up a naive version, then calibrate ΔK handling,
forward calc, and time convention until the proxy tracks the official VIX1D
close within a tolerance you log daily.

## 2. Derived features

From the raw proxy, compute and expose:

| Feature | Definition | Use |
|---|---|---|
| `vix1d` | proxy level (annualized vol, %) | core level |
| `exp_move_pts` | `SPX_spot * (vix1d/100) / sqrt(252)` | 1-day expected 1σ move in SPX points |
| `exp_move_adj` | `exp_move_pts * rp_factor` (rp_factor < 1) | risk-premium-adjusted move; VIX1D overstates realized |
| `vix1d_tod_z` | level minus time-of-day baseline, in SD | detrended level (see below) |
| `rv_intraday` | rolling realized vol of SPX (e.g. Parkinson / Garman-Klass on 1–5m bars, annualized) | IV-vs-RV read |
| `iv_rv_spread` | `vix1d − rv_intraday` | fade-vs-trend tilt |
| `vix1d_roc` | rate of change over N minutes | spike detection |
| `term_ratio` (v2) | `vix1d / vix9d_proxy` | contango (<1) vs backwardation (>1) |

**Time-of-day detrend is mandatory.** VIX1D has a systematic intraday drift
(it mechanically rises as the 0DTE strip's time value bleeds and gamma
dominates), so a "high" reading at 15:30 is not comparable to the same
number at 09:45. Build a time-of-day baseline curve (median VIX1D by
minute-of-day over a trailing window, e.g. 60 sessions) and evaluate
deviations against that, via `vix1d_tod_z`. Regime logic should key off
`vix1d_tod_z`, not the raw level.

## 3. Regime classification

Emit one label per tick (or per bar) with hysteresis to avoid flip-flop.

```python
def classify_regime(f):  # f = feature dict
    # 1. Compression vs expansion (detrended level)
    if f.vix1d_tod_z <= LOW_Z and f.iv_rv_spread > 0:
        vol_state = "COMPRESSED"      # IV rich, RV quiet -> fade/mean-revert
    elif f.vix1d_tod_z >= HIGH_Z or f.iv_rv_spread < 0:
        vol_state = "EXPANSIVE"       # RV catching/exceeding IV -> trend/expand
    else:
        vol_state = "NEUTRAL"
    # 2. Momentum overlay
    spiking = f.vix1d_roc >= ROC_SPIKE_THRESH
    return RegimeLabel(vol_state, spiking)
```

Combine with the existing GEX regime into a 2-factor grid — this is where
the real gating lives:

| | Positive GEX (dealers dampen) | Negative GEX (dealers amplify) |
|---|---|---|
| **COMPRESSED** | Highest-conviction fade the liquidity grab. Range/reversion. | Mixed — respect the grab but expect quick two-sided swings. |
| **EXPANSIVE** | Transitional — vol waking up inside a dampened tape; reduce size. | Trend / grab-becomes-distribution. Favor continuation, widen targets. |

The corners are the tradeable signal; the diagonals are reduce-size /
stand-aside.

## 4. How it gates the strategy

- **Expected-move guardrail.** Reject any entry whose target/stop geometry
  is incoherent with `exp_move_adj`. E.g. don't set a profit target beyond
  ~1σ expected move in a COMPRESSED/Positive-GEX tape, and don't set a stop
  tighter than expected noise.
- **Strike/structure selection.** Feed `exp_move_adj` into strike distance
  for spreads/condors; wider expected move → push short strikes out.
- **Fade-vs-trend routing.** COMPRESSED + Positive GEX biases toward fading
  the open liquidity grab back into range. EXPANSIVE + Negative GEX biases
  toward treating the grab as the start of distribution (trend entry).
- **No-trade filters.**
  - `spiking == True` and no position → stand aside until `vix1d_roc`
    normalizes (don't chase the vol gap).
  - Proxy-vs-official residual outside tolerance → flag data quality,
    downgrade signal confidence.
  - First N minutes: let the Judas/liquidity-grab window resolve before
    acting on the level.
- **Sizing.** Scale position size inversely to `vix1d_tod_z` (bigger when
  quiet/compressed, smaller when expansive), bounded by your existing risk
  limits.

All thresholds live in config, nothing hard-coded:

```yaml
vix1d:
  proxy:
    business_day_year: 252
    strike_stop_rule: two_consecutive_no_bid
    rp_factor: 0.85          # calibrate; VIX1D overstates realized
  regime:
    low_z: -0.75
    high_z: 0.75
    roc_spike_thresh: <calibrate>   # per-minute % move
    hysteresis_bars: 3
  tod_baseline:
    lookback_sessions: 60
    granularity: minute
  qa:
    reconcile_with_official: true
    residual_tolerance: <calibrate>
```

## 5. Module structure

```
engine/
  vol1d/
    __init__.py
    proxy.py         # VIX1D variance-swap computation from OPRA SPXW chain
    features.py      # exp_move, detrend, RV, spreads, ROC
    regime.py        # classify_regime + GEX-cross grid, hysteresis
    baseline.py      # time-of-day baseline build/update (nightly job)
    qa.py            # daily reconcile vs official print, residual logging
    state.py         # Vol1DState dataclass (the single object the engine reads)
```

`Vol1DState` is the only thing the rest of the engine consumes:

```python
@dataclass(frozen=True)
class Vol1DState:
    ts: datetime
    vix1d: float
    exp_move_pts: float
    exp_move_adj: float
    vix1d_tod_z: float
    iv_rv_spread: float
    vix1d_roc: float
    vol_state: str          # COMPRESSED | NEUTRAL | EXPANSIVE
    spiking: bool
    combined_regime: str    # vol_state x GEX corner
    confidence: float       # downgraded when data QA flags
```

Update cadence: recompute on each chain snapshot (align to your GEX
cadence; ~15s is fine and matches the official index refresh). Baseline +
QA run as nightly jobs.

## 6. Journaling additions

Log at entry, exit, and EOD: `vix1d`, `vix1d_tod_z`, `exp_move_adj`,
`iv_rv_spread`, `vol_state`, `combined_regime`, `spiking`,
proxy-vs-official residual. This lets you later bucket P&L by vol regime
and check whether the fade/trend routing is actually paying.

## 7. Validation / backtest plan

1. **Proxy fidelity:** replay N historical sessions; plot proxy vs official
   VIX1D close; report tracking error and intraday correlation. Tune until
   acceptable.
2. **Regime separation:** bucket forward intraday SPX realized move by
   `combined_regime` at 10:00 ET — confirm EXPANSIVE/Neg-GEX shows fatter
   forward ranges than COMPRESSED/Pos-GEX. If it doesn't separate, the
   signal isn't earning its place.
3. **Routing test:** backtest fade rules only in COMPRESSED/Pos-GEX vs
   unconditionally; compare expectancy. Same for trend rules in
   EXPANSIVE/Neg-GEX.
4. **Guardrail test:** measure how many losing trades the expected-move
   guardrail would have vetoed.

Ship v1 as **signal-only / shadow mode** (logs the label and what it would
have done) before it gates live orders.

## 8. Caveats to keep in the code comments

- Proxy ≠ official; treat the level as relative, not absolute.
- VIX1D drifts up intraday by construction — always compare against the
  time-of-day baseline, never the raw level across different times of day.
- VIX1D overstates next-day realized (risk premium); `exp_move_adj` is
  deliberately shrunk.
- Don't overfit thresholds to a handful of sessions. Wide bands +
  hysteresis beat precise-but-fragile cutoffs.
- This module describes the **size** of the field; GEX describes the
  **mechanism**; liquidity levels describe the **targets**. Keep them as
  three independent inputs — don't let one silently proxy another.
