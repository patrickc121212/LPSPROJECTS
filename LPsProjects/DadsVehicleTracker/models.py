"""
SQLite persistence. Three tables:

  vehicle_state   — last known position, speed, battery, online flag
  door_state      — last known open/closed (mirrored from Google Home Graph)
  inbox           — messages, source of truth for in-app messaging
  allowlist       — per-door explicit allow of non-owner vehicle keys
  read_receipt    — when each driver last opened the inbox (for SMS fallback)

Schema is created on first import if missing.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH, GARAGE_DOORS, VEHICLES


SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_state (
    vehicle_key    TEXT PRIMARY KEY,
    latitude       REAL,
    longitude      REAL,
    speed_mph      REAL,
    battery_pct    INTEGER,
    online         INTEGER,
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS door_state (
    door_key       TEXT PRIMARY KEY,
    is_open        INTEGER,        -- 0 closed, 1 open, NULL unknown
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS inbox (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_key     TEXT NOT NULL,  -- vehicle key of sender
    recipient_key  TEXT NOT NULL,  -- vehicle key of recipient
    body           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    read_at        REAL,           -- NULL = unread
    sms_sent_at    REAL            -- NULL = not pushed over SMS (yet)
);

CREATE TABLE IF NOT EXISTS allowlist (
    door_key       TEXT NOT NULL,
    vehicle_key    TEXT NOT NULL,
    PRIMARY KEY (door_key, vehicle_key)
);

CREATE TABLE IF NOT EXISTS read_receipt (
    driver_key     TEXT PRIMARY KEY,
    last_seen_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_inbox_recipient ON inbox(recipient_key, read_at);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. Applied idempotently on startup so
# an existing data/tracker.db picks them up without a manual migration.
_MIGRATIONS = [
    ("inbox", "sms_sent_at", "ALTER TABLE inbox ADD COLUMN sms_sent_at REAL"),
]


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in _MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(ddl)
        # Seed door_state rows so SSE has a stable shape from t=0
        now = time.time()
        for door in GARAGE_DOORS:
            conn.execute(
                "INSERT OR IGNORE INTO door_state(door_key, is_open, updated_at) VALUES (?, NULL, ?)",
                (door.key, now),
            )
        for v in VEHICLES:
            conn.execute(
                "INSERT OR IGNORE INTO vehicle_state(vehicle_key, online, updated_at) VALUES (?, 0, ?)",
                (v.key, now),
            )


# --- Vehicle state ---------------------------------------------------------

def upsert_vehicle_state(
    vehicle_key: str,
    latitude: float | None,
    longitude: float | None,
    speed_mph: float | None,
    battery_pct: int | None,
    online: bool,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO vehicle_state(vehicle_key, latitude, longitude, speed_mph, battery_pct, online, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_key) DO UPDATE SET
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                speed_mph=excluded.speed_mph,
                battery_pct=excluded.battery_pct,
                online=excluded.online,
                updated_at=excluded.updated_at
            """,
            (vehicle_key, latitude, longitude, speed_mph, battery_pct, int(online), time.time()),
        )


def all_vehicle_states() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT vehicle_key, latitude, longitude, speed_mph, battery_pct, online, updated_at "
            "FROM vehicle_state"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Door state ------------------------------------------------------------

def upsert_door_state(door_key: str, is_open: bool | None) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO door_state(door_key, is_open, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(door_key) DO UPDATE SET
                is_open=excluded.is_open,
                updated_at=excluded.updated_at
            """,
            (door_key, None if is_open is None else int(is_open), time.time()),
        )


def all_door_states() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT door_key, is_open, updated_at FROM door_state"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Inbox -----------------------------------------------------------------

def add_message(sender_key: str, recipient_key: str, body: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO inbox(sender_key, recipient_key, body, created_at) VALUES (?, ?, ?, ?)",
            (sender_key, recipient_key, body, time.time()),
        )
        return cur.lastrowid


def list_inbox(recipient_key: str, limit: int = 100) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, sender_key, recipient_key, body, created_at, read_at, sms_sent_at "
            "FROM inbox WHERE recipient_key = ? ORDER BY id DESC LIMIT ?",
            (recipient_key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def unread_unpushed(recipient_key: str, older_than: float) -> list[dict]:
    """Unread messages created before `older_than` that haven't been pushed
    over SMS yet. Oldest first so the fallback sends in order."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, sender_key, recipient_key, body, created_at "
            "FROM inbox WHERE recipient_key = ? AND read_at IS NULL "
            "AND sms_sent_at IS NULL AND created_at < ? ORDER BY id ASC",
            (recipient_key, older_than),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_sms_sent(message_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE inbox SET sms_sent_at = ? WHERE id = ?", (time.time(), message_id))


def mark_read(recipient_key: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE inbox SET read_at = ? WHERE recipient_key = ? AND read_at IS NULL",
            (time.time(), recipient_key),
        )
        conn.execute(
            "INSERT INTO read_receipt(driver_key, last_seen_at) VALUES (?, ?) "
            "ON CONFLICT(driver_key) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (recipient_key, time.time()),
        )


def last_seen(driver_key: str) -> float | None:
    with db() as conn:
        row = conn.execute(
            "SELECT last_seen_at FROM read_receipt WHERE driver_key = ?", (driver_key,)
        ).fetchone()
    return None if row is None else row["last_seen_at"]


# --- Allowlist -------------------------------------------------------------

def get_allowlist(door_key: str) -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT vehicle_key FROM allowlist WHERE door_key = ?", (door_key,)
        ).fetchall()
    return {r["vehicle_key"] for r in rows}


def set_allowlist(door_key: str, vehicle_keys: list[str]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM allowlist WHERE door_key = ?", (door_key,))
        conn.executemany(
            "INSERT INTO allowlist(door_key, vehicle_key) VALUES (?, ?)",
            [(door_key, k) for k in vehicle_keys],
        )


def all_allowlists() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT door_key, vehicle_key FROM allowlist ORDER BY door_key, vehicle_key"
        ).fetchall()
    for r in rows:
        out.setdefault(r["door_key"], []).append(r["vehicle_key"])
    return out
