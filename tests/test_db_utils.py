import os
import sqlite3

import db_utils


def test_connect_enables_wal(tmp_path):
    path = str(tmp_path / "wal_test.db")
    conn = db_utils.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_connect_is_idempotent(tmp_path):
    path = str(tmp_path / "idem.db")
    # First call sets PRAGMAs; second call should reuse and not error.
    c1 = db_utils.connect(path)
    c2 = db_utils.connect(path)
    c1.execute("CREATE TABLE IF NOT EXISTS t(x)")
    c2.execute("INSERT INTO t VALUES (1)")
    c2.commit()
    rows = c1.execute("SELECT x FROM t").fetchall()
    c1.close()
    c2.close()
    assert rows == [(1,)]


def test_connect_returns_sqlite_connection(tmp_path):
    path = str(tmp_path / "type.db")
    conn = db_utils.connect(path)
    assert isinstance(conn, sqlite3.Connection)
    conn.close()
