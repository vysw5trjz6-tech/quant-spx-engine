# vol1d/chain_source.py
# Chain snapshot sources for the VIX1D proxy, behind one small interface so
# the proxy math never knows where quotes came from.
#
# Live v1 source: CBOE's free delayed-quotes JSON (same cdn.cboe.com the
# regime filter and index_data already trust first). It carries bid/ask for
# the full SPX + SPXW chain plus the spot level, ~15 minutes delayed —
# fine for the detrended regime read; only vix1d_roc sees the delay. The
# existing Databento path CANNOT feed this proxy: its `statistics` chain is
# EOD OI + settlement only, with no bids.
#
# A snapshot is a plain dict (repo style):
#   {
#     "ts":    datetime (ET, naive),
#     "spot":  float,          # underlying index level
#     "quotes": [ {"root","expiry" (date),"type" ("call"/"put"),
#                  "strike" (float), "bid" (float), "ask" (float)}, ... ],
#     "source": "cboe_delayed",
#   }
# Zero/absent bids are preserved as 0.0 — the proxy's strike-selection rule
# (drop zero-bid, stop after two consecutive) needs to SEE them.

import json
import re
from datetime import datetime

import pytz
import requests

from vol1d import config as vol1d_config

ET = pytz.timezone("America/New_York")

# OSI-style tail on CBOE option symbols: ROOT + YYMMDD + C/P + 8-digit
# strike in 1/1000ths (e.g. "SPXW260711P06250000"). Same layout
# databento_adapter parses for OPRA raw symbols.
_OSI_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse_option_symbol(sym):
    """Parse an OSI-style option symbol into
    {root, expiry (date), type, strike} or None."""
    m = _OSI_RE.match(str(sym).strip().replace(" ", ""))
    if not m:
        return None
    root, yy, mm, dd, cp, k = m.groups()
    try:
        expiry = datetime(2000 + int(yy), int(mm), int(dd)).date()
    except ValueError:
        return None
    return {
        "root":   root,
        "expiry": expiry,
        "type":   "call" if cp == "C" else "put",
        "strike": int(k) / 1000.0,
    }


class ChainSource(object):
    """Interface: returns a snapshot dict or None on failure."""

    def get_snapshot(self):
        raise NotImplementedError


class CboeDelayedChainSource(ChainSource):
    """Free key-less delayed SPX/SPXW chain from cdn.cboe.com."""

    def __init__(self, url=None, timeout=None, roots=None, session=None):
        cfg = vol1d_config.get_config()
        self.url     = url or cfg["chain_source"]["cboe_url"]
        self.timeout = timeout or cfg["chain_source"]["timeout_secs"]
        # Snapshot-level root set (union of every consumer's needs). The
        # proxy narrows to proxy.roots itself; gex_live to gex_live.roots.
        self.roots   = set(roots if roots is not None
                           else cfg["chain_source"]["roots"])
        self._session = session or requests

    def get_snapshot(self):
        try:
            r = self._session.get(self.url, timeout=self.timeout)
        except Exception as e:
            print("[vol1d] CBOE chain fetch failed: {}".format(e))
            return None
        if r.status_code != 200 or not r.text:
            print("[vol1d] CBOE chain HTTP {}".format(r.status_code))
            return None
        try:
            payload = r.json()
        except (ValueError, json.JSONDecodeError):
            print("[vol1d] CBOE chain: bad JSON")
            return None
        return self.parse_payload(payload)

    def parse_payload(self, payload):
        """Pure parse of the CBOE delayed-quotes payload — offline-testable."""
        data = (payload or {}).get("data") or {}
        spot = data.get("current_price") or data.get("close")
        try:
            spot = float(spot)
        except (TypeError, ValueError):
            return None
        if spot <= 0:
            return None

        quotes = []
        for o in data.get("options") or []:
            parsed = parse_option_symbol(o.get("option", ""))
            if not parsed or parsed["root"] not in self.roots:
                continue
            try:
                bid = float(o.get("bid") or 0.0)
                ask = float(o.get("ask") or 0.0)
            except (TypeError, ValueError):
                continue
            if bid < 0 or ask < 0:
                continue
            parsed["bid"] = bid
            parsed["ask"] = ask
            # OI + IV ride along for the intraday GEX consumer; absent or
            # unparsable values degrade to 0/None rather than dropping the
            # quote (the proxy still needs its bid/ask).
            try:
                parsed["open_interest"] = int(o.get("open_interest") or 0)
            except (TypeError, ValueError):
                parsed["open_interest"] = 0
            try:
                iv = o.get("iv")
                parsed["iv"] = float(iv) if iv is not None else None
            except (TypeError, ValueError):
                parsed["iv"] = None
            quotes.append(parsed)

        if not quotes:
            return None
        return {
            "ts":     datetime.now(ET).replace(tzinfo=None),
            "spot":   spot,
            "quotes": quotes,
            "source": "cboe_delayed",
        }
