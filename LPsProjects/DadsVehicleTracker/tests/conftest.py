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

import config  # noqa: E402
import models  # noqa: E402
import geofence_worker  # noqa: E402
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
def fired(monkeypatch):
    """Capture routine triggers instead of hitting the webhook."""
    calls: list[str] = []
    monkeypatch.setattr(geofence_worker, "_trigger_routine", calls.append)
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
