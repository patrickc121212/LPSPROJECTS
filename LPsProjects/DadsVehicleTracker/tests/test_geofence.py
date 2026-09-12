"""Geofence auto-open: haversine, owner/allowlist permissions, edge-triggered
firing, debounce, and GPS-dropout handling."""
from __future__ import annotations

import config
import geofence_worker as gw
from conftest import at, door


def test_haversine_known_distance():
    # SF City Hall -> Ferry Building is ~2.9 km.
    d = gw.haversine_m(37.7793, -122.4193, 37.7955, -122.3937)
    assert 2800 < d < 2950


def test_haversine_zero():
    assert gw.haversine_m(37.0, -122.0, 37.0, -122.0) == 0.0


def test_owner_always_allowed():
    assert config.allowed_for_door("dad", "garage1", set()) is True
    assert config.allowed_for_door("lp", "garage2", set()) is True
    assert config.allowed_for_door("mom", "garage3", set()) is True


def test_non_owner_denied_without_allowlist():
    assert config.allowed_for_door("lp", "garage1", set()) is False
    assert config.allowed_for_door("mom", "garage1", set()) is False


def test_non_owner_allowed_when_listed():
    assert config.allowed_for_door("lp", "garage1", {"lp"}) is True
    assert config.allowed_for_door("mom", "garage1", {"lp"}) is False


def _eval(states, allow=None, now=1000.0):
    return gw.evaluate(states, allow or {}, now)


def test_first_observation_inside_is_baseline_not_fire():
    """Restarting the server with a car already parked inside must not
    open the door."""
    g1 = door("garage1")
    assert _eval({"dad": at(g1)}) == []


def test_fires_once_on_enter_then_stays_quiet():
    g1 = door("garage1")
    far = at(g1, offset_m=500)
    inside = at(g1, offset_m=10)
    assert _eval({"dad": far}, now=0) == []
    assert _eval({"dad": inside}, now=30) == [("dad", "garage1")]
    # Car sits in the garage for an hour: zero further fires.
    for t in range(60, 3600, 30):
        assert _eval({"dad": inside}, now=t) == []


def test_refire_after_leaving_and_returning():
    g1 = door("garage1")
    far, inside = at(g1, 500), at(g1, 10)
    _eval({"dad": far}, now=0)
    assert _eval({"dad": inside}, now=30) == [("dad", "garage1")]
    _eval({"dad": far}, now=1000)
    assert _eval({"dad": inside}, now=1030) == [("dad", "garage1")]


def test_debounce_blocks_edge_flapping():
    """GPS jitter bouncing the car across the fence line within the
    debounce window must not re-fire."""
    g1 = door("garage1")
    out, inside = at(g1, g1.radius_m + 5), at(g1, g1.radius_m - 5)
    _eval({"dad": out}, now=0)
    assert _eval({"dad": inside}, now=30) == [("dad", "garage1")]
    _eval({"dad": out}, now=60)
    assert _eval({"dad": inside}, now=90) == []  # within 120s debounce
    _eval({"dad": out}, now=200)
    assert _eval({"dad": inside}, now=230) == [("dad", "garage1")]


def test_non_owner_entering_does_not_fire_without_allowlist():
    g1 = door("garage1")
    _eval({"lp": at(g1, 500)}, now=0)
    assert _eval({"lp": at(g1, 10)}, now=30) == []


def test_allowlisted_non_owner_fires():
    g1 = door("garage1")
    allow = {"garage1": {"lp"}}
    _eval({"lp": at(g1, 500)}, allow, now=0)
    assert _eval({"lp": at(g1, 10)}, allow, now=30) == [("lp", "garage1")]


def test_gps_dropout_does_not_refire():
    """Position goes None (car asleep) then comes back inside: still inside,
    so no transition, so no fire."""
    g1 = door("garage1")
    _eval({"dad": at(g1, 500)}, now=0)
    assert _eval({"dad": at(g1, 10)}, now=30) == [("dad", "garage1")]
    _eval({"dad": {"latitude": None, "longitude": None}}, now=60)
    assert _eval({"dad": at(g1, 10)}, now=300) == []


def test_each_vehicle_only_opens_its_own_door_by_default():
    """All three cars drive home at once: exactly three fires, each owner
    to its own door."""
    states_far = {v.key: at(door(f"garage{i}"), 500) for i, v in enumerate(config.VEHICLES, 1)}
    states_home = {v.key: at(door(f"garage{i}"), 5) for i, v in enumerate(config.VEHICLES, 1)}
    _eval(states_far, now=0)
    fired = _eval(states_home, now=30)
    assert sorted(fired) == [("dad", "garage1"), ("lp", "garage2"), ("mom", "garage3")]


def test_tick_integrates_with_db_and_bus(fired, events):
    """End-to-end through SQLite: vehicle state in DB -> routine fired ->
    door state updated -> 'doors' event published."""
    import json
    import models
    g1 = door("garage1")
    far, inside = at(g1, 500), at(g1, 5)
    models.upsert_vehicle_state("dad", far["latitude"], far["longitude"], 30, 80, True)
    gw._tick()
    assert fired == []
    models.upsert_vehicle_state("dad", inside["latitude"], inside["longitude"], 5, 80, True)
    gw._tick()
    assert fired == [g1.routine_open]
    state = {d["door_key"]: d["is_open"] for d in models.all_door_states()}
    assert state["garage1"] == 1
    ev = json.loads(events.get_nowait())
    assert ev["event"] == "doors"
