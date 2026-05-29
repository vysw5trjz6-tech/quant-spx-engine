"""Unified scanner orchestration + structured rationale.

Two responsibilities, both pure (no import of main, so no circular dependency):

  * build_rationale(sig)  -- turn a raw signal dict into a structured,
    human-readable rationale (stored as rationale_json, surfaced on cards,
    Telegram and the chat context).

  * merge_and_rank(...) / rank_secondary_universe(...) -- combine the intraday
    and weekly passes into one tiered payload, deduped by (symbol, horizon) and
    ranked by a conviction-adjusted score.

The scanner is tiered on two axes the caller sets:
  horizon       : "WEEKLY" (primary) | "INTRADAY" (secondary, SPY/QQQ only)
  product_class : "ETF" (SPY/QQQ) | "STOCK" (weekly universe).  "INDEX"
                  (SPX/NDX) is context only and never produces a signal dict.
"""


def _verdict_for_rs(rs):
    if rs is None:
        return "n/a"
    if rs >= 1.0:
        return "leading"
    if rs <= -1.0:
        return "lagging"
    return "inline"


def conviction_adjusted_score(sig):
    """Ranking key: raw probability/grade scaled by the conviction weight.

    Leaves the raw prob/grade untouched (the AI optimizer must stay
    dollar-free and conviction-free); this is only used for ordering.
    """
    base = sig.get("prob")
    if base is None:
        base = sig.get("grade_pts") or sig.get("score") or 0
    conv = sig.get("conviction")
    if conv is None:
        conv = 1.0
    try:
        return float(base) * float(conv)
    except (TypeError, ValueError):
        return 0.0


def build_rationale(sig):
    """Return a structured rationale dict for a signal.

    Never raises -- on bad input it returns a minimal dict so callers can store
    it unconditionally.
    """
    try:
        direction = sig.get("direction")
        horizon   = sig.get("horizon")
        stype     = sig.get("signal_type")
        rs        = sig.get("rs", sig.get("spy_rs"))

        confluence = []
        for t in (sig.get("all_types") or []):
            if t and t != stype:
                confluence.append(t)
        notes = sig.get("notes")
        if notes:
            confluence.append(notes)
        # Intraday confluence: pull itemized factors out of the grade breakdown.
        bd = sig.get("grade_breakdown") or {}
        for k, v in (bd.get("base_components") or {}).items():
            if v:
                confluence.append("{}:{}".format(k, v))

        expected_move = None
        if sig.get("expected_move") is not None or sig.get("atm_iv") is not None:
            expected_move = {
                "one_sd": sig.get("expected_move"),
                "iv":     sig.get("atm_iv"),
                "dte":    sig.get("dte"),
            }

        targets = {
            "t1":      sig.get("t1"),
            "t1_prob": sig.get("t1_prob"),
            "t2":      sig.get("t2"),
            "t2_prob": sig.get("t2_prob"),
            "stop":    sig.get("stop"),
            "basis":   sig.get("target_basis"),
        }

        key_levels = {
            "swing_low":  sig.get("swing_low"),
            "swing_high": sig.get("swing_high"),
            "pivot":      sig.get("pct_from_pivot"),
            "near_fib":   sig.get("near_fib_val"),
        }

        prob = sig.get("prob")
        if prob is None:
            prob = sig.get("grade_pts") or sig.get("score")

        bits = []
        bits.append("{} {} on {}".format(
            stype or "signal", direction or "?", sig.get("symbol", "?")))
        if horizon == "WEEKLY" and sig.get("week_expiry"):
            bits.append("wk exp {} ({}DTE)".format(
                sig.get("week_expiry"), sig.get("dte")))
        if sig.get("t1") is not None:
            tp = sig.get("t1_prob")
            bits.append("T1 {}{}".format(
                sig["t1"], " ({}%)".format(tp) if tp is not None else ""))
        if rs is not None:
            bits.append("RS {} ({})".format(rs, _verdict_for_rs(rs)))
        if expected_move and expected_move.get("one_sd"):
            bits.append("1σ ±{}".format(expected_move["one_sd"]))
        summary = " | ".join(bits)

        return {
            "signal_type":  stype,
            "horizon":      horizon,
            "direction":    direction,
            "confluence":   confluence,
            "rs":           {"value": rs, "verdict": _verdict_for_rs(rs)},
            "expected_move": expected_move,
            "key_levels":   key_levels,
            "targets":      targets,
            "probability":  prob,
            "conviction":   sig.get("conviction"),
            "summary":      summary,
        }
    except Exception:
        return {"summary": "", "signal_type": sig.get("signal_type")}


def rank_secondary_universe(weekly_signals):
    """STOCK-class weekly signals ranked by relative strength (descending)."""
    stocks = [s for s in (weekly_signals or [])
              if s.get("product_class") == "STOCK"]
    return sorted(stocks, key=lambda s: -(s.get("rs") or s.get("spy_rs") or 0))


def merge_and_rank(intraday_signals=None, weekly_signals=None, context=None):
    """Combine the two passes into one tiered payload.

    Dedupes by (symbol, horizon) -- on Fridays the weekly SPY/QQQ expiry equals
    the intraday 0DTE expiry, so an ETF can appear in both passes; we keep both
    tiers but never duplicate within a tier. Each tier is sorted by the
    conviction-adjusted score (then raw probability).
    """
    intraday_signals = intraday_signals or []
    weekly_signals   = weekly_signals or []

    def _dedupe(rows):
        seen = {}
        for r in rows:
            key = (r.get("symbol"), r.get("horizon"))
            if key not in seen:
                seen[key] = r
        return list(seen.values())

    weekly   = _dedupe(weekly_signals)
    intraday = _dedupe(intraday_signals)

    def _rank(rows):
        return sorted(rows, key=lambda s: (-conviction_adjusted_score(s),
                                           -(s.get("prob") or 0)))

    return {
        "weekly":   _rank(weekly),
        "intraday": _rank(intraday),
        "context":  context or {},
    }
