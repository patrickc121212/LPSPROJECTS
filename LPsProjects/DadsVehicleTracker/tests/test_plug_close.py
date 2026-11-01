"""Plugging the car back in closes its door, even if it never left.

The case this exists for: buckle up at the garage, the door opens, then
change your mind, get out and plug back in. DEPART_GRACE_S is 600 s, so
without this rule the door stands open for ten minutes with the car sitting
in the garage -- the grace is there to stop a brief unbuckle being read as
"finished for the day", and it cannot tell that case from this one.

Connecting the cable can. It says "finished" with no ambiguity, so it
cancels the departure outright.

Edge-triggered throughout: the moment the cable goes in, never the state of
being plugged in. A level check would close the door every time you opened
it to fetch something with the car sitting on the charger.
"""
from __future__ import annotations

import pytest

import config
import door_control
import geofence_worker as gw
import models
from conftest import at, owned_by

from test_autoclose import _ll, _open_state, _parked_at


@pytest.fixture
def plug_closes(monkeypatch):
    monkeypatch.setattr(config, "PLUG_IN_CLOSES_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_DWELL_S", 30)
    monkeypatch.setattr(config, "PARKED_RADIUS_M", 25.0)
    monkeypatch.setattr(config, "PARKED_JITTER_M", 8.0)
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "DEPART_OPEN_RADIUS_M", 25.0)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 3600)
    return config


def _plug(door, charge, belt="Unlatched", gear="P", offset_m=3):
    """A snapshot of dad's car at the garage with a given charge state."""
    return {"dad": {**at(door, offset_m), "seatbelt": belt, "gear": gear,
                    "charge_state": charge}}


# --- the behaviour asked for ------------------------------------------------

def test_plugging_in_closes_the_door(plug_closes):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now) == []
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), st, now + 5) == [d.key]


def test_plugging_in_beats_the_departure_grace(plug_closes):
    """The car never left, so the grace is still running. This is the point."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    # Buckle up at the garage: the door opens and the grace starts.
    assert gw.doors_to_open_for_departure(
        _plug(d, "Disconnected", belt="Latched"), {}, now) == [d.key]
    assert gw._departing[d.key] == now
    # Change of mind: unbuckle, get out, plug back in.
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now + 10)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), st, now + 20) == [d.key]
    # Cancelled, not merely bypassed, so nothing re-applies it later.
    assert d.key not in gw._departing
    assert config.DEPART_GRACE_S > 20, "otherwise this test proves nothing"


def test_the_edge_closes_sooner_than_the_parked_rule_would(plug_closes):
    """Two mechanisms now close this door; the edge is the prompt one.

    The grace bypass further down lets the parked rule close it too, but
    that rule still waits out PARKED_DWELL_S. Plugging in is proof the car
    has stopped, so the edge skips the dwell and closes at once -- the
    difference between shutting as you walk away and shutting half a minute
    later.
    """
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_open_for_departure(
        _plug(d, "Disconnected", belt="Latched"), {}, now)
    unplugged, plugged = _plug(d, "Disconnected"), _plug(d, "Charging")

    # Start the parked rule's dwell clock, then plug in one second later.
    gw.doors_to_close_after_parking(unplugged, st, now + 9)
    gw.doors_to_close_on_plug_in(unplugged, st, now + 9)
    assert gw.doors_to_close_after_parking(plugged, st, now + 10) == [], \
        "the dwell has not elapsed, so the parked rule is not ready"
    assert gw.doors_to_close_on_plug_in(plugged, st, now + 10) == [d.key], \
        "the edge closes it anyway"


# --- it must be the moment, not the state -----------------------------------

def test_merely_being_plugged_in_never_closes_a_door(plug_closes):
    """A level check would shut the door on you every time you opened it to
    fetch something, with the car sitting plugged in."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    plugged = _plug(d, "Charging")
    assert gw.doors_to_close_on_plug_in(plugged, st, now) == []
    for i in range(1, 6):
        assert gw.doors_to_close_on_plug_in(plugged, st, now + i * 30) == []


def test_a_restart_with_the_car_already_plugged_in_does_not_close(plug_closes):
    """_plugged_prev is empty after a restart. If "no baseline" counted as an
    edge, every restart would pulse the door -- and the app restarts often."""
    d = owned_by("dad")
    now = 1_000_000.0
    assert gw._plugged_prev == {}
    assert gw.doors_to_close_on_plug_in(
        _plug(d, "Charging"), _open_state(d, now), now) == []


def test_unplugging_rearms_it(plug_closes):
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), st, now + 10) == [d.key]
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now + 20)
    gw._parked_closed.discard(d.key)          # door opened again later
    later = _open_state(d, now + 4000)
    assert gw.doors_to_close_on_plug_in(
        _plug(d, "Charging"), later, now + 4000) == [d.key]


# --- the safety conditions it still honours ---------------------------------

def test_buckled_suppresses_it(plug_closes):
    """Plugged in but still belted is contradictory, and the departure rule
    may be about to open this very door."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected", belt="Latched"), st, now)
    assert gw.doors_to_close_on_plug_in(
        _plug(d, "Charging", belt="Latched"), st, now + 10) == []


@pytest.mark.parametrize("gear", ["D", "R", None])
def test_not_in_park_suppresses_it(plug_closes, gear):
    """A car plugged in and not in park is something we do not understand,
    and we do not move doors on things we do not understand."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected", gear=gear), st, now)
    assert gw.doors_to_close_on_plug_in(
        _plug(d, "Charging", gear=gear), st, now + 10) == []


def test_plugging_in_away_from_home_closes_nothing(plug_closes):
    """A public charger is not your garage."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected", offset_m=5000), st, now)
    assert gw.doors_to_close_on_plug_in(
        _plug(d, "Charging", offset_m=5000), st, now + 10) == []


def test_an_already_closed_door_is_not_pulsed(plug_closes):
    """These openers are toggles, so pulsing a closed door OPENS it."""
    d = owned_by("dad")
    now = 1_000_000.0
    shut = {d.key: {"door_key": d.key, "is_open": 0, "updated_at": now}}
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), shut, now)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), shut, now + 10) == []


def test_cooldown_suppresses_it(plug_closes):
    """Two pulses seconds apart is what stops a door halfway, and it has
    happened on this garage before."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now)
    door_control.record_action(d.key, now)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), st, now + 5) == []


def test_it_does_not_double_pulse_with_the_parked_rule(plug_closes):
    """Both rules want this door shut. Exactly one pulse may go out."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now)
    plugged = _plug(d, "Charging")
    assert gw.doors_to_close_on_plug_in(plugged, st, now + 10) == [d.key]
    assert gw.doors_to_close_after_parking(plugged, st, now + 100) == []


def test_it_cancels_the_unplugged_reminder(plug_closes, monkeypatch):
    monkeypatch.setattr(config, "PLUGGED_IN_ALERT_S", 600)
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    unplugged = _parked_at(d, charge=None)
    gw.doors_to_close_after_parking(unplugged, st, now)
    gw.doors_to_close_after_parking(unplugged, st, now + 31)
    assert gw._awaiting_plug.get(d.key) is not None
    # The tick runs both rules every pass, so the plug rule has seen the car
    # unplugged too. Record that baseline here or there is no edge to detect.
    gw.doors_to_close_on_plug_in(_plug(d, "Disconnected"), st, now + 31)
    assert gw.doors_to_close_on_plug_in(_plug(d, "Charging"), st, now + 40) == [d.key]
    assert gw.doors_waiting_for_plug(now + 700) == []


# --- through the tick, not just the pure rule -------------------------------

def test_it_runs_end_to_end_through_the_tick(plug_closes):
    """The pure rule is covered above. This proves the tick wires it up --
    the step unit tests have silently missed in this project before.
    """
    d = owned_by("dad")
    models.upsert_door_state(d.key, True)
    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True,
                                gear="P", seatbelt="Unlatched",
                                charge_state="Disconnected")
    gw._tick()
    doors = {x["door_key"]: x["is_open"] for x in models.all_door_states()}
    assert doors[d.key] == 1, "baseline tick must not touch the door"

    models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True,
                                gear="P", seatbelt="Unlatched",
                                charge_state="Charging")
    gw._tick()
    doors = {x["door_key"]: x["is_open"] for x in models.all_door_states()}
    assert doors[d.key] == 0


def test_the_tick_leaves_it_alone_when_disabled(plug_closes, monkeypatch):
    monkeypatch.setattr(config, "PLUG_IN_CLOSES_ENABLED", False)
    d = owned_by("dad")
    models.upsert_door_state(d.key, True)
    for charge in ("Disconnected", "Charging"):
        models.upsert_vehicle_state("dad", *_ll(at(d, 3)), 0, 80, True,
                                    gear="P", seatbelt="Unlatched",
                                    charge_state=charge)
        gw._tick()
    doors = {x["door_key"]: x["is_open"] for x in models.all_door_states()}
    assert doors[d.key] == 1


# --- the case the edge cannot catch ----------------------------------------
#
# To drive off you must unplug, so "unplug, buckle, change mind, plug back
# in" produces an edge. But buckling up BEFORE unplugging is just as common,
# and if you then change your mind the cable never moved -- no transition,
# nothing for the edge rule to see. Observed live on 2026-11-01 09:37:54.

def test_a_connected_cable_cancels_the_grace_without_any_edge(plug_closes):
    """Buckle up while still plugged in, then change your mind. The cable
    never moved, so only the grace bypass can close this door."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    # Buckled while plugged in: the door opens and the grace starts.
    assert gw.doors_to_open_for_departure(
        _plug(d, "Complete", belt="Latched"), {}, now) == [d.key]
    assert gw._departing[d.key] == now
    # Unbuckle. Still plugged in; the cable never came out, so no edge.
    still_plugged = _plug(d, "Complete")
    assert gw.doors_to_close_on_plug_in(still_plugged, st, now + 10) == []
    # The parked rule closes it once the dwell passes, grace notwithstanding.
    gw.doors_to_close_after_parking(still_plugged, st, now + 10)
    assert gw.doors_to_close_after_parking(still_plugged, st, now + 45) == [d.key]
    assert config.DEPART_GRACE_S > 45, "otherwise this test proves nothing"


def test_the_grace_still_protects_an_unplugged_departure(plug_closes):
    """The bypass must be narrow. Unplugged and mid-departure, a momentary
    unbuckle must NOT be read as finished -- that is what the grace is for,
    and shutting a door on someone loading the car is the failure it
    prevents.
    """
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    assert gw.doors_to_open_for_departure(
        _plug(d, "Disconnected", belt="Latched"), {}, now) == [d.key]
    unplugged = _plug(d, "Disconnected")
    gw.doors_to_close_after_parking(unplugged, st, now + 10)
    assert gw.doors_to_close_after_parking(unplugged, st, now + 45) == []
    assert gw.doors_to_close_after_parking(unplugged, st, now + 300) == []


def test_the_grace_bypass_needs_the_car_actually_parked(plug_closes):
    """Plugged in but reporting D or R is not a car that has finished."""
    d = owned_by("dad")
    now = 1_000_000.0
    st = _open_state(d, now)
    gw.doors_to_open_for_departure(
        _plug(d, "Complete", belt="Latched"), {}, now)
    rolling = _plug(d, "Complete", gear="R")
    gw.doors_to_close_after_parking(rolling, st, now + 10)
    assert gw.doors_to_close_after_parking(rolling, st, now + 45) == []


# --- the zero-width window --------------------------------------------------

def test_warns_when_the_grace_outlives_the_open_belief(monkeypatch):
    """Observed live on 2026-11-01: DEPART_GRACE_S and DOOR_OPEN_TTL_S were
    both 600 s, so the parked rule's permitted window -- after the grace,
    before the belief expires -- was zero seconds wide. Garage 2 opened on a
    buckle at 09:37:54 and nothing could ever close it.
    """
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "DEPART_GRACE_S", 600)
    monkeypatch.setattr(config, "PARKED_DWELL_S", 30)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 600)
    warnings = " ".join(door_control.config_warnings())
    assert "NEVER close" in warnings, warnings


def test_no_warning_when_the_belief_outlives_the_grace(monkeypatch):
    monkeypatch.setattr(config, "DEPART_OPEN_ENABLED", True)
    monkeypatch.setattr(config, "PARKED_CLOSE_ENABLED", True)
    monkeypatch.setattr(config, "DEPART_GRACE_S", 600)
    monkeypatch.setattr(config, "PARKED_DWELL_S", 30)
    monkeypatch.setattr(door_control, "DOOR_OPEN_TTL_S", 1800)
    assert "NEVER close" not in " ".join(door_control.config_warnings())
