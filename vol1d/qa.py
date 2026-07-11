# vol1d/qa.py
# Proxy-vs-official reconciliation (spec §"qa").
#
# The official VIX1D print is CGIF-only on Databento (raw PCAP, ~$750/mo),
# so the official CLOSE comes from Cboe's free daily index CSV — the same
# cdn.cboe.com history files regime_filter already trusts for VIX/VIX9D/
# VIX3M. The fetch sits behind OfficialVix1dSource so it is NOT a hard
# dependency: a deployment can stub it out (reconcile_with_official=false)
# and the proxy still runs.
#
# Residuals are stored per-day; the regime layer downgrades confidence when
# the latest residual exceeds qa.residual_tolerance.

import csv
import io
from datetime import datetime, timezone

import requests

import db_utils
from vol1d import config as vol1d_config

_CBOE_VIX1D_URL = ("https://cdn.cboe.com/api/global/us_indices/"
                   "daily_prices/VIX1D_History.csv")
# A last row older than this many days means the history file silently
# froze (mirrors regime_filter's guard).
_MAX_STALE_DAYS = 6

_QA_DB = db_utils.data_path("vol1d_state.db")


class OfficialVix1dSource(object):
    """Interface: daily official VIX1D values, or None when unavailable."""

    def get_history(self):
        """{date_iso: close} for every session the source carries."""
        raise NotImplementedError

    def get_close(self, date_iso):
        return (self.get_history() or {}).get(date_iso)


class CboeCsvOfficialSource(OfficialVix1dSource):
    """Official daily closes from Cboe's free VIX1D history CSV."""

    def __init__(self, url=_CBOE_VIX1D_URL, timeout=15, session=None):
        self.url = url
        self.timeout = timeout
        self._session = session or requests
        self._cache = None   # one fetch per process run is plenty (daily data)

    def get_history(self):
        if self._cache is not None:
            return self._cache
        try:
            r = self._session.get(self.url, timeout=self.timeout)
        except Exception as e:
            print("[vol1d.qa] official VIX1D fetch failed: {}".format(e))
            return None
        if r.status_code != 200 or not r.text:
            print("[vol1d.qa] official VIX1D HTTP {}".format(r.status_code))
            return None
        hist = self.parse_csv(r.text)
        if hist:
            self._cache = hist
        return hist

    @staticmethod
    def parse_csv(text):
        """CSV header: DATE,OPEN,HIGH,LOW,CLOSE (DATE like MM/DD/YYYY).
        Returns {date_iso: close} or None on parse/staleness failure."""
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception:
            return None
        if not rows:
            return None
        out = {}
        date_key  = next((k for k in rows[-1] if k.strip().upper() == "DATE"), None)
        close_key = next((k for k in rows[-1] if k.strip().upper() == "CLOSE"), None)
        if not date_key or not close_key:
            return None
        for row in rows:
            try:
                d = datetime.strptime(row[date_key].strip(), "%m/%d/%Y").date()
                out[d.isoformat()] = float(row[close_key])
            except (ValueError, KeyError, TypeError):
                continue
        if not out:
            return None
        last = max(out)
        age = (datetime.now(timezone.utc).date()
               - datetime.strptime(last, "%Y-%m-%d").date()).days
        if age > _MAX_STALE_DAYS:
            print("[vol1d.qa] official VIX1D history stale (last={})".format(last))
            return None
        return out


class NullOfficialSource(OfficialVix1dSource):
    """Stub for deployments that skip reconciliation."""

    def get_history(self):
        return None


def _init_residual_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vol1d_residuals (
            session_date   TEXT PRIMARY KEY,
            proxy_close    REAL,
            official_close REAL,
            residual       REAL,
            stored_at      TEXT
        )
    """)


def save_residual(session_date, proxy_close, official_close, db_path=None):
    """Store the day's proxy-vs-official residual. Returns the residual."""
    residual = None
    if proxy_close is not None and official_close is not None:
        residual = round(proxy_close - official_close, 4)
    conn = db_utils.connect(db_path or _QA_DB)
    _init_residual_table(conn)
    conn.execute("""
        INSERT OR REPLACE INTO vol1d_residuals
        (session_date, proxy_close, official_close, residual, stored_at)
        VALUES (?, ?, ?, ?, ?)
    """, (session_date, proxy_close, official_close, residual,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return residual


def latest_residual(db_path=None):
    """(session_date, residual) of the newest reconcile, or (None, None)."""
    conn = db_utils.connect(db_path or _QA_DB)
    _init_residual_table(conn)
    row = conn.execute("""
        SELECT session_date, residual FROM vol1d_residuals
        WHERE residual IS NOT NULL
        ORDER BY session_date DESC LIMIT 1
    """).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def residual_within_tolerance(residual, cfg=None):
    """None (unknown) counts as within — QA absence must not kill the signal,
    it only skips the confidence bump."""
    if residual is None:
        return True
    cfg = cfg or vol1d_config.get_config()
    return abs(residual) <= cfg["qa"]["residual_tolerance"]


def reconcile_daily(session_date, proxy_close, source=None, cfg=None,
                    db_path=None):
    """Nightly job body: fetch the official close, store the residual, and
    return {official_close, residual, within_tolerance} (or None when the
    official source is unavailable/disabled)."""
    cfg = cfg or vol1d_config.get_config()
    if not cfg["qa"]["reconcile_with_official"]:
        return None
    source = source or CboeCsvOfficialSource()
    official = source.get_close(session_date)
    if official is None:
        print("[vol1d.qa] no official close for {}".format(session_date))
        return None
    residual = save_residual(session_date, proxy_close, official, db_path)
    ok = residual_within_tolerance(residual, cfg)
    print("[vol1d.qa] {} proxy={} official={} residual={} within_tol={}".format(
        session_date, proxy_close, official, residual, ok))
    return {"official_close": official, "residual": residual,
            "within_tolerance": ok}
