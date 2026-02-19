import sqlite3
from datetime import datetime

DB_NAME = "trades.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            mode TEXT,
            bias TEXT,
            entry REAL,
            stop REAL,
            target REAL,
            probability INTEGER,
            vol_regime TEXT,
            outcome TEXT,
            r_multiple REAL
        )
    """)

    conn.commit()
    conn.close()

def log_trade(ticker, mode, bias, entry, stop, target, probability, vol_regime):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        INSERT INTO trades 
        (timestamp, ticker, mode, bias, entry, stop, target, probability, vol_regime, outcome, r_multiple)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        ticker,
        mode,
        bias,
        entry,
        stop,
        target,
        probability,
        vol_regime,
        None,
        None
    ))

    conn.commit()
    conn.close()
