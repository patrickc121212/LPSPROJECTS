"""Is there a person in the vehicle?

The signals are limited in a way that matters. `DriverSeatOccupied` covers
the driver's seat only, Tesla describes it as platform-dependent, and
`PassengerSeatBelt` is documented by Tesla as reporting the wrong seat
entirely. So these tests mostly pin down what the app must NOT claim: no
combination of signals may produce "the car is empty", because a passenger,
a child or a dog in the back is invisible to all of them.
"""
from __future__ import annotations

import time

import pytest

import models
import occupancy
import telemetry_worker as tw


def _row(**kw):
    row = {"vehicle_key": "dad", "updated_at": time.time()}
    row.update(kw)
    return row


# --- what counts as somebody being present ---------------------------------

def test_driving_means_someone_is_aboard():
    v = occupancy.summarise(_row(gear="D", speed_mph=40))
    assert v["state"] == occupancy.PRESENT


def test_in_gear_counts_even_at_a_standstill():
    """Stopped in traffic still has a driver in it."""
    v = occupancy.summarise(_row(gear="D", speed_mph=0))
    assert v["state"] == occupancy.PRESENT


def test_a_latched_belt_means_someone_is_aboard():
    v = occupancy.summarise(_row(gear="P", seatbelt="Latched"))
    assert v["state"] == occupancy.PRESENT


def test_the_direct_signal_means_someone_is_aboard():
    v = occupancy.summarise(_row(gear="P", seatbelt="Unlatched", occupied=True))
    assert v["state"] == occupancy.PRESENT


def test_belt_wins_even_when_the_seat_signal_disagrees():
    """A latched belt is physical evidence; presence detection is modelled.
    Where they conflict, report the person as present."""
    v = occupancy.summarise(
        _row(gear="P", seatbelt="Latched", occupied=False))
    assert v["state"] == occupancy.PRESENT


def test_an_awake_screen_is_only_a_hint():
    v = occupancy.summarise(
        _row(gear="P", seatbelt="Unlatched", occupied=False, display_on=True))
    assert v["state"] == occupancy.LIKELY
    assert "driver" in v["detail"]


# --- what it must never claim ----------------------------------------------

def test_no_driver_is_never_described_as_empty():
    """The whole point. A passenger, a child or a dog in the back reports
    nothing at all, so "nobody in the driver's seat" is not "empty"."""
    v = occupancy.summarise(
        _row(gear="P", seatbelt="Unlatched", occupied=False, display_on=False))
    assert v["state"] == occupancy.NO_DRIVER
    assert "empty" not in v["label"].lower()
    assert "not the same as empty" in v["detail"]


def test_silence_is_not_absence():
    """A sleeping car transmits nothing. That must read as unknown, never as
    nobody aboard."""
    v = occupancy.summarise(_row(updated_at=time.time() - 3 * 3600,
                                 gear="P", seatbelt="Unlatched",
                                 occupied=False))
    assert v["state"] == occupancy.UNKNOWN
    assert "asleep" in v["detail"]


def test_a_stale_present_verdict_does_not_persist():
    """Someone was aboard three hours ago. That is history, not news."""
    v = occupancy.summarise(_row(updated_at=time.time() - 3 * 3600,
                                 gear="D", speed_mph=55, occupied=True))
    assert v["state"] == occupancy.UNKNOWN


def test_a_car_that_never_reports_presence_says_so():
    """Distinguishable from "nobody aboard", so the UI is not confidently
    blank for a model that cannot answer."""
    v = occupancy.summarise(_row(gear="P", seatbelt="Unlatched"))
    assert v["state"] == occupancy.UNKNOWN
    assert "may not support" in v["detail"]
    assert occupancy.supported(_row(gear="P")) is False
    assert occupancy.supported(_row(occupied=False)) is True


# --- the wire format ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("true", True), ("false", False),
    ("DriverSeatOccupiedTrue", True),
    ("Occupied", True), ("Unoccupied", False),
    ("1", True), ("0", False),
])
def test_presence_encodings_are_all_understood(raw, expected):
    """DriverSeatBelt was declared an enum and arrived as a JSON boolean, so
    assume nothing about how this one is encoded either."""
    assert tw._occupied(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "Faulted", "Nonsense", "Unknown"])
def test_an_unreadable_presence_value_is_unknown_not_absent(raw):
    """None, never False. "Nobody is in the car" is the answer that would
    justify moving a door, so a value we cannot parse must not produce it."""
    assert tw._occupied(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("Off", False), ("DisplayStateOff", False), ("Sleep", False),
    ("On", True), ("Driving", True), ("Charging", True), ("Sentry", True),
    (None, None), ("Unknown", None),
])
def test_display_states(raw, expected):
    assert tw._display_awake(raw) is expected


# --- it reaches the app ----------------------------------------------------

def test_presence_survives_the_round_trip_through_sqlite():
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True,
                                gear="P", seatbelt="Unlatched",
                                occupied=True, display_on=True)
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["occupied"] == 1 and row["display_on"] == 1
    assert row["presence"]["state"] == occupancy.PRESENT


def test_a_source_that_does_not_report_presence_does_not_wipe_it():
    """The poller reports no presence; it must not erase what telemetry knows."""
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True, occupied=True)
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True)
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["occupied"] == 1


def test_false_is_stored_rather_than_dropped():
    """COALESCE keeps old values when the new one is NULL, so 0 must not be
    confused with "not reported" on the way in."""
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True, occupied=True)
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True, occupied=False)
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["occupied"] == 0


def test_the_telemetry_field_lands_in_the_row():
    row = tw.apply_signal("dad", "DriverSeatOccupied", True)
    assert row["occupied"] is True
    row = tw.apply_signal("dad", "CenterDisplay", "Off")
    assert row["display_on"] is False


def test_every_vehicle_row_carries_a_verdict():
    """The map renders this per row, so a missing key would blank the UI."""
    for r in models.all_vehicle_states():
        assert "presence" in r
        assert r["presence"]["state"] in (
            occupancy.PRESENT, occupancy.LIKELY,
            occupancy.NO_DRIVER, occupancy.UNKNOWN)


# --- the field we deliberately did not subscribe ---------------------------

def test_passenger_seat_belt_is_not_used():
    """Tesla's docs: PassengerSeatBelt "improperly reports if the 2nd row
    center seat belt is buckled". Subscribing it would put a wrong answer
    about the front passenger on screen.
    """
    import tesla_setup
    assert "PassengerSeatBelt" not in tesla_setup.telemetry_fields()
    assert "DriverSeatOccupied" in tesla_setup.telemetry_fields()


# --- it actually reaches the page ------------------------------------------

def test_the_map_page_renders_a_presence_slot_per_vehicle(auth):
    """The map template had no render coverage at all. A Jinja typo there
    would have reached the live site unnoticed -- which is how the launcher
    went three hours broken earlier today.
    """
    import config
    r = auth.get("/map")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for v in config.VEHICLES:
        assert f'presence-{v.key}' in html, f"no presence slot for {v.key}"


def test_the_vehicles_api_carries_the_verdict(auth):
    """app.js reads row.presence straight off this payload."""
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True,
                                gear="P", seatbelt="Latched")
    rows = auth.get("/api/vehicles").get_json()
    dad = {r["vehicle_key"]: r for r in rows}["dad"]
    assert dad["presence"]["state"] == occupancy.PRESENT
    assert dad["presence"]["label"] and dad["presence"]["detail"]
