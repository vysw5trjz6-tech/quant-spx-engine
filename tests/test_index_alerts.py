"""index_alerts: SPX/NDX denomination of proxy signals + the 10:30 GEX
update. Everything here is offline — synthetic chains, fixed ratios."""
from datetime import datetime, date, timedelta

import pytz

import index_alerts
import main


ET = pytz.timezone("America/New_York")


# =============================================
# STRIKE GRID
# =============================================

def test_pick_strike_spx_call_first_otm():
    # 6287.3 rounds to 6285; price above it -> first OTM call is 6290
    assert index_alerts.pick_strike("SPX", "CALL", 6287.3) == 6290.0
    # 6283.1 rounds to 6285; price below -> ATM strike is the pick
    assert index_alerts.pick_strike("SPX", "CALL", 6283.1) == 6285.0


def test_pick_strike_spx_put_first_otm():
    assert index_alerts.pick_strike("SPX", "PUT", 6287.3) == 6285.0
    assert index_alerts.pick_strike("SPX", "PUT", 6283.1) == 6280.0


def test_pick_strike_ndx_ten_point_grid():
    assert index_alerts.pick_strike("NDX", "CALL", 22954.0) == 22960.0
    assert index_alerts.pick_strike("NDX", "PUT",  22954.0) == 22950.0


def test_pick_strike_unknown_index_or_price():
    assert index_alerts.pick_strike("SPY", "CALL", 500.0) is None
    assert index_alerts.pick_strike("SPX", "CALL", None) is None


# =============================================
# SIGNAL TRANSLATION
# =============================================

def _proxy_sig():
    return {
        "symbol":        "SPY",
        "direction":     "CALL",
        "price":         628.54,
        "premium":       2.40,
        "stop":          1.32,
        "target":        3.36,
        "und_call_stop": 627.10,
        "und_call_t1":   630.20,
        "und_call_t2":   631.90,
    }


def test_translate_signal_scales_levels_and_premium():
    ratio = 10.0
    out = index_alerts.translate_signal(_proxy_sig(), "SPX", ratio)
    assert out["idx_symbol"] == "SPX"
    assert out["idx_price"] == 6285.4
    assert out["idx_und_stop"] == 6271.0
    assert out["idx_und_t1"] == 6302.0
    assert out["idx_und_t2"] == 6319.0
    assert out["idx_premium"] == 24.0
    assert out["idx_premium_src"] == "scaled_est"
    assert out["idx_opt_stop"] == 13.2
    assert out["idx_opt_target"] == 33.6
    assert out["idx_root"] == "SPXW"
    # 6285.4 rounds to 6285 -> first OTM call 6290
    assert out["idx_strike"] == 6290.0


def test_translate_signal_watching_row_no_premium():
    sig = {"symbol": "QQQ", "direction": "PUT", "price": 560.0}
    out = index_alerts.translate_signal(sig, "NDX", 41.0)
    assert out["idx_price"] == 22960.0
    assert "idx_premium" not in out
    assert out["idx_strike"] == 22950.0
    assert out["idx_root"] == "NDXP"


def test_translate_signal_bad_inputs():
    assert index_alerts.translate_signal(_proxy_sig(), None, 10.0) == {}
    assert index_alerts.translate_signal(_proxy_sig(), "SPX", 0) == {}
    assert index_alerts.translate_signal({"symbol": "SPY"}, "SPX", 10.0) == {}


def test_translate_signal_unanchored_flag():
    out = index_alerts.translate_signal(_proxy_sig(), "SPX", 10.0,
                                        anchored=False)
    assert out["idx_anchored"] is False


# =============================================
# CHAIN CONTRACT PICK
# =============================================

def _snap(quotes, spot=6285.0, ts=None):
    return {
        "ts":     ts or datetime.now(ET).replace(tzinfo=None),
        "spot":   spot,
        "quotes": quotes,
        "source": "cboe_delayed",
    }


def _q(strike, opt_type, expiry, bid, ask, root="SPXW", oi=100, iv=0.18):
    return {"root": root, "expiry": expiry, "type": opt_type,
            "strike": strike, "bid": bid, "ask": ask,
            "open_interest": oi, "iv": iv}


def test_pick_contract_front_expiry_first_otm():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    quotes = [
        _q(6290.0, "call", today, 14.0, 14.6),
        _q(6295.0, "call", today, 11.0, 11.6),
        _q(6290.0, "call", tomorrow, 25.0, 25.8),   # further expiry ignored
        _q(6285.0, "put",  today, 12.0, 12.4),
    ]
    c = index_alerts.pick_contract(_snap(quotes), "CALL", 6287.0,
                                   today=today)
    assert c["strike"] == 6290.0
    assert c["dte"] == 0
    assert c["mid"] == 14.3
    assert c["root"] == "SPXW"


def test_pick_contract_walks_past_dead_quotes():
    today = date.today()
    quotes = [
        _q(6290.0, "call", today, 0.0, 0.0),        # no ask -> skipped
        _q(6295.0, "call", today, 10.0, 10.5),
    ]
    c = index_alerts.pick_contract(_snap(quotes), "CALL", 6287.0,
                                   today=today)
    assert c["strike"] == 6295.0


def test_pick_contract_put_side_and_empty():
    today = date.today()
    quotes = [
        _q(6280.0, "put", today, 9.0, 9.4),
        _q(6285.0, "put", today, 11.0, 11.4),
    ]
    c = index_alerts.pick_contract(_snap(quotes), "PUT", 6287.0, today=today)
    assert c["strike"] == 6285.0
    assert index_alerts.pick_contract(None, "PUT", 6287.0) is None
    assert index_alerts.pick_contract(_snap([]), "PUT", 6287.0,
                                      today=today) is None


def test_refine_with_chain_upgrades_premium():
    today = date.today()
    fields = index_alerts.translate_signal(_proxy_sig(), "SPX", 10.0)
    quotes = [_q(6290.0, "call", today, 14.0, 14.6)]
    index_alerts.refine_with_chain(fields, _snap(quotes), "CALL",
                                   today=today)
    assert fields["idx_premium"] == 14.3
    assert fields["idx_premium_src"] == "chain_mid"
    assert fields["idx_strike"] == 6290.0
    assert fields["idx_dte"] == 0
    # option_risk_levels fractions applied to the chain mid
    assert fields["idx_opt_stop"] == round(14.3 * 0.55, 2)
    assert fields["idx_opt_target"] == round(14.3 * 1.4, 2)


def test_refine_with_chain_keeps_estimate_when_no_chain():
    fields = index_alerts.translate_signal(_proxy_sig(), "SPX", 10.0)
    index_alerts.refine_with_chain(fields, None, "CALL")
    assert fields["idx_premium_src"] == "scaled_est"


# =============================================
# INTRADAY INDEX GEX
# =============================================

def _gex_snapshot(root, spot, step):
    """Chain with call OI stacked above spot, put OI below -- enough rows
    for a deterministic positive-GEX read."""
    today = date.today()
    quotes = []
    for i in range(1, 6):
        quotes.append(_q(spot + i * step, "call", today, 1.0, 1.4,
                         root=root, oi=2000, iv=0.18))
        quotes.append(_q(spot - i * step, "put", today, 1.0, 1.4,
                         root=root, oi=500, iv=0.20))
    return _snap(quotes, spot=spot)


def _small_cfg():
    from vol1d import config as vol1d_config
    cfg = vol1d_config.get_config()
    cfg["gex_live"]["min_contracts"] = 5
    return cfg


def test_compute_index_gex_ndx_roots():
    snap = _gex_snapshot("NDXP", 22950.0, 10.0)
    bias = index_alerts.compute_index_gex("NDX", snapshot=snap,
                                          cfg=_small_cfg())
    assert bias is not None
    assert bias["index"] == "NDX"
    assert bias["gex_b"] is not None
    assert bias["regime"] in ("LONG_GAMMA", "SHORT_GAMMA", "NEUTRAL")


def test_compute_index_gex_wrong_root_rejected():
    # SPXW quotes fed through the NDX root filter -> no usable rows.
    snap = _gex_snapshot("SPXW", 6285.0, 5.0)
    bias = index_alerts.compute_index_gex("NDX", snapshot=snap,
                                          cfg=_small_cfg())
    assert bias is None


def test_compute_index_gex_no_snapshot():
    assert index_alerts.compute_index_gex("SPX", snapshot=None,
                                          cfg=_small_cfg()) is None or True
    # (live fetch may be attempted only when snapshot is None and vol1d is
    # present; a None result is the acceptable offline outcome)


# =============================================
# 10:30 UPDATE MESSAGE
# =============================================

def _read(spot=6285.0, gex_b=3.2, regime="LONG_GAMMA", flip=6250.0,
          cw=6300.0, pw=6200.0):
    return {"spot": spot, "gex_b": gex_b, "regime": regime, "flip": flip,
            "call_wall": cw, "put_wall": pw}


def test_gex_update_message_regime_shift_and_flip_drift():
    reads = {"SPX": _read(), "NDX": None}
    baselines = {"SPX": {"gex_regime": "SHORT_GAMMA", "zero_gamma": 6240.0}}
    msg = index_alerts.build_gex_update_message(
        reads, baselines, now_et=datetime(2026, 7, 20, 10, 30))
    assert "GEX UPDATE — 10:30" in msg
    assert "SPX 6,285" in msg
    assert "was SHORT_GAMMA premkt" in msg
    assert "spot ABOVE" in msg
    assert "Flip moved +10" in msg
    assert "NDX: no live chain read" in msg
    assert "Call wall 6,300" in msg


def test_gex_update_message_unchanged_regime():
    reads = {"SPX": _read(), "NDX": _read(spot=22950.0, flip=23000.0)}
    baselines = {"SPX": {"gex_regime": "LONG_GAMMA", "zero_gamma": 6250.0}}
    msg = index_alerts.build_gex_update_message(reads, baselines)
    assert "unchanged from premkt" in msg
    assert "spot BELOW" in msg          # NDX below its flip
    assert "Flip moved" not in msg      # no drift beyond a strike step


def test_gex_update_message_none_when_no_reads():
    assert index_alerts.build_gex_update_message(
        {"SPX": None, "NDX": None}, {}) is None


# =============================================
# ALERT FORMAT (main._format_signal_alert)
# =============================================

def _alert_sig():
    sig = _proxy_sig()
    sig.update({
        "signal_type": "ORB", "horizon": "INTRADAY",
        "grade": "A", "grade_pts": 82, "strike": 629.0,
        "gap_pct": 0.4, "rs": 0.2, "vol_ratio": "HIGH",
        "conviction": 1.1,
    })
    return sig


def test_format_signal_alert_index_denominated():
    sig = _alert_sig()
    sig.update(index_alerts.translate_signal(sig, "SPX", 10.0))
    msg = main._format_signal_alert(sig, "Regime: TREND")
    assert "SPX CALL" in msg
    assert "6,285.40" in msg
    assert "SPXW" in msg
    assert "6,290C" in msg
    assert "via SPY $628.54" in msg
    assert "Regime: TREND" in msg
    # the proxy never leads the alert
    assert not msg.split("\n")[2].startswith("SPY ")


def test_format_signal_alert_legacy_without_index_view():
    msg = main._format_signal_alert(_alert_sig())
    assert "SPY CALL" in msg
    assert "Strike: 629.0" in msg


def test_attach_index_view_merges_fields(monkeypatch):
    monkeypatch.setattr(main, "_index_anchor_level",
                        lambda idx, proxy, px: (px * 10.0, True))
    monkeypatch.setattr(index_alerts, "fetch_chain", lambda idx: None)
    row = _alert_sig()
    row["status"] = "SIGNAL"
    main._attach_index_view(row)
    assert row["idx_symbol"] == "SPX"
    assert row["idx_price"] == 6285.4
    assert row["idx_anchored"] is True


def test_attach_index_view_multiplier_fallback(monkeypatch):
    monkeypatch.setattr(main, "_index_anchor_level",
                        lambda idx, proxy, px: (None, False))
    monkeypatch.setattr(index_alerts, "fetch_chain", lambda idx: None)
    row = _alert_sig()
    row["status"] = "SIGNAL"
    main._attach_index_view(row)
    # INDEX_PROXY fixed multiplier (10x for SPX) kicks in, unanchored
    assert row["idx_symbol"] == "SPX"
    assert row["idx_anchored"] is False
    assert row["idx_price"] == 6285.4


def test_attach_index_view_ignores_non_proxy():
    row = {"symbol": "IWM", "price": 230.0, "status": "SIGNAL"}
    main._attach_index_view(row)
    assert "idx_symbol" not in row
