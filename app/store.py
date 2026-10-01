"""
Spike Agent — SQLite persistence.
Stores runtime settings, trades, equity curve, spike events, signals,
logs and learned model weights. Everything survives restarts so the
agent keeps learning across cloud redeploys.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config

_lock = threading.RLock()   # reentrant: init() may be called while held
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,            -- drift | spike
    side TEXT NOT NULL,            -- MULTUP | MULTDOWN
    live INTEGER NOT NULL,         -- 0 paper, 1 live
    stake REAL,
    multiplier INTEGER,
    entry_px REAL,
    exit_px REAL,
    profit REAL,
    commission REAL DEFAULT 0,
    ticks_held INTEGER,
    reason TEXT,
    balance_after REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL,
    value REAL NOT NULL,
    live INTEGER NOT NULL,
    PRIMARY KEY (ts, live)
);
CREATE TABLE IF NOT EXISTS spikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    epoch REAL,
    age_ticks INTEGER,
    size REAL,
    direction TEXT,
    price REAL
);
CREATE INDEX IF NOT EXISTS idx_spikes_sym ON spikes(symbol, ts);
CREATE TABLE IF NOT EXISTS signals (
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    age_ticks INTEGER,
    p_fast REAL,
    p_slow REAL,
    hazard REAL,
    action TEXT,
    confidence REAL
);
CREATE TABLE IF NOT EXISTS model_state (
    symbol TEXT PRIMARY KEY,
    blob TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tick_cache (
    symbol TEXT NOT NULL,
    epoch REAL NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, epoch)
);
"""


def init(db_path: Optional[Path] = None) -> None:
    global _conn
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def _exec(sql: str, args: tuple = ()) -> None:
    conn = _c()                      # init outside the query lock
    with _lock:
        conn.execute(sql, args)
        conn.commit()


def _query(sql: str, args: tuple = ()) -> list[dict]:
    conn = _c()
    with _lock:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ---- settings -------------------------------------------------------------

def load_settings() -> dict:
    """Runtime settings merged over defaults (values JSON-decoded)."""
    out = dict(config.DEFAULT_SETTINGS)
    rows = _query("SELECT key, value FROM settings")
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def save_setting(key: str, value: Any) -> None:
    _exec("INSERT INTO settings(key, value) VALUES(?, ?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, json.dumps(value)))


def load_json(key: str, default: Any = None) -> Any:
    """Read one JSON value from the settings table (None when absent)."""
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value"])
    except Exception:
        return default


def save_json(key: str, value: Any) -> None:
    save_setting(key, value)


def last_model_save() -> float:
    """Timestamp of the most recent learned-model save (0 = never)."""
    rows = _query("SELECT MAX(updated) AS t FROM model_state")
    return float(rows[0]["t"] or 0.0) if rows else 0.0


# ---- trades / equity ------------------------------------------------------

def record_trade(symbol: str, mode: str, side: str, live: bool, stake: float,
                 multiplier: int, entry_px: float, exit_px: float, profit: float,
                 commission: float, ticks_held: int, reason: str,
                 balance_after: float) -> None:
    _exec(
        "INSERT INTO trades(ts, symbol, mode, side, live, stake, multiplier,"
        " entry_px, exit_px, profit, commission, ticks_held, reason, balance_after)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), symbol, mode, side, int(live), stake, multiplier,
         entry_px, exit_px, profit, commission, ticks_held, reason, balance_after),
    )


def get_trades(limit: int = 200, live: Optional[bool] = None) -> list[dict]:
    if live is None:
        rows = _query("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
    else:
        rows = _query("SELECT * FROM trades WHERE live=? ORDER BY ts DESC LIMIT ?",
                      (int(live), limit))
    return rows


def record_equity(value: float, live: bool) -> None:
    _exec("INSERT OR REPLACE INTO equity(ts, value, live) VALUES(?,?,?)",
          (time.time(), value, int(live)))


def get_equity(live: bool, limit: int = 1440) -> list[dict]:
    return _query("SELECT ts, value FROM equity WHERE live=? ORDER BY ts DESC LIMIT ?",
                  (int(live), limit))[::-1]


# ---- spikes / signals / logs ---------------------------------------------

def record_spike(symbol: str, epoch: float, age_ticks: int, size: float,
                 direction: str, price: float) -> None:
    _exec("INSERT INTO spikes(ts, symbol, epoch, age_ticks, size, direction, price)"
          " VALUES(?,?,?,?,?,?,?)",
          (time.time(), symbol, epoch, age_ticks, size, direction, price))


def get_spikes(symbol: Optional[str] = None, limit: int = 200) -> list[dict]:
    if symbol:
        return _query("SELECT * FROM spikes WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                      (symbol, limit))
    return _query("SELECT * FROM spikes ORDER BY ts DESC LIMIT ?", (limit,))


def record_signal(symbol: str, age: int, p_fast: float, p_slow: float,
                  hazard: float, action: str, confidence: float) -> None:
    _exec("INSERT INTO signals(ts, symbol, age_ticks, p_fast, p_slow, hazard,"
          " action, confidence) VALUES(?,?,?,?,?,?,?,?)",
          (time.time(), symbol, age, p_fast, p_slow, hazard, action, confidence))


def get_signals(limit: int = 100) -> list[dict]:
    return _query("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))


def log(level: str, message: str) -> None:
    try:
        _exec("INSERT INTO logs(ts, level, message) VALUES(?,?,?)",
              (time.time(), level, message))
    except Exception:
        pass


def get_logs(limit: int = 200) -> list[dict]:
    return _query("SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,))


# ---- model weights --------------------------------------------------------

def save_model(symbol: str, blob: dict) -> None:
    _exec("INSERT INTO model_state(symbol, blob, updated) VALUES(?,?,?) "
          "ON CONFLICT(symbol) DO UPDATE SET blob=excluded.blob, updated=excluded.updated",
          (symbol, json.dumps(blob), time.time()))


def load_model(symbol: str) -> Optional[dict]:
    rows = _query("SELECT blob FROM model_state WHERE symbol=?", (symbol,))
    if not rows:
        return None
    try:
        return json.loads(rows[0]["blob"])
    except Exception:
        return None


# ---- tick cache (warm-start after restarts) -------------------------------

def cache_ticks(symbol: str, ticks: list[tuple[float, float]]) -> None:
    conn = _c()
    with _lock:
        conn.executemany(
            "INSERT OR REPLACE INTO tick_cache(symbol, epoch, price) VALUES(?,?,?)",
            [(symbol, e, p) for e, p in ticks])
        conn.commit()


def load_cached_ticks(symbol: str, limit: int = 6000) -> list[tuple[float, float]]:
    rows = _query(
        "SELECT epoch, price FROM tick_cache WHERE symbol=? "
        "ORDER BY epoch DESC LIMIT ?", (symbol, limit))
    return [(r["epoch"], r["price"]) for r in rows][::-1]
