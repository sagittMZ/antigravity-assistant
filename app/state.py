"""
state.py — SQLite-based persistent key-value store for bot state.
"""
import sqlite3
import json
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent.parent / "data" / "bot_state.db"
DB_PATH.parent.mkdir(exist_ok=True)

def _get_conn():
    return sqlite3.connect(DB_PATH, isolation_level=None)

def init_db():
    with _get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT)")

def get_val(key: str, default: Any = None) -> Any:
    with _get_conn() as conn:
        cur = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else default

def set_val(key: str, value: Any):
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)", 
            (key, json.dumps(value))
        )

def clear_val(key: str):
    with _get_conn() as conn:
        conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))