"""Event bus semantics, Tesla poller simulator + backoff, DB migration."""
from __future__ import annotations

import json
import queue
import sqlite3
import time

import pytest

import eventbus
import models
import tesla_poller as tp
from eventbus import EventBus


# --- Event bus -------------------------------------------------------------

def test_bus_fans_out_to_all_subscribers():
    b = EventBus()
    _, q1 = b.subscribe()
    _, q2 = b.subscribe()
    b.publish("x", {"a": 1})
    for q in (q1, q2):
        msg = json.loads(q.get_nowait())
        assert msg["event"] == "x" and msg["data"] == {"a": 1} and "ts" in msg


def test_bus_unsubscribe():
    b = EventBus()
    sid, q = b.subscribe()
    b.unsubscribe(sid)
    b.publish("x", 1)
    assert q.empty()
    assert b.subscriber_count() == 0


def test_bus_slow_subscriber_drops_event_but_stays_subscribed(monkeypatch):
    """A browser that falls behind loses individual events, not its stream."""
    monkeypatch.setattr(eventbus, "MAXSIZE", 2)
    b = EventBus()
    sid, q = b.subscribe()
    for i in range(5):
        b.publish("x", i)
    assert b.subscriber_count() == 1
    got = []
    while True:
        try:
            got.append(json.loads(q.get_nowait())["data"])
        except queue.Empty:
            break
    assert got == [0, 1]
    # Drain and the subscriber picks up again.
    b.publish("x", 99)
    assert json.loads(q.get_nowait())["data"] == 99


# --- Tesla poller ----------------------------------------------------------

def test_simulator_produces_all_vehicles_with_fixes():
    tp._init_sim()
    states = tp._step_sim()
    assert {s["vehicle_key"] for s in states} == {"dad", "lp", "mom"}
    for s in states:
        assert s["latitude"] is not None and s["longitude"] is not None
        assert 0 <= s["battery_pct"] <= 100
        assert s["online"] is True


def test_simulator_parks_each_car_at_its_own_garage():
    import config
    tp._init_sim()
    for v in config.VEHICLES:
        own = next(g for g in config.GARAGE_DOORS if g.owner_key == v.key)
        assert tp._sim_state[v.key]["lat"] == own.latitude
        assert tp._sim_state[v.key]["lon"] == own.longitude


def test_simulator_round_trip_leaves_and_returns_home():
    """The demo only works if the cars actually exit the fence and come
    back; walk enough ticks to see one full cycle for every car."""
    import config
    import geofence_worker as gw
    tp._init_sim()
    g = {v.key: next(d for d in config.GARAGE_DOORS if d.owner_key == v.key) for v in config.VEHICLES}
    left, returned = set(), set()
    for _ in range(400):
        for s in tp._step_sim():
            d = g[s["vehicle_key"]]
            dist = gw.haversine_m(s["latitude"], s["longitude"], d.latitude, d.longitude)
            if dist > d.radius_m:
                left.add(s["vehicle_key"])
            elif s["vehicle_key"] in left:
                returned.add(s["vehicle_key"])
    assert left == returned == {"dad", "lp", "mom"}


def test_simulator_return_triggers_geofence_once_per_trip(fired, monkeypatch):
    """Drive the real worker off the simulator through SQLite for many
    trips: every arrival home fires exactly one auto-open, and a car
    parked at home never re-fires."""
    import time as _time
    import config
    import geofence_worker as gw
    from collections import Counter

    tp._init_sim()
    base = _time.time()
    returns: Counter = Counter()
    for i in range(400):
        before = {k: st["phase"] for k, st in tp._sim_state.items()}
        tp._publish(tp._step_sim())
        for k, st in tp._sim_state.items():
            if before[k] != "parked_home" and st["phase"] == "parked_home":
                returns[k] += 1
        monkeypatch.setattr(gw.time, "time", lambda: base + i * config.TESLA_POLL_INTERVAL_S)
        gw._tick()

    assert all(returns[v.key] >= 5 for v in config.VEHICLES), returns
    fires = Counter(fired)
    for v in config.VEHICLES:
        door = next(d for d in config.GARAGE_DOORS if d.owner_key == v.key)
        assert fires[door.routine_open] == returns[v.key], (v.key, fires, returns)


def test_publish_writes_db_and_emits_event(events):
    tp._init_sim()
    tp._publish(tp._step_sim())
    rows = {r["vehicle_key"]: r for r in models.all_vehicle_states()}
    assert rows["dad"]["online"] == 1 and rows["dad"]["latitude"] is not None
    assert json.loads(events.get_nowait())["event"] == "vehicles"


def test_backoff_grows_and_caps():
    b = 1.0
    seq = []
    for _ in range(6):
        b = tp.next_backoff(b, rate_limited=False)
        seq.append(b)
    assert seq == [2.0, 4.0, 8.0, 16.0, 16.0, 16.0]


def test_rate_limit_backs_off_harder():
    assert tp.next_backoff(1.0, rate_limited=True) == 4.0
    assert tp.next_backoff(4.0, rate_limited=True) == 16.0
    assert tp.next_backoff(16.0, rate_limited=True) == 16.0


def test_fetch_real_maps_fleet_response(monkeypatch):
    """Drive the async fetch with a fake SDK to check field mapping and
    that a RateLimited error is surfaced to the loop."""
    import asyncio
    import config
    import tesla_fleet_api.exceptions as ex

    class FakeFleet:
        def __init__(self, vin): self.vin = vin
        async def vehicle_data(self, endpoints):
            assert "location_data" in endpoints
            if self.vin == "VIN_RL":
                raise ex.RateLimited({})
            if self.vin == "VIN_OFF":
                raise ex.VehicleOffline({})
            return {"response": {
                "state": "online",
                "drive_state": {"latitude": 1.5, "longitude": 2.5, "speed": 100},
                "charge_state": {"battery_level": 77},
            }}

    class FakeVehicles:
        def createFleet(self, vin): return FakeFleet(vin)

    class FakeApi:
        def __init__(self, session, access_token, region):
            assert access_token == "tok" and region == "na"
            self.vehicles = FakeVehicles()

    import tesla_fleet_api
    monkeypatch.setattr(tesla_fleet_api, "TeslaFleetApi", FakeApi)

    from dataclasses import replace
    fake_vehicles = [
        replace(config.VEHICLES[0], tesla_vin="VIN_OK"),
        replace(config.VEHICLES[1], tesla_vin="VIN_OFF"),
        replace(config.VEHICLES[2], tesla_vin=""),  # unpaired -> skipped
    ]
    monkeypatch.setattr(config, "VEHICLES", fake_vehicles)

    out = asyncio.run(tp._fetch_real_async("tok", "na"))
    assert out[0]["vehicle_key"] == "dad"
    assert out[0]["latitude"] == 1.5 and out[0]["battery_pct"] == 77
    assert out[0]["speed_mph"] == 100.0  # Fleet API speed is already mph
    assert out[0]["online"] is True
    assert out[1] == {"vehicle_key": "lp", "online": False}
    assert len(out) == 2

    monkeypatch.setattr(config, "VEHICLES", [replace(config.VEHICLES[0], tesla_vin="VIN_RL")])
    import pytest
    with pytest.raises(tp.RateLimitedError):
        asyncio.run(tp._fetch_real_async("tok", "na"))


# --- DB migration ----------------------------------------------------------

def test_init_db_adds_sms_sent_at_to_legacy_schema(tmp_path, monkeypatch):
    """A tracker.db from before the sms_sent_at column must be upgraded in
    place rather than crash on first query."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript("""
        CREATE TABLE inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sender_key TEXT NOT NULL,
            recipient_key TEXT NOT NULL, body TEXT NOT NULL,
            created_at REAL NOT NULL, read_at REAL);
        INSERT INTO inbox(sender_key, recipient_key, body, created_at) VALUES ('dad','lp','old',1.0);
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(models, "DB_PATH", str(legacy))
    models.init_db()
    rows = models.list_inbox("lp")
    assert rows[0]["body"] == "old" and rows[0]["sms_sent_at"] is None
    models.init_db()  # idempotent


# --- the worker threads actually run ---------------------------------------
# These exist because a missing config attribute once killed the geofence
# thread at startup while every _tick()-level test still passed: nothing
# executed _loop(), so the crash was invisible until the live log was read.

@pytest.fixture
def geofence_thread():
    """Run the real worker loop and always shut it down afterwards."""
    import geofence_worker as gw
    gw._stop.clear()
    gw._wake.clear()
    yield gw
    gw.stop()
    time.sleep(0.15)
    gw._stop.clear()


def test_geofence_worker_thread_survives_startup(caplog, geofence_thread):
    import logging
    import threading
    gw = geofence_thread
    caplog.set_level(logging.INFO)
    before = {t.name for t in threading.enumerate()}
    gw.start_background()
    deadline = time.time() + 5
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name == "geofence-worker" and t.is_alive()]
        if alive and any("Geofence worker started" in r.message for r in caplog.records):
            break
        time.sleep(0.05)
    alive = [t for t in threading.enumerate() if t.name == "geofence-worker" and t.is_alive()]
    assert alive, "geofence worker thread died on startup"
    assert any("Geofence worker started" in r.message for r in caplog.records)
    assert "geofence-worker" not in before


def test_geofence_loop_reacts_to_request_tick(monkeypatch, geofence_thread):
    """The loop must wake on request_tick rather than sleeping out its
    heartbeat — that wait was the bulk of the auto-open delay."""
    import threading
    import config
    gw = geofence_thread
    ticks = threading.Semaphore(0)
    monkeypatch.setattr(gw, "_tick", lambda: ticks.release())
    monkeypatch.setattr(config, "GEOFENCE_INTERVAL_S", 3600)  # heartbeat must not save us
    gw._wake.clear()
    threading.Thread(target=gw._loop, name="geofence-test", daemon=True).start()
    assert ticks.acquire(timeout=2), "no initial tick"
    gw.request_tick()
    assert ticks.acquire(timeout=2), "loop did not wake on request_tick"


def test_every_config_attribute_the_workers_use_exists():
    """Cheap guard against the exact failure above: name the settings the
    background loops read, and fail loudly if one goes missing."""
    import config
    for name in ("GEOFENCE_INTERVAL_S", "GEOFENCE_DEBOUNCE_S", "TESLA_POLL_INTERVAL_S",
                 "SMS_FALLBACK_AFTER_S", "VEHICLE_SOURCE", "TESLA_REGION", "DB_PATH"):
        assert hasattr(config, name), f"config.{name} is missing"
