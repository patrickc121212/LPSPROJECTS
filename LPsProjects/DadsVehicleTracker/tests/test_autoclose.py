"""Auto-close on departure, and the position sensor that makes it safe.

Auto-close is off by default; these tests turn it on explicitly so the
default path stays covered by the rest of the suite.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import time as _time

import config
import door_control
import geofence_worker as gw
import models
import shelly
from conftest import at, owned_by


@pytest.fixture
def auto_close(monkeypatch):
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 180)
    # Keep the belief alive across these scenarios; the TTL interaction has
    # its own tests below.
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    return config


def _advance(monkeypatch, seconds):
    """Jump the worker's clock forward. Capture the real function first —
    gw.time is the time module itself, so a lambda calling time.time()
    after patching would call itself."""
    frozen = _time.time() + seconds
    monkeypatch.setattr(gw.time, "time", lambda: frozen)


def _parked_at(door, offset_m=3, belt="Unlatched", gear="P"):
    """A snapshot of a car sitting at the garage, as the car reports it."""
    return {"dad": {**at(door, offset_m), "seatbelt": belt, "gear": gear}}


def _open_state(door, now, age=0.0):
    return {door.key: {"door_key": door.key, "is_open": 1, "updated_at": now - age}}


# --- timing -----------------------------------------------------------------

def test_no_close_until_the_delay_has_elapsed(auto_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    assert gw.departure_actions({"dad": at(d, 500)}, st, now)[0] == []
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 179)[0] == []
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 181)[0] == [d.key]


def test_away_timer_resets_when_the_owner_returns(auto_close):
    """Backing out and pulling straight back in must not bank away-time."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.departure_actions({"dad": at(d, 500)}, st, now)
    gw.departure_actions({"dad": at(d, 5)}, st, now + 100)     # came back
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 120)[0] == []
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 290)[0] == []
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 310)[0] == [d.key]


def test_only_one_close_attempt_per_departure(auto_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.departure_actions({"dad": at(d, 500)}, st, now)
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 200)[0] == [d.key]
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 400)[0] == []
    # Coming home and leaving again arms it once more.
    gw.departure_actions({"dad": at(d, 5)}, st, now + 500)
    gw.departure_actions({"dad": at(d, 500)}, st, now + 600)
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 900)[0] == [d.key]


def test_no_close_when_door_is_not_open(auto_close):
    d = owned_by("dad")
    now = 1_000_000.0
    shut = {d.key: {"door_key": d.key, "is_open": 0, "updated_at": now}}
    assert gw.departure_actions({"dad": at(d, 500)}, shut, now + 900)[0] == []


def test_no_close_without_a_gps_fix(auto_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    no_fix = {"dad": {"latitude": None, "longitude": None}}
    assert gw.departure_actions(no_fix, st, now + 900)[0] == []


def test_disabled_by_default_only_clears_the_belief():
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    to_close, to_assume = gw.departure_actions({"dad": at(d, 500)}, st, now)
    assert to_close == [] and to_assume == [d.key]


# --- sensor -----------------------------------------------------------------

def _sensor_door(raw_state, invert=False, monkeypatch=None):
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", sensor_input="0",
                sensor_invert=invert)
    monkeypatch.setattr(shelly, "get_input_state", lambda *a, **k: raw_state)
    return d


def test_sensor_contact_closed_means_door_closed(monkeypatch):
    d = _sensor_door(True, monkeypatch=monkeypatch)
    assert door_control.sensed_open(d, now=1.0) is False
    assert door_control.has_sensor(d) is True


def test_sensor_contact_open_means_door_open(monkeypatch):
    d = _sensor_door(False, monkeypatch=monkeypatch)
    assert door_control.sensed_open(d, now=1.0) is True


def test_sensor_invert_flips_the_reading(monkeypatch):
    d = _sensor_door(True, invert=True, monkeypatch=monkeypatch)
    assert door_control.sensed_open(d, now=1.0) is True


def test_no_sensor_configured_returns_none(monkeypatch):
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", sensor_input="")
    assert door_control.sensed_open(d, now=1.0) is None
    assert door_control.has_sensor(d) is False


def test_unreachable_sensor_returns_none(monkeypatch):
    d = _sensor_door(None, monkeypatch=monkeypatch)
    assert door_control.sensed_open(d, now=1.0) is None


def test_sensor_overrides_a_wrong_belief(monkeypatch):
    """The whole point of the sensor: truth beats memory."""
    d = _sensor_door(True, monkeypatch=monkeypatch)  # contact closed = shut
    now = 1_000_000.0
    believes_open = _open_state(d, now)
    assert door_control.is_open(d, believes_open, now) is False


def test_sensor_reading_is_cached_briefly(monkeypatch):
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", sensor_input="0")
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(shelly, "get_input_state", counting)
    door_control.sensed_open(d, now=100.0)
    door_control.sensed_open(d, now=100.5)
    assert calls["n"] == 1, "should not re-poll the relay within the cache window"
    door_control.sensed_open(d, now=100.0 + door_control.SENSOR_CACHE_S + 0.1)
    assert calls["n"] == 2


def test_sensor_stops_the_dangerous_close(monkeypatch, auto_close):
    """With a sensor saying the door is already shut, auto-close must not
    fire — this is exactly the case that would otherwise OPEN the door at
    an empty house."""
    d = _sensor_door(True, monkeypatch=monkeypatch)  # really closed
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    now = 1_000_000.0
    believes_open = _open_state(d, now)
    gw.departure_actions({"dad": at(d, 500)}, believes_open, now)
    assert gw.departure_actions({"dad": at(d, 500)}, believes_open, now + 900)[0] == []


def test_sensor_allows_the_close_when_really_open(monkeypatch, auto_close):
    d = _sensor_door(False, monkeypatch=monkeypatch)  # really open
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    now = 1_000_000.0
    shut_belief = {d.key: {"door_key": d.key, "is_open": 0, "updated_at": now}}
    gw.departure_actions({"dad": at(d, 500)}, shut_belief, now)      # away timer starts
    assert gw.departure_actions({"dad": at(d, 500)}, shut_belief, now + 900)[0] == [d.key]


# --- end to end through _tick ----------------------------------------------

def test_tick_pulses_once_on_departure(pulses, monkeypatch, auto_close):
    d = replace(owned_by("dad"), shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    models.upsert_door_state(d.key, True)          # we believe it's open
    models.upsert_vehicle_state("dad", at(d, 500)["latitude"], d.longitude, 30, 80, True)

    gw._tick()                                     # away timer starts
    assert pulses == []
    _advance(monkeypatch, 1000)
    gw._tick()                                     # delay elapsed -> close
    assert pulses == [("10.0.0.9", 0)]
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 0
    gw._tick()
    assert len(pulses) == 1, "must not keep pulsing"


def test_failed_auto_close_is_not_retried_but_is_logged(monkeypatch, auto_close, caplog):
    import logging
    d = replace(owned_by("dad"), shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(door_control, "actuate",
                        lambda door, action: {"ok": False, "via": "shelly", "detail": "timeout"})
    models.upsert_door_state(d.key, True)
    models.upsert_vehicle_state("dad", at(d, 500)["latitude"], d.longitude, 30, 80, True)
    caplog.set_level(logging.WARNING)
    gw._tick()
    _advance(monkeypatch, 1000)
    gw._tick()
    assert any("Auto-close" in r.message and "FAILED" in r.message for r in caplog.records)
    # The door is still believed open, so the next arrival is guarded.
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 1


# --- the TTL / delay interaction -------------------------------------------

def test_belief_expiring_before_the_delay_blocks_auto_close(monkeypatch):
    """Documents the trap: without a sensor the 'open' belief expires after
    DOOR_OPEN_TTL_S, so a longer close delay means the close never happens."""
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 900)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 600)
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.departure_actions({"dad": at(d, 500)}, st, now)
    assert gw.departure_actions({"dad": at(d, 500)}, st, now + 901)[0] == []


def test_startup_warns_when_the_delay_outlives_the_belief(monkeypatch):
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 900)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 600)
    warnings = door_control.config_warnings()
    assert any("never run" in w for w in warnings)


def test_startup_warns_about_sensorless_auto_close(monkeypatch):
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", sensor_input="")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    assert any("empty house" in w for w in door_control.config_warnings())


def test_no_warnings_when_a_sensor_is_fitted(monkeypatch):
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 180)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 600)
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", sensor_input="0")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    assert door_control.config_warnings() == []


# --- separate, tighter close fence -----------------------------------------

def test_close_radius_defaults_to_the_open_radius():
    d = owned_by("dad")
    assert config.close_radius(replace(d, close_radius_m=0)) == d.radius_m
    assert config.close_radius(replace(d, close_radius_m=40)) == 40.0


def test_departure_uses_the_close_fence_not_the_open_fence(auto_close, monkeypatch):
    """Leaving the tight close fence must arm auto-close even though the car
    is still well inside the big open fence."""
    d = replace(owned_by("dad"), radius_m=150.0, close_radius_m=40.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 5)
    now = 1_000_000.0
    st = _open_state(d, now)
    at_60m = at(d, 60)          # outside close fence, inside open fence
    gw.departure_actions({"dad": at_60m}, st, now)
    assert gw.departure_actions({"dad": at_60m}, st, now + 6)[0] == [d.key]


def test_still_in_the_driveway_does_not_arm_the_close(auto_close, monkeypatch):
    d = replace(owned_by("dad"), radius_m=150.0, close_radius_m=40.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 5)
    now = 1_000_000.0
    st = _open_state(d, now)
    at_20m = at(d, 20)          # still inside the close fence
    gw.departure_actions({"dad": at_20m}, st, now)
    assert gw.departure_actions({"dad": at_20m}, st, now + 600)[0] == []


# --- close after parking ----------------------------------------------------

@pytest.fixture
def parked_close(monkeypatch):
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_DWELL_S", 30)
    monkeypatch.setattr(config, "PARKED_RADIUS_M", 25.0)
    monkeypatch.setattr(config, "PARKED_JITTER_M", 8.0)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    return config


def test_stationary_clock_starts_and_runs(parked_close):
    d = owned_by("dad")
    p = at(d, 2)
    now = 1_000_000.0
    assert gw.stationary_seconds("dad", p["latitude"], p["longitude"], now) == 0.0
    assert gw.stationary_seconds("dad", p["latitude"], p["longitude"], now + 25) == 25.0


def test_real_movement_resets_the_stationary_clock(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    gw.stationary_seconds("dad", *_ll(at(d, 2)), now)
    assert gw.stationary_seconds("dad", *_ll(at(d, 2)), now + 20) == 20.0
    # Moved 30 m: that is the car, not GPS noise.
    assert gw.stationary_seconds("dad", *_ll(at(d, 32)), now + 25) == 0.0


def test_gps_jitter_does_not_reset_the_clock(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    gw.stationary_seconds("dad", *_ll(at(d, 2)), now)
    assert gw.stationary_seconds("dad", *_ll(at(d, 6)), now + 20) == 20.0


def test_parked_at_garage_closes_after_the_dwell(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    parked = _parked_at(d)
    assert gw.doors_to_close_after_parking(parked, st, now) == []
    assert gw.doors_to_close_after_parking(parked, st, now + 29) == []
    assert gw.doors_to_close_after_parking(parked, st, now + 31) == [d.key]
    # Once only.
    assert gw.doors_to_close_after_parking(parked, st, now + 60) == []


def test_idling_in_the_driveway_is_not_parked_at_the_garage(parked_close):
    """100 m out is inside the geofence but nowhere near the door; closing
    on someone about to drive away would be wrong."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    driveway = _parked_at(d, 100)
    gw.doors_to_close_after_parking(driveway, st, now)
    assert gw.doors_to_close_after_parking(driveway, st, now + 600) == []


def test_parked_close_skipped_when_door_is_not_open(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    shut = {d.key: {"door_key": d.key, "is_open": 0, "updated_at": now}}
    parked = _parked_at(d)
    gw.doors_to_close_after_parking(parked, shut, now)
    assert gw.doors_to_close_after_parking(parked, shut, now + 600) == []


def test_driving_away_rearms_parked_close(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    parked = _parked_at(d)
    gw.doors_to_close_after_parking(parked, st, now)
    assert gw.doors_to_close_after_parking(parked, st, now + 31) == [d.key]
    gw.doors_to_close_after_parking(_parked_at(d, 400, gear="D"), st, now + 60)   # left
    gw.doors_to_close_after_parking(parked, st, now + 120)               # back, clock restarts
    assert gw.doors_to_close_after_parking(parked, st, now + 160) == [d.key]


def test_parked_close_disabled_by_default(monkeypatch):
    d = owned_by("dad")
    models.upsert_door_state(d.key, True)
    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True)
    assert config.PARKED_CLOSE_ENABLED is False
    gw._tick()
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 1


def _ll(p):
    return p["latitude"], p["longitude"]


def test_zero_delay_closes_on_the_first_reading_outside(auto_close, monkeypatch):
    """AUTO_CLOSE_DELAY_S=0 must fire on the first position outside the close
    fence, not one tick later."""
    d = replace(owned_by("dad"), radius_m=150.0, close_radius_m=20.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 0)
    now = 1_000_000.0
    st = _open_state(d, now)
    assert gw.departure_actions({"dad": at(d, 25)}, st, now)[0] == [d.key]


def test_zero_delay_still_requires_leaving_the_close_fence(auto_close, monkeypatch):
    d = replace(owned_by("dad"), radius_m=150.0, close_radius_m=20.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 0)
    now = 1_000_000.0
    st = _open_state(d, now)
    assert gw.departure_actions({"dad": at(d, 15)}, st, now)[0] == []


def test_warns_when_the_parked_zone_overlaps_the_close_fence(monkeypatch):
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_RADIUS_M", 25.0)
    d = replace(owned_by("dad"), close_radius_m=20.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    assert any("both parked at the garage and departed" in w
               for w in door_control.config_warnings())


def test_no_overlap_warning_when_parked_zone_is_inside(monkeypatch):
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_RADIUS_M", 15.0)
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", False)
    d = replace(owned_by("dad"), close_radius_m=20.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    assert door_control.config_warnings() == []


# --- open when the driver buckles up ---------------------------------------

@pytest.fixture
def depart_open(monkeypatch):
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "DEPART_OPEN_RADIUS_M", 25.0)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    return config


def _belted(door, offset_m=3, belt="Latched"):
    p = at(door, offset_m)
    return {"dad": {**p, "seatbelt": belt}}


def _shut(door, now):
    return {door.key: {"door_key": door.key, "is_open": 0, "updated_at": now}}


def test_buckling_at_the_garage_opens_the_door(depart_open):
    d = owned_by("dad")
    now = 1_000_000.0
    assert gw.doors_to_open_for_departure(_belted(d), _shut(d, now), now) == [d.key]


def test_unbuckled_does_not_open(depart_open):
    d = owned_by("dad")
    now = 1_000_000.0
    assert gw.doors_to_open_for_departure(_belted(d, belt="Unlatched"), _shut(d, now), now) == []
    assert gw.doors_to_open_for_departure(_belted(d, belt=None), _shut(d, now), now) == []


def test_buckling_far_from_the_garage_does_nothing(depart_open):
    """Belted in on the motorway must not open the door at home."""
    d = owned_by("dad")
    now = 1_000_000.0
    assert gw.doors_to_open_for_departure(_belted(d, offset_m=400), _shut(d, now), now) == []


def test_already_open_door_is_not_re_pulsed(depart_open):
    d = owned_by("dad")
    now = 1_000_000.0
    open_now = _open_state(d, now)
    assert gw.doors_to_open_for_departure(_belted(d), open_now, now) == []


def test_one_open_per_buckle(depart_open):
    d = owned_by("dad")
    now = 1_000_000.0
    shut = _shut(d, now)
    assert gw.doors_to_open_for_departure(_belted(d), shut, now) == [d.key]
    assert gw.doors_to_open_for_departure(_belted(d), shut, now + 5) == []
    # Unbuckling re-arms it.
    gw.doors_to_open_for_departure(_belted(d, belt="Unlatched"), shut, now + 10)
    assert gw.doors_to_open_for_departure(_belted(d), shut, now + 20) == [d.key]


def test_buckled_suppresses_the_parked_close(depart_open, parked_close):
    """Otherwise the door would open on buckle, then the dwell timer would
    shut it again while the driver sits there belted in."""
    d = owned_by("dad")
    now = 1_000_000.0
    open_now = _open_state(d, now)
    belted = _parked_at(d, belt="Latched")
    gw.doors_to_close_after_parking(belted, open_now, now)
    assert gw.doors_to_close_after_parking(belted, open_now, now + 600) == []
    # Unbuckle and the dwell rule takes over again.
    unbuckled = _parked_at(d, belt="Unlatched")
    gw.doors_to_close_after_parking(unbuckled, open_now, now + 601)
    assert gw.doors_to_close_after_parking(unbuckled, open_now, now + 640) == [d.key]


def test_depart_open_disabled_by_default():
    d = owned_by("dad")
    models.upsert_door_state(d.key, False)
    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True, seatbelt="Latched")
    assert config.DEPART_OPEN_ENABLED is False
    gw._tick()
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 0


def test_tick_opens_on_buckle_end_to_end(pulses, monkeypatch, depart_open):
    d = replace(owned_by("dad"), shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    models.upsert_door_state(d.key, False)
    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True, seatbelt="Unlatched")
    gw._tick()
    assert pulses == []
    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True, seatbelt="Latched")
    gw._tick()
    assert pulses == [("10.0.0.9", 0)]
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 1
    gw._tick()
    assert len(pulses) == 1, "must not keep pulsing while belted"


# --- the bug: two pulses close together stop a moving door -------------------

def test_close_cannot_follow_an_open_within_the_cooldown(monkeypatch):
    """The real incident: buckling opened the door, then the parked-close
    rule fired four seconds later. An opener reads the second pulse as STOP,
    so the door halted half open."""
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_DWELL_S", 30)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    d = owned_by("dad")
    now = 1_000_000.0

    # Car has been parked for ages, so the dwell clock is long satisfied.
    parked = _parked_at(d)
    gw.doors_to_close_after_parking(parked, _shut(d, now), now - 5000)
    gw.doors_to_close_after_parking(parked, _shut(d, now), now)

    # Buckle up: the door opens.
    belted = _parked_at(d, belt="Latched")
    assert gw.doors_to_open_for_departure(belted, _shut(d, now), now) == [d.key]
    door_control.record_action(d.key, now)

    # Belt reads unlatched a moment later — which is what happened — so the
    # suppression lifts. The cooldown must still hold the close off.
    open_now = _open_state(d, now)
    assert gw.doors_to_close_after_parking(parked, open_now, now + 4) == []
    assert gw.doors_to_close_after_parking(parked, open_now, now + 30) == []


def test_departure_open_restarts_the_dwell_clock(monkeypatch):
    """Belt-open must reset the parked timer, so the close has to earn its
    30 seconds afresh rather than firing the instant suppression lifts."""
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    d = owned_by("dad")
    now = 1_000_000.0
    parked = _parked_at(d)
    gw.doors_to_close_after_parking(parked, _shut(d, now), now - 5000)
    assert gw.stationary_seconds("dad", *_ll(at(d, 3)), now) > config.PARKED_DWELL_S

    belted = _parked_at(d, belt="Latched")
    gw.doors_to_open_for_departure(belted, _shut(d, now), now)
    assert gw.stationary_seconds("dad", *_ll(at(d, 3)), now) == 0.0


def test_manual_press_also_holds_off_the_automatic_rules(auth, monkeypatch, pulses):
    """Someone opening the door by hand must not have it closed from under
    them seconds later."""
    monkeypatch.setattr(config, "AUTO_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CLOSE_DELAY_S", 0)
    d = replace(owned_by("dad"), shelly_host="10.0.0.9", close_radius_m=20.0)
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    auth.post("/api/door", json={"door_key": d.key, "action": "open"})
    assert len(pulses) == 1
    models.upsert_vehicle_state("dad", at(d, 500)["latitude"], d.longitude, 30, 80, True)
    gw._tick()
    assert len(pulses) == 1, "auto-close must respect a manual press"


def test_cooldown_expires():
    door_control.record_action("garage2", 1000.0)
    assert door_control.in_cooldown("garage2", 1000.0 + config.DOOR_ACTION_COOLDOWN_S - 1)
    assert not door_control.in_cooldown("garage2", 1000.0 + config.DOOR_ACTION_COOLDOWN_S + 1)


def test_cooldown_ignores_a_backwards_clock():
    """A clock that jumps back must not freeze every automatic rule."""
    door_control.record_action("garage2", 2_000_000.0)
    assert door_control.in_cooldown("garage2", 1_000_000.0) is False


def test_no_cooldown_for_a_door_never_commanded():
    assert door_control.in_cooldown("garage9", 1000.0) is False
    assert door_control.seconds_since_action("garage9", 1000.0) is None


# --- must not close while the driver is still manoeuvring --------------------

def test_reversing_in_is_not_parked(parked_close):
    """The incident: backing into the garage is a run of sub-10 m moves, so
    the cars report no new position at all and the old position-only check
    concluded the driver had finished. Gear says otherwise."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    reversing = _parked_at(d, gear="R")
    gw.doors_to_close_after_parking(reversing, st, now)
    assert gw.doors_to_close_after_parking(reversing, st, now + 600) == []


def test_creeping_forward_is_not_parked(parked_close):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    creeping = _parked_at(d, gear="D")
    gw.doors_to_close_after_parking(creeping, st, now)
    assert gw.doors_to_close_after_parking(creeping, st, now + 600) == []


def test_unknown_gear_does_not_close(parked_close):
    """A car that has never reported its gear must leave the rule off
    rather than guess."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    unknown = {"dad": {**at(d, 3), "seatbelt": "Unlatched"}}
    gw.doors_to_close_after_parking(unknown, st, now)
    assert gw.doors_to_close_after_parking(unknown, st, now + 600) == []


def test_dwell_starts_when_the_car_goes_into_park(parked_close):
    """Shifting to P begins the countdown; time spent manoeuvring does not
    count towards it."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    for t in range(0, 300, 30):
        gw.doors_to_close_after_parking(_parked_at(d, gear="R"), st, now + t)
    assert gw.doors_to_close_after_parking(_parked_at(d), st, now + 300) == []
    assert gw.doors_to_close_after_parking(_parked_at(d), st, now + 310) == []
    assert gw.doors_to_close_after_parking(_parked_at(d), st, now + 331) == [d.key]


def test_gear_is_persisted_from_telemetry(monkeypatch):
    import telemetry_worker as tw
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    tw._state.clear()
    tw.apply_signal("dad", "Location", {"latitude": 1.0, "longitude": 2.0})
    tw.apply_signal("dad", "Gear", "ShiftStateR")
    tw.flush()
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["gear"] == "R"
    tw._state.clear()


def test_gear_is_not_wiped_by_a_source_that_omits_it():
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True, gear="P")
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True)
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["gear"] == "P"
