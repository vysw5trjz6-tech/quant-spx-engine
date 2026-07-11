# Intraday GEX from the delayed CBOE chain: payload OI/IV parsing, the
# snapshot -> gamma_exposure mapping, sign/neutral banding, and the
# updater's live-over-snapshot preference with throttling and the
# snapshot-fallback confidence downgrade.
#
# gamma_exposure.compute_gex_from_chain reads the REAL clock for DTE, so
# fixtures use dynamic future business days (the fixed historic dates the
# proxy tests pin would all be dropped as expired).

from datetime import date, datetime, timedelta

import vol_math
from vol1d import config as vol1d_config
from vol1d import daycount, gex_live, regime
from vol1d import state as vol1d_state
from vol1d.chain_source import ChainSource, CboeDelayedChainSource


def _next_bday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


TODAY = _next_bday(date.today() + timedelta(days=1))
NEXT  = _next_bday(TODAY + timedelta(days=1))
NOW   = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0)
SPOT  = 6300.0


def _cfg(**gex_over):
    cfg = vol1d_config.get_config()
    cfg["proxy"]["risk_free_rate"] = 0.0
    cfg["gex_live"].update({"min_contracts": 2, "neutral_band_b": 0.001,
                            "min_interval_secs": 60})
    cfg["gex_live"].update(gex_over)
    return cfg


def _quote(expiry, typ, strike, bid=1.0, ask=1.2, oi=0, iv=0.2, root="SPXW"):
    return {"root": root, "expiry": expiry, "type": typ, "strike": strike,
            "bid": bid, "ask": ask, "open_interest": oi, "iv": iv}


def _snap(quotes, ts=NOW):
    return {"ts": ts, "spot": SPOT, "quotes": quotes, "source": "test"}


# ---------------------------------------------------------------------------
# Payload parsing carries OI/IV
# ---------------------------------------------------------------------------

def test_payload_parse_carries_oi_and_iv():
    payload = {"data": {
        "current_price": SPOT,
        "options": [
            {"option": "SPXW{:%y%m%d}C06300000".format(TODAY),
             "bid": 5.0, "ask": 5.5, "open_interest": 1200, "iv": 0.18},
            {"option": "SPX{:%y%m%d}P06300000".format(TODAY),
             "bid": 4.0, "ask": 4.5},   # no OI/IV in payload
        ],
    }}
    snap = CboeDelayedChainSource(roots=["SPXW", "SPX"]).parse_payload(payload)
    by_root = {q["root"]: q for q in snap["quotes"]}
    assert by_root["SPXW"]["open_interest"] == 1200
    assert by_root["SPXW"]["iv"] == 0.18
    assert by_root["SPX"]["open_interest"] == 0     # degrades, not dropped
    assert by_root["SPX"]["iv"] is None


# ---------------------------------------------------------------------------
# snapshot -> gamma_exposure row mapping
# ---------------------------------------------------------------------------

def test_mapping_filters_roots_window_and_dead_quotes():
    far = TODAY + timedelta(days=30)
    quotes = [
        _quote(TODAY, "call", 6300, oi=100),                    # keep
        _quote(TODAY, "call", 6310, oi=100, root="SPX"),        # keep (SPX in gex roots)
        _quote(TODAY, "call", 6320, oi=100, root="XSP"),        # wrong root
        _quote(far,   "call", 6300, oi=100),                    # past window
        _quote(TODAY, "call", 6330, oi=0),                      # no OI
        _quote(TODAY, "call", 6340, oi=100, iv=None),           # no IV
    ]
    rows = gex_live.snapshot_to_gex_chain(_snap(quotes), _cfg()["gex_live"],
                                          today=date.today())
    assert sorted(r["strike"] for r in rows) == [6300, 6310]


def test_percent_form_iv_is_normalized():
    rows = gex_live.snapshot_to_gex_chain(
        _snap([_quote(TODAY, "call", 6300, oi=100, iv=18.0)]),
        _cfg()["gex_live"], today=date.today())
    assert rows[0]["implied_volatility"] == 0.18


# ---------------------------------------------------------------------------
# compute_bias: sign convention, neutral band, thin-chain refusal
# ---------------------------------------------------------------------------

def _book(call_oi, put_oi, n=6):
    quotes = []
    for i in range(n):
        k = 6280.0 + 10 * i
        quotes.append(_quote(TODAY, "call", k, oi=call_oi))
        quotes.append(_quote(TODAY, "put",  k, oi=put_oi))
    return quotes


def test_calls_heavy_book_reads_long_gamma():
    out = gex_live.compute_bias(_snap(_book(call_oi=5000, put_oi=100)), _cfg())
    assert out is not None
    assert out["regime"] == "LONG_GAMMA"
    assert out["gex_b"] > 0
    assert out["source"] == "cboe_delayed_live"


def test_puts_heavy_book_reads_short_gamma():
    out = gex_live.compute_bias(_snap(_book(call_oi=100, put_oi=5000)), _cfg())
    assert out["regime"] == "SHORT_GAMMA"
    assert out["gex_b"] < 0


def test_neutral_band_maps_small_gex_to_neutral():
    out = gex_live.compute_bias(_snap(_book(call_oi=5000, put_oi=100)),
                                _cfg(neutral_band_b=1e9))
    assert out["regime"] == "NEUTRAL"
    assert regime.map_gex_regime(out) == regime.UNKNOWN_GEX


def test_thin_chain_and_disabled_return_none():
    assert gex_live.compute_bias(_snap(_book(5000, 100)),
                                 _cfg(min_contracts=999)) is None
    assert gex_live.compute_bias(_snap(_book(5000, 100)),
                                 _cfg(enabled=False)) is None


# ---------------------------------------------------------------------------
# Updater integration: live beats snapshot; throttle; fallback + confidence
# ---------------------------------------------------------------------------

class _FullSource(ChainSource):
    """Proxy-computable flat-vol chain (both strips) whose call side also
    carries OI, so the same snapshot feeds strips AND the live GEX."""

    def __init__(self, call_oi=5000, put_oi=100, now=NOW):
        self.call_oi, self.put_oi, self.now = call_oi, put_oi, now

    def get_snapshot(self):
        quotes = []
        for expiry in (TODAY, NEXT):
            t = daycount.business_time_to_expiry(self.now, expiry)
            for k in range(int(SPOT * 0.96), int(SPOT * 1.04), 5):
                for typ, oi in (("call", self.call_oi), ("put", self.put_oi)):
                    mid = vol_math.bs_price(SPOT, float(k), t, 0.0, 0.2, typ)
                    live = mid >= 0.05
                    quotes.append({
                        "root": "SPXW", "expiry": expiry, "type": typ,
                        "strike": float(k),
                        "bid": mid * 0.99 if live else 0.0,
                        "ask": mid * 1.01 if live else 0.05,
                        "open_interest": oi, "iv": 0.2,
                    })
        return _snap(quotes, ts=self.now)


def test_updater_prefers_live_gex_over_stale_snapshot(tmp_path):
    up = vol1d_state.Vol1DUpdater(chain_source=_FullSource(), cfg=_cfg(),
                                  db_path=str(tmp_path / "s.db"))
    # Nightly snapshot says SHORT; the live calls-heavy book says LONG.
    st = up.compute_once(now_et=NOW, gex_bias={"regime": "SHORT_GAMMA"})
    assert st.gex_source == "live"
    assert st.combined_regime.endswith("POS_GEX")
    assert up.gex_live["regime"] == "LONG_GAMMA"


def test_updater_falls_back_to_snapshot_when_live_unavailable(tmp_path):
    up = vol1d_state.Vol1DUpdater(chain_source=_FullSource(),
                                  cfg=_cfg(enabled=False),
                                  db_path=str(tmp_path / "s.db"))
    st = up.compute_once(now_et=NOW, gex_bias={"regime": "SHORT_GAMMA"})
    assert st.gex_source == "snapshot"
    assert st.combined_regime.endswith("NEG_GEX")
    assert up.gex_live is None


def test_snapshot_fallback_costs_confidence():
    cfg = _cfg()
    live = vol1d_state.compute_confidence(0.5, 60, 0.5, regime.POS_GEX, 1.0,
                                          cfg, gex_source="live")
    snap = vol1d_state.compute_confidence(0.5, 60, 0.5, regime.POS_GEX, 1.0,
                                          cfg, gex_source="snapshot")
    assert live == 1.0
    assert snap == cfg["confidence"]["snapshot_gex_mult"]


def test_live_gex_compute_is_throttled(tmp_path):
    src = _FullSource(call_oi=5000, put_oi=100)
    up = vol1d_state.Vol1DUpdater(chain_source=src,
                                  cfg=_cfg(min_interval_secs=300),
                                  db_path=str(tmp_path / "s.db"))
    up.compute_once(now_et=NOW, gex_bias=None)
    assert up.gex_live["regime"] == "LONG_GAMMA"

    # Book flips 15s later — inside the throttle, the read must not move.
    src.call_oi, src.put_oi = 100, 5000
    up.compute_once(now_et=NOW + timedelta(seconds=15), gex_bias=None)
    assert up.gex_live["regime"] == "LONG_GAMMA"

    # Past the interval it recomputes and sees the flip.
    up.compute_once(now_et=NOW + timedelta(seconds=301), gex_bias=None)
    assert up.gex_live["regime"] == "SHORT_GAMMA"
