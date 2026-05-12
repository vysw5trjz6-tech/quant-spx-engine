# db_utils.py
# Single entry point for sqlite3.connect() across the codebase.
#
# - Enables WAL journal mode + synchronous=NORMAL on first contact with each
#   DB file. WAL lets readers and writers proceed without blocking each other,
#   which matters because background scheduler threads and Flask request
#   threads all hit the same trade DB.
# - Sets a 5s busy_timeout so concurrent writers retry instead of failing.
# - Tracks which files have been initialized so the PRAGMAs only run once.

import sqlite3
import threading


_INITIALIZED = set()
_INIT_LOCK   = threading.Lock()


def connect(path, timeout=30):
    """Drop-in replacement for sqlite3.connect that applies WAL once per file."""
    conn = sqlite3.connect(path, timeout=timeout)

    with _INIT_LOCK:
        first_touch = path not in _INITIALIZED
        if first_touch:
            _INITIALIZED.add(path)

    if first_touch:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.DatabaseError:
            pass

    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        pass

    return conn
