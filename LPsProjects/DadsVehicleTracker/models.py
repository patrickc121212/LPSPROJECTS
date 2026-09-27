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
    updated_at     REAL,
    seatbelt       TEXT,           -- "Latched" / "Unlatched" / NULL if unknown
    gear           TEXT            -- "P" / "R" / "N" / "D" / NULL if unknown
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

CREATE TABLE IF NOT EXISTS charge_session (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_key    TEXT NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL,            -- NULL while charging
    start_pct      INTEGER,
    end_pct        INTEGER,
    kwh            REAL,            -- energy delivered, filled in when it ends
    cost           REAL,
    at_home        INTEGER,         -- 1 home, 0 away, NULL unknown
    peak_kw        REAL,
    source         TEXT,            -- how kwh was derived: lifetime | counter
    -- running values kept so a restart mid-session loses nothing
    lifetime_start REAL,
    lifetime_last  REAL,
    counter_last   REAL,
    kwh_dc         REAL          -- energy that reached the battery, vs kwh from the wall
);

CREATE TABLE IF NOT EXISTS trip (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_key    TEXT NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL,            -- NULL while driving
    start_lat      REAL, start_lon REAL,
    end_lat        REAL, end_lon   REAL,
    distance_mi    REAL DEFAULT 0,
    max_speed_mph  REAL DEFAULT 0,
    start_pct      INTEGER, end_pct INTEGER,
    odo_start      REAL, odo_end   REAL,
    source         TEXT,            -- how distance was measured: odometer | gps
    last_lat       REAL, last_lon  REAL,   -- running point for GPS distance
    last_moved_at  REAL             -- last time it was actually moving
);

CREATE TABLE IF NOT EXISTS position_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_key    TEXT NOT NULL,
    ts             REAL NOT NULL,
    latitude       REAL NOT NULL,
    longitude      REAL NOT NULL,
    speed_mph      REAL,
    battery_pct    INTEGER,
    trip_id        INTEGER
);

CREATE TABLE IF NOT EXISTS vehicle_alert (
    vehicle_key    TEXT NOT NULL,
    name           TEXT NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL,            -- NULL while the alert is active
    audiences      TEXT,
    seen_at        REAL,
    PRIMARY KEY (vehicle_key, name, started_at)
);

CREATE INDEX IF NOT EXISTS idx_alert_active ON vehicle_alert(ended_at, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_vehicle ON trip(vehicle_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_open ON trip(vehicle_key, ended_at);
CREATE INDEX IF NOT EXISTS idx_pos_vehicle_ts ON position_history(vehicle_key, ts);
CREATE INDEX IF NOT EXISTS idx_pos_trip ON position_history(trip_id, ts);
CREATE INDEX IF NOT EXISTS idx_inbox_recipient ON inbox(recipient_key, read_at);
CREATE INDEX IF NOT EXISTS idx_charge_vehicle ON charge_session(vehicle_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_charge_open ON charge_session(vehicle_key, ended_at);
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
    ("vehicle_state", "seatbelt", "ALTER TABLE vehicle_state ADD COLUMN seatbelt TEXT"),
    ("vehicle_state", "gear", "ALTER TABLE vehicle_state ADD COLUMN gear TEXT"),
    ("charge_session", "kwh_dc", "ALTER TABLE charge_session ADD COLUMN kwh_dc REAL"),
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
    seatbelt: str | None = None,
    gear: str | None = None,
) -> None:
    """seatbelt=None leaves any previously stored value alone, so a source
    that doesn't report it (the poller) can't wipe what telemetry knows."""
    with db() as conn:
        conn.execute(
            """
            INSERT INTO vehicle_state(vehicle_key, latitude, longitude, speed_mph, battery_pct, online, updated_at, seatbelt, gear)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_key) DO UPDATE SET
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                speed_mph=excluded.speed_mph,
                battery_pct=excluded.battery_pct,
                online=excluded.online,
                updated_at=excluded.updated_at,
                seatbelt=COALESCE(excluded.seatbelt, vehicle_state.seatbelt),
                gear=COALESCE(excluded.gear, vehicle_state.gear)
            """,
            (vehicle_key, latitude, longitude, speed_mph, battery_pct, int(online), time.time(),
             seatbelt, gear),
        )


def all_vehicle_states() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT vehicle_key, latitude, longitude, speed_mph, battery_pct, online, updated_at, "
            "seatbelt, gear FROM vehicle_state"
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


# --- Vehicle alerts ---------------------------------------------------------

def upsert_alert(vehicle_key: str, name: str, started_at: float,
                 ended_at: float | None, audiences: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO vehicle_alert(vehicle_key, name, started_at, ended_at, audiences, seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(vehicle_key, name, started_at) DO UPDATE SET
                ended_at=COALESCE(excluded.ended_at, vehicle_alert.ended_at),
                audiences=excluded.audiences,
                seen_at=excluded.seen_at
            """,
            (vehicle_key, name, started_at, ended_at, audiences, time.time()),
        )


def get_alert(vehicle_key: str, name: str, started_at: float) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_alert WHERE vehicle_key = ? AND name = ? AND started_at = ?",
            (vehicle_key, name, started_at)).fetchone()
    return None if row is None else dict(row)


def list_alerts(active_only: bool = True, vehicle_key: str | None = None,
                limit: int = 200) -> list[dict]:
    q = "SELECT * FROM vehicle_alert WHERE 1=1"
    args: list = []
    if active_only:
        q += " AND ended_at IS NULL"
    if vehicle_key:
        q += " AND vehicle_key = ?"
        args.append(vehicle_key)
    q += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


# --- Trips and position history --------------------------------------------

def add_position(vehicle_key: str, ts: float, lat: float, lon: float,
                 speed_mph: float | None, battery_pct: int | None,
                 trip_id: int | None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO position_history(vehicle_key, ts, latitude, longitude, "
            "speed_mph, battery_pct, trip_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (vehicle_key, ts, lat, lon, speed_mph, battery_pct, trip_id),
        )


def open_trip(vehicle_key: str, started_at: float, lat: float, lon: float,
              battery_pct: int | None, odometer: float | None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO trip(vehicle_key, started_at, start_lat, start_lon, "
            "last_lat, last_lon, start_pct, odo_start, last_moved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vehicle_key, started_at, lat, lon, lat, lon, battery_pct, odometer, started_at),
        )
        return cur.lastrowid


def get_open_trip(vehicle_key: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM trip WHERE vehicle_key = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (vehicle_key,)
        ).fetchone()
    return None if row is None else dict(row)


def update_trip(trip_id: int, **fields) -> None:
    allowed = {"end_lat", "end_lon", "distance_mi", "max_speed_mph", "end_pct",
               "odo_end", "last_lat", "last_lon", "last_moved_at"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    with db() as conn:
        conn.execute(f"UPDATE trip SET {clause} WHERE id = ?", (*sets.values(), trip_id))


def close_trip(trip_id: int, ended_at: float, distance_mi: float | None,
               source: str | None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE trip SET ended_at = ?, distance_mi = ?, source = ? WHERE id = ?",
            (ended_at, distance_mi, source, trip_id),
        )


def delete_trip(trip_id: int) -> None:
    """Used to discard trips too short to be worth keeping."""
    with db() as conn:
        conn.execute("UPDATE position_history SET trip_id = NULL WHERE trip_id = ?", (trip_id,))
        conn.execute("DELETE FROM trip WHERE id = ?", (trip_id,))


def list_trips(limit: int = 100, vehicle_key: str | None = None,
               since: float | None = None) -> list[dict]:
    q = "SELECT * FROM trip WHERE 1=1"
    args: list = []
    if vehicle_key:
        q += " AND vehicle_key = ?"
        args.append(vehicle_key)
    if since is not None:
        q += " AND started_at >= ?"
        args.append(since)
    q += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_trip(trip_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM trip WHERE id = ?", (trip_id,)).fetchone()
    return None if row is None else dict(row)


def trip_path(trip_id: int, limit: int = 5000) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, latitude, longitude, speed_mph, battery_pct FROM position_history "
            "WHERE trip_id = ? ORDER BY ts LIMIT ?", (trip_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def positions_between(vehicle_key: str, start: float, end: float,
                      limit: int = 5000) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, latitude, longitude, speed_mph, battery_pct FROM position_history "
            "WHERE vehicle_key = ? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT ?",
            (vehicle_key, start, end, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def trip_totals_by_month() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT vehicle_key, strftime('%Y-%m', started_at, 'unixepoch', 'localtime') AS month, "
            "COUNT(*) AS trips, SUM(distance_mi) AS miles, "
            "SUM(ended_at - started_at) AS seconds "
            "FROM trip WHERE ended_at IS NOT NULL "
            "GROUP BY vehicle_key, month ORDER BY month DESC, vehicle_key"
        ).fetchall()
    return [dict(r) for r in rows]


def prune_history(older_than: float) -> int:
    """Drop position points older than a cutoff. Trips themselves are kept —
    they are small, and losing the summary loses more than the breadcrumbs."""
    with db() as conn:
        cur = conn.execute("DELETE FROM position_history WHERE ts < ?", (older_than,))
        return cur.rowcount


# --- Charging sessions -----------------------------------------------------

def open_charge_session(vehicle_key: str, started_at: float, start_pct: int | None,
                        at_home: bool | None, lifetime_start: float | None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO charge_session(vehicle_key, started_at, start_pct, at_home, "
            "lifetime_start, lifetime_last, peak_kw) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (vehicle_key, started_at, start_pct,
             None if at_home is None else int(at_home), lifetime_start, lifetime_start),
        )
        return cur.lastrowid


def get_open_charge_session(vehicle_key: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM charge_session WHERE vehicle_key = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (vehicle_key,)
        ).fetchone()
    return None if row is None else dict(row)


def update_charge_session(session_id: int, **fields) -> None:
    """Update only the columns given; unknown keys are ignored deliberately
    so a caller can pass a partial snapshot."""
    allowed = {"end_pct", "peak_kw", "lifetime_last", "counter_last", "kwh_dc"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    with db() as conn:
        conn.execute(f"UPDATE charge_session SET {clause} WHERE id = ?",
                     (*sets.values(), session_id))


def close_charge_session(session_id: int, ended_at: float, kwh: float | None,
                         cost: float | None, source: str | None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE charge_session SET ended_at = ?, kwh = ?, cost = ?, source = ? WHERE id = ?",
            (ended_at, kwh, cost, source, session_id),
        )


def list_charge_sessions(limit: int = 100, vehicle_key: str | None = None) -> list[dict]:
    q = "SELECT * FROM charge_session"
    args: list = []
    if vehicle_key:
        q += " WHERE vehicle_key = ?"
        args.append(vehicle_key)
    q += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def charge_totals_by_month() -> list[dict]:
    """Completed sessions aggregated per month per vehicle."""
    with db() as conn:
        rows = conn.execute(
            "SELECT vehicle_key, strftime('%Y-%m', started_at, 'unixepoch', 'localtime') AS month, "
            "COUNT(*) AS sessions, SUM(kwh) AS kwh, SUM(cost) AS cost "
            "FROM charge_session WHERE ended_at IS NOT NULL AND kwh IS NOT NULL "
            "GROUP BY vehicle_key, month ORDER BY month DESC, vehicle_key"
        ).fetchall()
    return [dict(r) for r in rows]


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
