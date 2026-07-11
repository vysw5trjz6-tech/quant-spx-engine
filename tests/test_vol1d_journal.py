# vol1d journaling (spec §6): entry rows on signals/trades and exit fields
# on trades must carry the vol regime, so P&L can be bucketed by
# combined_regime later. Absence of the module/state must never block a
# trade from logging.

from datetime import datetime

import db_utils
import main
from vol1d import regime as vol1d_regime
from vol1d.state import Vol1DState


def _fake_state(**over):
    base = dict(
        ts=datetime(2026, 7, 8, 10, 30), vix1d=18.5, exp_move_pts=73.4,
        exp_move_adj=62.4, vix1d_tod_z=-0.9, iv_rv_spread=2.1,
        vix1d_roc=0.2, vol_state="COMPRESSED", spiking=False,
        combined_regime="COMPRESSED/POS_GEX", confidence=1.0,
        grid_action=vol1d_regime.FADE, spot=6300.0, baseline_sessions=60,
        qa_residual=-0.4)
    base.update(over)
    return Vol1DState(**base)


def _install_state(monkeypatch, st):
    with main._market_state_lock:
        main._market_state["vol1d"] = st


def _one(sql, args=()):
    conn = db_utils.connect(main.DB_FILE)
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def test_signal_row_carries_vol1d_fields(monkeypatch):
    main.init_db()
    main.init_vol1d_journal_columns()
    _install_state(monkeypatch, _fake_state())
    main.db_log_signal({"symbol": "SPY", "direction": "CALL", "price": 630.0,
                        "score": 80, "premium": 1.5})
    row = _one("SELECT vix1d, vix1d_tod_z, exp_move_adj, iv_rv_spread, "
               "vol_state, combined_regime, vol1d_spiking, vol1d_residual, "
               "vol1d_confidence FROM signals ORDER BY id DESC LIMIT 1")
    assert row == (18.5, -0.9, 62.4, 2.1, "COMPRESSED",
                   "COMPRESSED/POS_GEX", 0, -0.4, 1.0)


def test_trade_entry_and_exit_stamps(monkeypatch):
    main.init_db()
    main.init_vol1d_journal_columns()
    _install_state(monkeypatch, _fake_state())
    tid = main.db_log_trade("SPY", "CALL", 1.5, mode="auto")
    assert tid is not None
    row = _one("SELECT vol_state, combined_regime FROM trades WHERE id=?",
               (tid,))
    assert row == ("COMPRESSED", "COMPRESSED/POS_GEX")

    # Regime flips before the close; exit stamp must reflect exit time.
    _install_state(monkeypatch, _fake_state(
        vix1d=24.0, vix1d_tod_z=1.4, vol_state="EXPANSIVE",
        combined_regime="EXPANSIVE/NEG_GEX", spiking=True))
    main.db_close_trade(tid, 2.0, "WIN")
    row = _one("SELECT exit_vix1d, exit_vix1d_tod_z, exit_vol_state, "
               "exit_combined_regime, exit_vol1d_spiking FROM trades "
               "WHERE id=?", (tid,))
    assert row == (24.0, 1.4, "EXPANSIVE", "EXPANSIVE/NEG_GEX", 1)


def test_paper_rows_are_not_stamped_with_eod_state(monkeypatch):
    main.init_db()
    main.init_vol1d_journal_columns()
    _install_state(monkeypatch, _fake_state())
    tid = main.db_log_trade("SPY", "CALL", 1.5, mode="paper")
    row = _one("SELECT vol_state FROM trades WHERE id=?", (tid,))
    # Paper replay inserts at ~16:04 — stamping then would journal the
    # WRONG (EOD) regime as the entry regime.
    assert row == (None,)


def test_logging_works_without_state(monkeypatch):
    main.init_db()
    main.init_vol1d_journal_columns()
    _install_state(monkeypatch, None)
    main.db_log_signal({"symbol": "QQQ", "direction": "PUT", "price": 560.0})
    tid = main.db_log_trade("QQQ", "PUT", 2.0, mode="auto")
    assert tid is not None
    row = _one("SELECT vol_state FROM trades WHERE id=?", (tid,))
    assert row == (None,)
