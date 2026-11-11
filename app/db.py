"""SQLite access. One shared connection in WAL mode, writes serialized by a lock."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en-US',
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'live',
    recap_json TEXT,
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id TEXT NOT NULL REFERENCES shows(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    price_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    units_left INTEGER NOT NULL,
    image_path TEXT,
    status TEXT NOT NULL DEFAULT 'live',
    source_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sold_out_at TEXT,
    CHECK (units_left >= 0)
);
CREATE INDEX IF NOT EXISTS idx_drops_show ON drops(show_id, id);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_id INTEGER NOT NULL REFERENCES drops(id),
    paypal_order_id TEXT NOT NULL UNIQUE,
    authorization_id TEXT,
    capture_id TEXT,
    payer_name TEXT,
    payer_email TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_drop ON attempts(drop_id);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_id INTEGER NOT NULL REFERENCES drops(id),
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    buyer_name TEXT NOT NULL,
    buyer_email TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    refund_id TEXT,
    status TEXT NOT NULL DEFAULT 'paid',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_drop ON orders(drop_id);

CREATE TABLE IF NOT EXISTS paypal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    debug_id TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements in one IMMEDIATE transaction (serialized writer)."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
