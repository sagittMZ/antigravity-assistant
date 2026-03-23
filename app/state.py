"""
state.py — SQLite-based persistent key-value store for bot state.

CHANGES vs original:
- Added PRAGMA journal_mode=WAL to prevent write locks under concurrent access
  (background_watcher + poll_for_response run simultaneously).
- Added async wrappers (get_val_async / set_val_async) via run_in_executor
  so async handlers never block the event loop with sync SQLite calls.
- check_same_thread=False is safe here because all sync calls go through
  run_in_executor (one thread per call, WAL handles concurrent reads).
"""
from __future__ import annotations

import asyncio
import sqlite3
import json
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent.parent / "data" / "bot_state.db"
DB_PATH.parent.mkdir(exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    # WAL mode: readers don't block writers and vice versa.
    # Critical when watcher and polling coroutines run concurrently.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Call once at bot startup."""
    with _get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv_store "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )


# --- Sync API (used by launcher and non-async contexts) ---

def get_val(key: str, default: Any = None) -> Any:
    with _get_conn() as conn:
        cur = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else default


def set_val(key: str, value: Any) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def clear_val(key: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))


# --- Async API (use these inside tg_bot.py / phone_worker.py) ---

async def get_val_async(key: str, default: Any = None) -> Any:
    """Non-blocking get — runs sync SQLite call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: get_val(key, default))


async def set_val_async(key: str, value: Any) -> None:
    """Non-blocking set — runs sync SQLite call in a thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: set_val(key, value))


async def clear_val_async(key: str) -> None:
    """Non-blocking delete."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: clear_val(key))
