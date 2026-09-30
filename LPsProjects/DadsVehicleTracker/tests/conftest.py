"""
Shared fixtures. Every test gets a fresh SQLite file in a temp dir and a
Flask app with the background workers disabled, so nothing here touches
Tesla, Twilio, or Google.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before config is imported (it reads env at import time).
os.environ.setdefault("TRACKER_SIMULATE", "1")
os.environ.setdefault("GEOFENCE_DEBOUNCE_S", "120")
os.environ.setdefault("SMS_FALLBACK_AFTER_S", "300")
os.environ["APP_USERNAME"] = "family"
os.environ["APP_PASSWORD"] = "testpw"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "adminpw"

# Pin everything the behaviour depends on. config.py calls load_dotenv(),
# which does NOT override values already in os.environ, so setting them here
# keeps the suite deterministic no matter what the live .env says — turning
# auto-close on in production must not change test outcomes.
for _k, _v in {
    "AUTO_CLOSE_ENABLED": "0",
    "AUTO_CLOSE_DELAY_S": "180",
    "PARKED_CLOSE_ENABLED": "0",
    "PARKED_DWELL_S": "30",
    "PARKED_RADIUS_M": "25",
    "PARKED_JITTER_M": "8",
    "DEPART_OPEN_ENABLED": "0",
    "DEPART_OPEN_RADIUS_M": "25",
    "DEPART_GRACE_S": "600",
    "DOOR_ACTION_COOLDOWN_S": "60",
    "TRIP_IDLE_END_S": "180",
    "TRIP_MIN_DISTANCE_MI": "0.2",
    "HISTORY_MIN_MOVE_M": "10",
    "HISTORY_RETENTION_DAYS": "365",
    "DOOR_OPEN_TTL_S": "600",
    "GEOFENCE_INTERVAL_S": "30",
    "TESLA_POLL_INTERVAL_S": "30",
    "VEHICLE_SOURCE": "sim",
    "GOOGLE_ROUTINE_WEBHOOK_URL": "",
}.items():
    os.environ[_k] = _v
for _n in ("1", "2", "3"):
    os.environ[f"GARAGE{_n}_RADIUS_M"] = "75"
    os.environ[f"GARAGE{_n}_CLOSE_RADIUS_M"] = ""
    os.environ[f"GARAGE{_n}_SHELLY_HOST"] = ""
    os.environ[f"GARAGE{_n}_SENSOR_INPUT"] = ""
    os.environ[f"GARAGE{_n}_SHELLY_MAC"] = ""

import config  # noqa: E402
import models  # noqa: E402
import door_control  # noqa: E402
import geofence_worker  # noqa: E402
import shelly  # noqa: E402
import sms  # noqa: E402
from eventbus import bus  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "tracker.db"
    monkeypatch.setattr(models, "DB_PATH", str(db_file))
    models.init_db()
    # Reset in-memory worker state between tests.
    geofence_worker._last_fire.clear()
    geofence_worker._inside.clear()
    geofence_worker._away_since.clear()
    geofence_worker._auto_closed.clear()
    geofence_worker._still_ref.clear()
    geofence_worker._parked_closed.clear()
    geofence_worker._depart_opened.clear()
    geofence_worker._departing.clear()
    import trips as _trips
    _trips._last_point.clear()
    _trips._last_prune = 0.0
    door_control._sensor_cache.clear()
    door_control._last_action.clear()
    shelly._located.clear()
    yield db_file


@pytest.fixture
def app():
    from app import create_app
    a = create_app(start_workers=False)
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth(client):
    client.post("/login", data={"username": "family", "password": "testpw"})
    return client


@pytest.fixture
def admin(client):
    """Signed in with the higher-privilege credential (trip history)."""
    client.post("/login", data={"username": "admin", "password": "adminpw"})
    return client


@pytest.fixture
def fired(monkeypatch):
    """Capture door actuations instead of pulsing a relay or hitting a webhook.

    Records the routine name for parity with the pre-Shelly tests.
    """
    calls: list[str] = []

    def fake(door, action):
        calls.append(door.routine_open if action == "open" else door.routine_close)
        return {"ok": True, "via": "test", "detail": "captured"}

    monkeypatch.setattr(door_control, "actuate", fake)
    return calls


@pytest.fixture
def pulses(monkeypatch):
    """Capture raw Shelly pulses: list of (host, channel)."""
    calls: list[tuple[str, int]] = []

    def fake(host, channel=0, pulse_s=0.5, timeout=4.0):
        calls.append((host, channel))
        return True, "pulsed"

    monkeypatch.setattr(shelly, "pulse", fake)
    return calls


@pytest.fixture
def sms_out(monkeypatch):
    """Capture outbound SMS instead of calling Twilio."""
    calls: list[tuple[str, str]] = []

    def fake(to, body):
        calls.append((to, body))
        return True

    monkeypatch.setattr(sms, "send_sms", fake)
    return calls


@pytest.fixture
def events():
    """Subscribe to the event bus; yields the queue; unsubscribes after."""
    sid, q = bus.subscribe()
    yield q
    bus.unsubscribe(sid)


def door(key: str) -> config.GarageDoor:
    return config.GARAGE_DOORS_BY_KEY[key]


def owned_by(vehicle_key: str) -> config.GarageDoor:
    """The door this vehicle owns — tests shouldn't encode the house layout."""
    return next(d for d in config.GARAGE_DOORS if d.owner_key == vehicle_key)


def other_driver(door_: config.GarageDoor) -> str:
    """Some vehicle key that does NOT own the door."""
    return next(v.key for v in config.VEHICLES if v.key != door_.owner_key)


def at(d: config.GarageDoor, offset_m: float = 0.0) -> dict:
    """A vehicle state positioned `offset_m` metres north of the door."""
    return {"latitude": d.latitude + offset_m / 111_320.0, "longitude": d.longitude}
