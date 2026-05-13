# plan_summary.py
# Composes the one-line trade plan for the morning Telegram brief.
#
# Lives in its own module so it can be unit-tested without importing all of
# main.py (which boots Flask + scheduler threads at import time).

def summarize_plan(regime, gex_bias, premarket):
    """
    One-sentence trade plan based on the morning's context.

    Resolves conflicts between regime label and GEX tape bias instead of
    emitting both. The previous version produced lines like
        'Range-bound day -- fade extremes only. ORB favored.'
    which is two opposite instructions on the same day. Real-time dealer
    positioning (GEX) wins ties when it conflicts with the trailing-vol
    regime classification.
    """
    r_name      = (regime or {}).get("regime", "")
    expansion   = (regime or {}).get("expansion_watch", False)
    flipped     = (regime or {}).get("intraday_flip", False)
    tape        = (gex_bias or {}).get("tape_bias", "")
    term        = (regime or {}).get("term_structure") or {}

    # Lean direction (fade vs trend) from each signal
    regime_lean = "neutral"
    if   r_name == "COMPRESSED" and not expansion: regime_lean = "fade"
    elif r_name == "ELEVATED":                     regime_lean = "trend"
    elif r_name == "CRISIS":                       regime_lean = "trend"

    gex_lean = "neutral"
    if   "TREND"       in tape: gex_lean = "trend"
    elif "MEAN_REVERT" in tape: gex_lean = "fade"

    parts = []

    if expansion:
        parts.append("EXPANSION_WATCH -- compressed vol + tight gap, breakout setup")
        parts.append("ORB and trend strategies favored")
    elif flipped:
        parts.append("Intraday regime flip -- trend strategies now active")
    elif regime_lean != "neutral" and gex_lean != "neutral" and regime_lean != gex_lean:
        # Real conflict. Defer to GEX (live positioning beats trailing vol).
        parts.append(
            "Mixed signals (regime: {}-leaning, dealers: {}-leaning) -- "
            "wait for IB confirmation, GEX takes priority post-10:00 ET"
            .format(regime_lean, gex_lean)
        )
    else:
        if r_name == "CRISIS":
            parts.append("Intraday only, half size, wide stops")
        elif r_name == "ELEVATED":
            parts.append("Trend strategies preferred")
        elif r_name == "COMPRESSED":
            parts.append("Range-bound day -- fade extremes only")
        else:
            parts.append("Standard regime")
        if gex_lean == "trend":
            parts.append("ORB favored")
        elif gex_lean == "fade":
            parts.append("VWAP fades favored")

    term_label = term.get("label")
    if term_label in ("DEEP_BACKWARDATION", "BACKWARDATION"):
        parts.append("VIX term backwardated -- bounce-prone")
    elif term_label == "DEEP_CONTANGO":
        parts.append("VIX term steep contango -- watch for vol spike")

    gap = (premarket or {}).get("gap", {})
    if gap.get("class") == "INSIDE_GAP":
        parts.append("morning fade likely")
    elif "GAP_AND_GO" in (gap.get("class") or ""):
        parts.append("continuation likely")

    return ". ".join(parts) + "."
