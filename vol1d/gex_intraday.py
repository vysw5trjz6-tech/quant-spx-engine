# vol1d/gex_intraday.py
# Intraday GEX v2 — delayed-feed architecture.
#
# GEX = a slow-moving curve over strikes (positioning) x a fast-moving
# evaluation point (spot). The free delayed CBOE chain only affects the
# slow part, so the module runs two loops:
#
#   SLOW (~5 min, rides the updater's chain fetch): OI baseline + unsigned
#   volume-diff flow -> per-strike positioning + mid IV -> reusable GEX
#   curve, flip, walls. Snapshots persist as the free historical dataset.
#
#   FAST (~15s, every updater pass): live SPY (Alpaca) bridged to an SPX
#   estimate via a rolling ratio basis; the latest curve is re-evaluated
#   at that spot — regime, spot_vs_flip, wall distances — with NO chain
#   math and no re-fetch.
#
# Because positioning is up to ~20 min old, the regime state machine damps
# flips: positive/negative needs TWO consecutive slow-loop profiles
# agreeing on the side of the flip AND |spot - flip| >= flip_band_pct;
# the fast loop may demote to `transitional` immediately (spot crossing
# the flip is live information) but never promotes out of it. A stale
# chain (> stale_secs) freezes the state entirely.
#
# Honest limits (spec: log, don't fix in v1):
#   * The flow layer is UNSIGNED volume — it inherits the crude dealer-sign
#     convention (dealers long calls / short puts) fully, hence the 0.5x
#     weight and the validation plan to drop it if it never changes a call.
#   * ~15-20 min positioning lag is worst near the close, exactly when
#     0DTE gamma is sharpest — final-hour walls are approximate.
#   * A process restart mid-session re-counts the day's cumulative volume
#     as fresh flow once (accumulators are in-memory by design).

import json
import math
import statistics
import zlib
from collections import deque
from datetime import datetime, timedelta

import pytz

import db_utils
from gamma_exposure import bs_gamma
from vol1d import config as vol1d_config

try:
    import vol_math
    _HAS_VOL_MATH = True
except ImportError:
    _HAS_VOL_MATH = False

ET = pytz.timezone("America/New_York")

_DB = db_utils.data_path("vol1d_state.db")

POSITIVE = "positive"
NEGATIVE = "negative"
TRANSITIONAL = "transitional"

# Settlement used for T: 4:00 PM ET (SPXW PM settle). SPX AM-settled
# monthlies technically settle at the open; the difference is immaterial
# for gamma at <= 5 DTE and the spec pins 4 PM.
_SETTLE_HOUR = 16


def _connect(db_path=None):
    conn = db_utils.connect(db_path or _DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gex_intraday_snapshots (
            session_date TEXT NOT NULL,
            ts           TEXT NOT NULL,
            chain_ts     TEXT,
            spot_ref     REAL,
            net_gex_all  REAL,
            net_gex_0dte REAL,
            flip         REAL,
            call_wall    REAL,
            put_wall     REAL,
            rows_blob    BLOB,
            PRIMARY KEY (session_date, ts)
        )
    """)
    return conn


# =============================================
# SLOW LOOP — positioning book + profile builder
# =============================================

class _StrikeRow(object):
    """Per (expiry, strike) accumulator. Plain attributes, no dataclass —
    ~10k of these are touched per ingest."""
    __slots__ = ("oi_call", "oi_put", "cum_dvol_call", "cum_dvol_put",
                 "last_vol_call", "last_vol_put",
                 "iv_call", "iv_put", "mid_call", "mid_put")

    def __init__(self):
        self.oi_call = self.oi_put = 0
        self.cum_dvol_call = self.cum_dvol_put = 0
        self.last_vol_call = self.last_vol_put = 0
        self.iv_call = self.iv_put = None
        self.mid_call = self.mid_put = None


class DelayedGEX(object):
    """Owns the positioning book and the latest reusable profile. One
    instance per session (the updater recreates it daily, which also
    resets the flow accumulators at the open)."""

    def __init__(self, cfg=None, db_path=None):
        self.cfg = (cfg or vol1d_config.get_config())["gex_intraday"]
        self.db_path = db_path
        self.rows = {}            # (expiry, strike) -> _StrikeRow
        self.profile = None       # latest computed curve (reusable)
        self.chain_ts = None      # the CHAIN's own timestamp
        self.session_date = None

    # ---- ingestion -------------------------------------------------------

    def on_snapshot(self, snapshot, now_et=None):
        """Ingest one chain snapshot: OI baseline, volume-diff flow, IV.
        Cadence-invariant: cumulative session volume diffs to the same
        total whether fed every 15s or every 5 min."""
        now_et = now_et or datetime.now(ET).replace(tzinfo=None)
        session = now_et.strftime("%Y-%m-%d")
        if session != self.session_date:
            # New session: yesterday's volume rolled into today's OI print,
            # so the flow accumulators restart from zero.
            self.rows = {}
            self.profile = None
            self.session_date = session

        roots = set(self.cfg["roots"])
        today = now_et.date()
        max_dte = self.cfg["max_dte"]

        for q in snapshot.get("quotes") or []:
            if q.get("root") not in roots:
                continue
            dte = (q["expiry"] - today).days
            if dte < 0 or dte > max_dte:
                continue
            r = self.rows.setdefault((q["expiry"], q["strike"]), _StrikeRow())
            oi = q.get("open_interest") or 0
            vol = q.get("volume") or 0
            bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else None
            iv = q.get("iv")
            if q["type"] == "call":
                r.oi_call = oi
                r.cum_dvol_call += max(0, vol - r.last_vol_call)
                r.last_vol_call = vol
                r.iv_call, r.mid_call = iv, mid
            else:
                r.oi_put = oi
                r.cum_dvol_put += max(0, vol - r.last_vol_put)
                r.last_vol_put = vol
                r.iv_put, r.mid_put = iv, mid

        self.chain_ts = snapshot.get("chain_ts") or snapshot.get("ts")

    # ---- IV handling -----------------------------------------------------

    def _clamp_iv(self, iv):
        if iv is None:
            return None
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            return None
        if iv <= 0:
            return None
        if iv > 5.0:          # percent-form feed (18.0 for 18%)
            iv = iv / 100.0
        return min(max(iv, self.cfg["iv_lo"]), self.cfg["iv_hi"])

    def _resolve_iv(self, feed_iv, mid, spot, strike, t_years, opt_type):
        """Feed IV first (clamped to the wings); solve from the delayed mid
        when the feed didn't supply one; None -> interpolated later."""
        iv = self._clamp_iv(feed_iv)
        if iv is not None:
            return iv
        if mid and _HAS_VOL_MATH:
            solved = vol_math.implied_vol(mid, spot, strike, t_years,
                                          option_type=opt_type)
            return self._clamp_iv(solved)
        return None

    @staticmethod
    def _interpolate_gaps(strikes, ivs):
        """Linear IV interpolation across gaps within one expiry/right,
        flat-extrapolated at the edges. `ivs` mutated in place."""
        known = [i for i, v in enumerate(ivs) if v is not None]
        if not known:
            return
        for i in range(len(ivs)):
            if ivs[i] is not None:
                continue
            lo = max((j for j in known if j < i), default=None)
            hi = min((j for j in known if j > i), default=None)
            if lo is None:
                ivs[i] = ivs[hi]
            elif hi is None:
                ivs[i] = ivs[lo]
            else:
                w = ((strikes[i] - strikes[lo])
                     / (strikes[hi] - strikes[lo]))
                ivs[i] = ivs[lo] + w * (ivs[hi] - ivs[lo])

    # ---- profile build ---------------------------------------------------

    def _t_years(self, expiry, now_et):
        settle = datetime(expiry.year, expiry.month, expiry.day, _SETTLE_HOUR)
        secs = (settle - now_et).total_seconds()
        floor = self.cfg["min_t_minutes"] * 60.0
        return max(secs, floor) / (365.0 * 86400.0)

    def build_profile(self, spot_ref, now_et=None):
        """Rebuild the reusable curve. Returns the profile dict (also kept
        on self.profile) or None on a thin/unusable book.

        The curve stores per-row (strike, T, iv, signed position) so the
        fast loop can re-evaluate gamma at any spot without the chain."""
        now_et = now_et or datetime.now(ET).replace(tzinfo=None)
        if spot_ref is None or spot_ref <= 0 or not self.rows:
            return None
        w = self.cfg["flow_weight"]
        today = now_et.date()

        # Resolve IVs per (expiry, right) column, then interpolate gaps.
        by_expiry = {}
        for (expiry, strike), r in self.rows.items():
            by_expiry.setdefault(expiry, []).append((strike, r))

        curve = []                     # (K, t_years, iv, pos_signed)
        for expiry, items in by_expiry.items():
            dte = (expiry - today).days
            if dte < 0 or dte > self.cfg["max_dte"]:
                continue
            items.sort(key=lambda kr: kr[0])
            t = self._t_years(expiry, now_et)
            strikes = [k for k, _ in items]
            for opt_type in ("call", "put"):
                ivs = []
                for k, r in items:
                    feed_iv = r.iv_call if opt_type == "call" else r.iv_put
                    mid = r.mid_call if opt_type == "call" else r.mid_put
                    ivs.append(self._resolve_iv(feed_iv, mid, spot_ref, k,
                                                t, opt_type))
                self._interpolate_gaps(strikes, ivs)
                for (k, r), iv in zip(items, ivs):
                    if iv is None:
                        continue
                    if opt_type == "call":
                        pos = r.oi_call + w * r.cum_dvol_call
                    else:
                        pos = -(r.oi_put + w * r.cum_dvol_put)
                    if pos != 0:
                        curve.append((k, t, iv, pos, dte))

        if len({k for k, _, _, _, _ in curve}) < self.cfg["min_strikes"]:
            return None

        by_strike = {}
        net_all = net_0dte = 0.0
        for k, t, iv, pos, dte in curve:
            gex_k = (bs_gamma(spot_ref, k, t, iv)
                     * pos * 100 * spot_ref ** 2 * 0.01)
            by_strike[k] = by_strike.get(k, 0.0) + gex_k
            net_all += gex_k
            if dte == 0:
                net_0dte += gex_k

        self.profile = {
            "built_at":     now_et,
            "chain_ts":     self.chain_ts,
            "spot_ref":     spot_ref,
            "curve":        curve,
            "gex_by_strike": by_strike,
            "net_gex_all":  net_all,
            "net_gex_0dte": net_0dte,
            "flip":         _flip_strike(by_strike),
            "call_wall":    _call_wall(by_strike),
            "put_wall":     _put_wall(by_strike),
        }
        return self.profile

    # ---- persistence (free historical dataset) ---------------------------

    def persist_snapshot(self, now_et=None):
        """Bank the latest profile + compact per-strike book. Prunes rows
        older than persist_days on each write."""
        if not self.cfg["persist_snapshots"] or not self.profile:
            return
        p = self.profile
        now_et = now_et or p["built_at"]
        compact = [
            [e.isoformat(), k, r.oi_call, r.oi_put,
             r.cum_dvol_call, r.cum_dvol_put, r.iv_call, r.iv_put]
            for (e, k), r in sorted(self.rows.items())
        ]
        blob = zlib.compress(json.dumps(compact).encode())
        conn = _connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO gex_intraday_snapshots
            (session_date, ts, chain_ts, spot_ref, net_gex_all,
             net_gex_0dte, flip, call_wall, put_wall, rows_blob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (self.session_date, now_et.isoformat(),
              p["chain_ts"].isoformat() if p["chain_ts"] else None,
              p["spot_ref"], p["net_gex_all"], p["net_gex_0dte"],
              p["flip"], p["call_wall"], p["put_wall"], blob))
        cutoff = (now_et - timedelta(days=self.cfg["persist_days"]))
        conn.execute("DELETE FROM gex_intraday_snapshots "
                     "WHERE session_date < ?", (cutoff.strftime("%Y-%m-%d"),))
        conn.commit()
        conn.close()

    def low_gex_threshold(self):
        """Trailing p25 of banked |net_gex_all|, or None until enough
        sessions exist (the regime machine then skips the check)."""
        conn = _connect(self.db_path)
        sessions = [r[0] for r in conn.execute(
            "SELECT DISTINCT session_date FROM gex_intraday_snapshots "
            "ORDER BY session_date DESC LIMIT ?",
            (self.cfg["pctile_lookback_sessions"],))]
        if len(sessions) < self.cfg["pctile_min_sessions"]:
            conn.close()
            return None
        vals = [abs(r[0]) for r in conn.execute(
            "SELECT net_gex_all FROM gex_intraday_snapshots "
            "WHERE session_date >= ? AND net_gex_all IS NOT NULL",
            (min(sessions),))]
        conn.close()
        if len(vals) < 4:
            return None
        q = statistics.quantiles(vals, n=100)
        return q[self.cfg["pctile"] - 1]


def _flip_strike(by_strike):
    """Zero-cross of cumulative GEX walking strikes upward, linearly
    interpolated between the bracketing strikes."""
    strikes = sorted(by_strike)
    cum = 0.0
    prev_k, prev_cum = None, None
    for k in strikes:
        cum += by_strike[k]
        if (prev_cum is not None and prev_cum != cum
                and (prev_cum < 0 <= cum or prev_cum > 0 >= cum)):
            frac = abs(prev_cum) / abs(cum - prev_cum)
            return prev_k + frac * (k - prev_k)
        prev_k, prev_cum = k, cum
    return None


def _call_wall(by_strike):
    pos = {k: v for k, v in by_strike.items() if v > 0}
    return max(pos, key=pos.get) if pos else None


def _put_wall(by_strike):
    neg = {k: v for k, v in by_strike.items() if v < 0}
    return min(neg, key=neg.get) if neg else None


# =============================================
# FAST LOOP — live spot proxy (SPY route)
# =============================================

class SpotProxy(object):
    """Bridge the delayed SPX quote to a live SPX estimate off live SPY.

    The chain's SPX spot is ~15 min old; live SPY samples are buffered so
    each slow-loop tick can pair the delayed SPX with the SPY price AS OF
    the chain's timestamp: ratio = spx_delayed / (spy_then * 10), EMA'd.
    fast loop: spot_est = spy_live * 10 * ema(ratio). The basis (ETF
    expense/dividend drift) moves slowly; an EMA over ~30 min is plenty.

    The repo has no broker ES feed (Yahoo ES=F is daily-bar fallback only,
    often blocked from datacenter IPs), so the SPY route is the live one.
    """

    def __init__(self, alpha=0.3, pair_tolerance_secs=420, buffer_secs=2400):
        self.alpha = alpha
        self.pair_tolerance = pair_tolerance_secs
        self.buffer_secs = buffer_secs
        self.ratio_ema = None
        self._buf = deque()       # (ts_et_naive, spy_price)

    def record_live(self, now_et, spy_price):
        if not spy_price or spy_price <= 0:
            return
        self._buf.append((now_et, float(spy_price)))
        cutoff = now_et - timedelta(seconds=self.buffer_secs)
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def update_basis(self, spx_delayed, chain_ts):
        """Called each slow-loop tick with the delayed SPX spot and the
        chain's own timestamp. No buffered SPY near chain_ts -> no update
        (never pair values from different clock windows)."""
        if not spx_delayed or not chain_ts or not self._buf:
            return
        ts, spy = min(self._buf,
                      key=lambda p: abs((p[0] - chain_ts).total_seconds()))
        if abs((ts - chain_ts).total_seconds()) > self.pair_tolerance:
            return
        ratio = spx_delayed / (spy * 10.0)
        self.ratio_ema = (ratio if self.ratio_ema is None
                          else self.alpha * ratio
                          + (1 - self.alpha) * self.ratio_ema)

    def spot(self, spy_live):
        """Live SPX estimate, or None before the first basis pairing."""
        if self.ratio_ema is None or not spy_live or spy_live <= 0:
            return None
        return spy_live * 10.0 * self.ratio_ema


# =============================================
# REGIME STATE MACHINE (flip damping)
# =============================================

class RegimeMachine(object):
    """positive / negative / transitional per the spec: promotion out of
    transitional needs `confirm_profiles` consecutive slow-loop profiles
    agreeing on the flip side AND |spot-flip| >= flip_band_pct; the fast
    loop demotes immediately but never promotes; a stale chain freezes
    the state entirely."""

    def __init__(self, cfg_gi):
        self.cfg = cfg_gi
        self.state = TRANSITIONAL
        self._signs = deque(maxlen=cfg_gi["confirm_profiles"])

    @staticmethod
    def _sign(profile, spot):
        flip = profile.get("flip")
        if flip and spot:
            return 1 if spot >= flip else -1
        # No zero-cross: the whole curve is one sign.
        net = profile.get("net_gex_all") or 0.0
        return 1 if net > 0 else (-1 if net < 0 else 0)

    def _band_pct(self, profile, spot):
        flip = profile.get("flip")
        if not flip or not spot:
            return None
        return abs(spot - flip) / spot * 100.0

    def on_profile(self, profile, spot, low_gex_threshold=None, stale=False):
        """Slow-loop tick: the only path that can emit positive/negative."""
        if stale or not profile:
            return self.state
        self._signs.append(self._sign(profile, spot))

        band = self._band_pct(profile, spot)
        inside_band = band is not None and band < self.cfg["flip_band_pct"]
        low_gex = (low_gex_threshold is not None
                   and abs(profile.get("net_gex_all") or 0.0)
                   < low_gex_threshold)
        agreed = (len(self._signs) == self._signs.maxlen
                  and len(set(self._signs)) == 1 and self._signs[0] != 0)

        if inside_band or low_gex or not agreed:
            self.state = TRANSITIONAL
        else:
            self.state = POSITIVE if self._signs[0] > 0 else NEGATIVE
        return self.state

    def on_fast(self, profile, spot, stale=False):
        """Fast-loop tick: spot crossing into the flip band is live
        information -> demote now. Promotion waits for the slow loop."""
        if stale or not profile or self.state == TRANSITIONAL:
            return self.state
        band = self._band_pct(profile, spot)
        crossed = (band is not None and band < self.cfg["flip_band_pct"]) or \
            (self._sign(profile, spot) > 0) != (self.state == POSITIVE)
        if crossed:
            self.state = TRANSITIONAL
        return self.state


# =============================================
# EVALUATION — output contract
# =============================================

def evaluate(profile, spot_est, machine, now_et=None, chain_ts=None,
             cfg_gi=None, live_est=True):
    """Curve lookup at the evaluation spot — no chain math. Returns the
    spec's output contract dict, or None without a profile. `spot_est`
    None (basis not yet paired) falls back to the delayed chain spot,
    labeled via spot_source."""
    if not profile:
        return None
    cfg_gi = cfg_gi or vol1d_config.get_config()["gex_intraday"]
    now_et = now_et or datetime.now(ET).replace(tzinfo=None)
    chain_ts = chain_ts or profile.get("chain_ts")

    stale = (chain_ts is None
             or (now_et - chain_ts).total_seconds() > cfg_gi["stale_secs"])

    spot_source = "proxy"
    if spot_est is None:
        spot_est, spot_source = profile["spot_ref"], "chain_delayed"

    flip, cw, pw = profile["flip"], profile["call_wall"], profile["put_wall"]

    net_live = None
    if live_est and spot_est and spot_est != profile["spot_ref"]:
        # Positioning stale, spot live — clearly an estimate.
        net_live = sum(
            bs_gamma(spot_est, k, t, iv) * pos * 100 * spot_est ** 2 * 0.01
            for k, t, iv, pos, _dte in profile["curve"])

    def _pct(a, b):
        return round((a - b) / spot_est * 100.0, 4) if (
            a is not None and b is not None and spot_est) else None

    return {
        "ts":                    now_et.isoformat(),
        "chain_ts":              chain_ts.isoformat() if chain_ts else None,
        "stale":                 stale,
        "spot_est":              round(spot_est, 2) if spot_est else None,
        "spot_source":           spot_source,
        "net_gex_all":           round(profile["net_gex_all"], 0),
        "net_gex_0dte":          round(profile["net_gex_0dte"], 0),
        "net_gex_live_est":      round(net_live, 0) if net_live is not None
                                 else None,
        "flip":                  round(flip, 2) if flip else None,
        "call_wall":             cw,
        "put_wall":              pw,
        "regime":                machine.state if machine else TRANSITIONAL,
        "spot_vs_flip_pct":      _pct(spot_est, flip),
        "dist_to_call_wall_pct": _pct(cw, spot_est),
        "dist_to_put_wall_pct":  _pct(spot_est, pw),
        "source":                "gex_intraday",
    }


def to_grid_bias(out):
    """Collapse the v2 output onto the gex_bias vocabulary the regime grid
    consumes (vol1d.regime.map_gex_regime). `transitional` maps NEUTRAL ->
    UNKNOWN_GEX -> STAND_ASIDE — never take a regime call off a single
    delayed snapshot."""
    if not out:
        return None
    regime = {POSITIVE: "LONG_GAMMA", NEGATIVE: "SHORT_GAMMA"}.get(
        out.get("regime"), "NEUTRAL")
    return {
        "regime":    regime,
        "flip":      out.get("flip"),
        "call_wall": out.get("call_wall"),
        "put_wall":  out.get("put_wall"),
        "spot":      out.get("spot_est"),
        "stale":     out.get("stale"),
        "source":    "gex_intraday",
    }
