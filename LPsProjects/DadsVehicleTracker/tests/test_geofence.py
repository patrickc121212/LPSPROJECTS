"""Geofence auto-open: haversine, owner/allowlist permissions, edge-triggered
firing, debounce, and GPS-dropout handling."""
from __future__ import annotations

import config
import geofence_worker as gw
from conftest import at, owned_by, other_driver


def test_haversine_known_distance():
    # SF City Hall -> Ferry Building is ~2.9 km.
    d = gw.haversine_m(37.7793, -122.4193, 37.7955, -122.3937)
    assert 2800 < d < 2950


def test_haversine_zero():
    assert gw.haversine_m(37.0, -122.0, 37.0, -122.0) == 0.0


def test_physical_layout_matches_house():
    """1 = Rosie (Mom), 2 = middle / AeroTitan (Dad), 3 = Model 3 (LP)."""
    assert {d.key: d.owner_key for d in config.GARAGE_DOORS} == {"garage1": "mom", "garage2": "dad", "garage3": "lp"}


def test_owner_always_allowed():
    for v in config.VEHICLES:
        assert config.allowed_for_door(v.key, owned_by(v.key).key, set()) is True


def test_non_owner_denied_without_allowlist():
    for d in config.GARAGE_DOORS:
        for v in config.VEHICLES:
            if v.key != d.owner_key:
                assert config.allowed_for_door(v.key, d.key, set()) is False, (v.key, d.key)


def test_non_owner_allowed_when_listed():
    d = owned_by("dad")
    guest = other_driver(d)
    third = next(v.key for v in config.VEHICLES if v.key not in (d.owner_key, guest))
    assert config.allowed_for_door(guest, d.key, {guest}) is True
    assert config.allowed_for_door(third, d.key, {guest}) is False


def _eval(states, allow=None, now=1000.0):
    return gw.evaluate(states, allow or {}, now)


def test_first_observation_inside_is_baseline_not_fire():
    """Restarting the server with a car already parked inside must not
    open the door."""
    g1 = owned_by("dad")
    assert _eval({"dad": at(g1)}) == []


def test_fires_once_on_enter_then_stays_quiet():
    g1 = owned_by("dad")
    far = at(g1, offset_m=500)
    inside = at(g1, offset_m=10)
    assert _eval({"dad": far}, now=0) == []
    assert _eval({"dad": inside}, now=30) == [("dad", g1.key)]
    # Car sits in the garage for an hour: zero further fires.
    for t in range(60, 3600, 30):
        assert _eval({"dad": inside}, now=t) == []


def test_refire_after_leaving_and_returning():
    g1 = owned_by("dad")
    far, inside = at(g1, 500), at(g1, 10)
    _eval({"dad": far}, now=0)
    assert _eval({"dad": inside}, now=30) == [("dad", g1.key)]
    _eval({"dad": far}, now=1000)
    assert _eval({"dad": inside}, now=1030) == [("dad", g1.key)]


def test_debounce_blocks_edge_flapping():
    """GPS jitter bouncing the car across the fence line within the
    debounce window must not re-fire."""
    g1 = owned_by("dad")
    out, inside = at(g1, g1.radius_m + 5), at(g1, g1.radius_m - 5)
    _eval({"dad": out}, now=0)
    assert _eval({"dad": inside}, now=30) == [("dad", g1.key)]
    _eval({"dad": out}, now=60)
    assert _eval({"dad": inside}, now=90) == []  # within 120s debounce
    _eval({"dad": out}, now=200)
    assert _eval({"dad": inside}, now=230) == [("dad", g1.key)]


def test_non_owner_entering_does_not_fire_without_allowlist():
    """A guest arriving at Dad's door never opens Dad's door. (It may open
    its OWN door if the bays are within one fence of each other — that's
    correct — so we only assert about Dad's.)"""
    g1 = owned_by("dad")
    guest = other_driver(g1)
    _eval({guest: at(g1, 500)}, now=0)
    fired = _eval({guest: at(g1, 10)}, now=30)
    assert (guest, g1.key) not in fired


def test_allowlisted_non_owner_fires():
    g1 = owned_by("dad")
    guest = other_driver(g1)
    allow = {g1.key: {guest}}
    _eval({guest: at(g1, 500)}, allow, now=0)
    fired = _eval({guest: at(g1, 10)}, allow, now=30)
    assert (guest, g1.key) in fired


def test_gps_dropout_does_not_refire():
    """Position goes None (car asleep) then comes back inside: still inside,
    so no transition, so no fire."""
    g1 = owned_by("dad")
    _eval({"dad": at(g1, 500)}, now=0)
    assert _eval({"dad": at(g1, 10)}, now=30) == [("dad", g1.key)]
    _eval({"dad": {"latitude": None, "longitude": None}}, now=60)
    assert _eval({"dad": at(g1, 10)}, now=300) == []


def test_each_vehicle_only_opens_its_own_door_by_default():
    """All three cars drive home at once: exactly three fires, each owner
    to its own door."""
    states_far = {v.key: at(owned_by(v.key), 500) for v in config.VEHICLES}
    states_home = {v.key: at(owned_by(v.key), 5) for v in config.VEHICLES}
    _eval(states_far, now=0)
    fired = _eval(states_home, now=30)
    assert sorted(fired) == sorted((v.key, owned_by(v.key).key) for v in config.VEHICLES)


def test_tick_integrates_with_db_and_bus(fired, events):
    """End-to-end through SQLite: vehicle state in DB -> routine fired ->
    door state updated -> 'doors' event published."""
    import json
    import models
    g1 = owned_by("dad")
    far, inside = at(g1, 500), at(g1, 5)
    models.upsert_vehicle_state("dad", far["latitude"], far["longitude"], 30, 80, True)
    gw._tick()
    assert fired == []
    models.upsert_vehicle_state("dad", inside["latitude"], inside["longitude"], 5, 80, True)
    gw._tick()
    assert fired == [g1.routine_open]
    state = {d["door_key"]: d["is_open"] for d in models.all_door_states()}
    assert state[g1.key] == 1
    ev = json.loads(events.get_nowait())
    assert ev["event"] == "doors"
