# vol1d/features.py
# Derived features on top of the raw proxy level (spec §2):
#   exp_move_pts / exp_move_adj   1-day expected 1-sigma move in SPX points
#   rv_intraday                   Parkinson realized vol on 1-min spot bars
#   iv_rv_spread                  vix1d - rv_intraday (fade-vs-trend tilt)
#   vix1d_roc                     level change over a trailing window
#
# exp_move_adj is DELIBERATELY shrunk (rp_factor < 1): VIX1D overstates
# next-day realized by a persistent risk premium. iv_rv_spread carries a
# structural positive bias too (the index prices overnight variance the
# intraday RV never sees) — that bias is absorbed by the regime thresholds
# rather than corrected here; don't re-tune one without the other.

import math
from collections import deque

from vol1d import config as vol1d_config

_LN4 = 4.0 * math.log(2.0)


def exp_move(spot, vix1d, cfg=None):
    """(exp_move_pts, exp_move_adj) — spec: spot * (vix1d/100) / sqrt(252),
    then the risk-premium shrink."""
    if not spot or spot <= 0 or vix1d is None or vix1d <= 0:
        return None, None
    cfg = cfg or vol1d_config.get_config()
    pts = spot * (vix1d / 100.0) / math.sqrt(252.0)
    return round(pts, 2), round(pts * cfg["features"]["rp_factor"], 2)


def parkinson_vol(bars, ann_minutes=252 * 390):
    """Annualized Parkinson vol (%) from 1-minute {h, l} bars:
    per-bar variance ln(H/L)^2 / (4 ln 2), annualized on trading minutes.
    Returns None with fewer than 2 usable bars."""
    per_bar = []
    for b in bars:
        h, l = b.get("h"), b.get("l")
        if not h or not l or h <= 0 or l <= 0 or h < l:
            continue
        per_bar.append(math.log(h / l) ** 2 / _LN4)
    if len(per_bar) < 2:
        return None
    return round(100.0 * math.sqrt(sum(per_bar) / len(per_bar) * ann_minutes), 3)


class IntradayFeatures(object):
    """Session-scoped accumulator the updater thread owns. Feed it every
    pass (~15s); it maintains the 1-min spot bars for RV and the level
    history for ROC, and resets itself when the session date rolls."""

    def __init__(self, cfg=None):
        self.cfg = cfg or vol1d_config.get_config()
        window = self.cfg["features"]["rv_window_min"]
        self._bars = deque(maxlen=window)          # {"minute","h","l"}
        self._levels = deque()                      # (ts_et, vix1d)
        self._session = None

    def _roll_session(self, ts_et):
        day = ts_et.strftime("%Y-%m-%d")
        if day != self._session:
            self._session = day
            self._bars.clear()
            self._levels.clear()

    def update(self, ts_et, spot, vix1d):
        """Record one pass. Returns the feature dict for this instant."""
        self._roll_session(ts_et)

        if spot and spot > 0:
            minute = ts_et.replace(second=0, microsecond=0)
            if self._bars and self._bars[-1]["minute"] == minute:
                bar = self._bars[-1]
                bar["h"] = max(bar["h"], spot)
                bar["l"] = min(bar["l"], spot)
            else:
                self._bars.append({"minute": minute, "h": spot, "l": spot})

        roc = None
        if vix1d is not None:
            window_s = self.cfg["regime"]["roc_window_min"] * 60.0
            self._levels.append((ts_et, vix1d))
            # Trim history past the ROC window (keep one older anchor so
            # the lookback always spans the full window).
            while (len(self._levels) > 2
                   and (ts_et - self._levels[1][0]).total_seconds() >= window_s):
                self._levels.popleft()
            anchor_ts, anchor_level = self._levels[0]
            if (ts_et - anchor_ts).total_seconds() >= window_s * 0.5:
                roc = round(vix1d - anchor_level, 3)

        rv = self.rv_intraday()
        pts, adj = exp_move(spot, vix1d, self.cfg)
        spread = (round(vix1d - rv, 3)
                  if vix1d is not None and rv is not None else None)
        return {
            "exp_move_pts": pts,
            "exp_move_adj": adj,
            "rv_intraday":  rv,
            "iv_rv_spread": spread,
            "vix1d_roc":    roc,
        }

    def rv_intraday(self):
        """Parkinson RV over the trailing window, excluding the live
        (incomplete) minute bar."""
        bars = list(self._bars)[:-1]
        return parkinson_vol(bars)
