"""Trip detection, distance measurement and position history."""
from __future__ import annotations

import config
import models
import telemetry_worker as tw
import trips
from conftest import owned_by


def _p(door, offset_m=0.0, speed=30.0, pct=80, gear="D", odo=None):
    """A snapshot `offset_m` north of a door."""
    return {"latitude": door.latitude + offset_m / 111_320.0,
            "longitude": door.longitude, "speed_mph": speed,
            "battery_pct": pct, "gear": gear, "odometer": odo}


def _drive(key, door, metres, **kw):
    return _p(door, metres, **kw)


# --- trip boundaries --------------------------------------------------------

def test_trip_opens_when_moving_and_closes_after_idling():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    assert models.get_open_trip("dad") is not None
    trips.observe("dad", _drive("dad", d, 2000), 1100.0)
    # Stops; the trip stays open until the idle timeout passes.
    trips.observe("dad", _drive("dad", d, 2000, speed=0, gear="P"), 1200.0)
    assert models.get_open_trip("dad") is not None
    trips.observe("dad", _drive("dad", d, 2000, speed=0, gear="P"),
                  1200.0 + config.TRIP_IDLE_END_S + 1)
    assert models.get_open_trip("dad") is None
    assert len(models.list_trips()) == 1


def test_parked_car_never_opens_a_trip():
    d = owned_by("dad")
    for i in range(5):
        trips.observe("dad", _drive("dad", d, 0, speed=0, gear="P"), 1000.0 + i)
    assert models.list_trips() == []
    assert models.get_open_trip("dad") is None


def test_gear_drive_at_a_standstill_counts_as_moving():
    """Creeping in traffic still reads 0 mph between samples."""
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0, speed=0.0, gear="D"), 1000.0)
    assert models.get_open_trip("dad") is not None


def test_short_shuffle_is_discarded():
    """Moving a car twenty metres on the driveway is not a trip."""
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 20), 1010.0)
    trips.observe("dad", _drive("dad", d, 20, speed=0, gear="P"),
                  1010.0 + config.TRIP_IDLE_END_S + 1)
    assert models.list_trips() == []


def test_trip_end_time_is_when_it_stopped_not_when_we_noticed():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 3000), 1100.0)
    stop = 1100.0
    trips.observe("dad", _drive("dad", d, 3000, speed=0, gear="P"),
                  stop + config.TRIP_IDLE_END_S + 5)
    t = models.list_trips()[0]
    assert t["ended_at"] == stop, "idle time must not be counted as driving"


def test_two_trips_are_separate():
    d = owned_by("dad")
    for base in (1000.0, 9000.0):
        trips.observe("dad", _drive("dad", d, 0), base)
        trips.observe("dad", _drive("dad", d, 3000), base + 60)
        trips.observe("dad", _drive("dad", d, 3000, speed=0, gear="P"),
                      base + 60 + config.TRIP_IDLE_END_S + 1)
    assert len(models.list_trips()) == 2


def test_trips_are_per_vehicle():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("lp", _drive("lp", d, 0), 1000.0)
    assert models.get_open_trip("dad")["id"] != models.get_open_trip("lp")["id"]


def test_missing_fix_is_ignored():
    trips.observe("dad", {"latitude": None, "longitude": None, "speed_mph": 40}, 1000.0)
    assert models.get_open_trip("dad") is None


# --- distance ---------------------------------------------------------------

def test_odometer_is_preferred_over_gps():
    assert trips.distance_of({"odo_start": 1000.0, "odo_end": 1012.4}, 11.9) == (12.4, "odometer")


def test_gps_used_when_there_is_no_odometer():
    assert trips.distance_of({"odo_start": None, "odo_end": None}, 7.25) == (7.25, "gps")


def test_odometer_going_backwards_falls_back_to_gps():
    assert trips.distance_of({"odo_start": 900.0, "odo_end": 10.0}, 4.0) == (4.0, "gps")


def test_no_distance_at_all():
    assert trips.distance_of({}, None) == (None, None)


def test_gps_distance_accumulates_along_the_route():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    for n in range(1, 6):
        trips.observe("dad", _drive("dad", d, n * 1609.344), 1000.0 + n * 60)
    t = models.get_open_trip("dad")
    assert abs(t["distance_mi"] - 5.0) < 0.05


def test_odometer_distance_wins_end_to_end():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0, odo=10_000.0), 1000.0)
    trips.observe("dad", _drive("dad", d, 8000, odo=10_005.5), 1100.0)
    trips.observe("dad", _drive("dad", d, 8000, speed=0, gear="P", odo=10_005.5),
                  1100.0 + config.TRIP_IDLE_END_S + 1)
    t = models.list_trips()[0]
    assert t["distance_mi"] == 5.5 and t["source"] == "odometer"


def test_gps_noise_does_not_inflate_distance():
    """Sub-threshold jitter while parked must not accumulate miles."""
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    start = models.get_open_trip("dad")["distance_mi"] or 0.0
    for i in range(20):
        trips.observe("dad", _drive("dad", d, 2 + (i % 2)), 1001.0 + i)
    assert (models.get_open_trip("dad")["distance_mi"] or 0.0) - start < 0.01


def test_max_speed_is_recorded():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0, speed=20), 1000.0)
    trips.observe("dad", _drive("dad", d, 2000, speed=64), 1060.0)
    trips.observe("dad", _drive("dad", d, 4000, speed=30), 1120.0)
    assert models.get_open_trip("dad")["max_speed_mph"] == 64


def test_battery_range_is_recorded():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0, pct=90), 1000.0)
    trips.observe("dad", _drive("dad", d, 5000, pct=82), 1100.0)
    t = models.get_open_trip("dad")
    assert t["start_pct"] == 90 and t["end_pct"] == 82


# --- position history -------------------------------------------------------

def test_positions_are_stored_and_linked_to_the_trip():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 1000), 1060.0)
    trip_id = models.get_open_trip("dad")["id"]
    path = models.trip_path(trip_id)
    assert len(path) >= 2
    assert path[0]["ts"] < path[-1]["ts"]


def test_history_is_kept_even_when_not_on_a_trip():
    """Breadcrumbs outside a trip are still worth having."""
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0, speed=0, gear="P"), 1000.0)
    trips.observe("dad", _drive("dad", d, 500, speed=0, gear="P"), 1060.0)
    rows = models.positions_between("dad", 0, 2000)
    assert len(rows) == 2 and all(r["ts"] for r in rows)


def test_repeated_identical_fixes_are_not_stored_twice():
    d = owned_by("dad")
    for i in range(10):
        trips.observe("dad", _drive("dad", d, 0, speed=0, gear="P"), 1000.0 + i)
    assert len(models.positions_between("dad", 0, 2000)) == 1


def test_pruning_drops_old_points_but_keeps_trips(monkeypatch):
    d = owned_by("dad")
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 30)
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 3000), 1100.0)
    trips.observe("dad", _drive("dad", d, 3000, speed=0, gear="P"),
                  1100.0 + config.TRIP_IDLE_END_S + 1)
    assert len(models.list_trips()) == 1
    trips._last_prune = 0.0
    removed = trips.maybe_prune(now=1100.0 + 40 * 86400)
    assert removed >= 2
    assert len(models.list_trips()) == 1, "the trip summary must survive"
    assert models.positions_between("dad", 0, 1e12) == []


def test_pruning_is_rate_limited(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 30)
    trips._last_prune = 0.0
    trips.maybe_prune(now=1_000_000.0)
    assert trips.maybe_prune(now=1_000_010.0) == 0


def test_retention_zero_disables_pruning(monkeypatch):
    d = owned_by("dad")
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 0)
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips._last_prune = 0.0
    assert trips.maybe_prune(now=1e12) == 0
    assert models.positions_between("dad", 0, 1e12) != []


# --- naming -----------------------------------------------------------------

def test_place_name_labels_home():
    d = owned_by("dad")
    assert trips.place_name(d.latitude, d.longitude) == "Home"
    assert trips.place_name(d.latitude + 0.5, d.longitude) is None
    assert trips.place_name(None, None) is None


def test_summary_totals():
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 5000, speed=55), 1100.0)
    trips.observe("dad", _drive("dad", d, 5000, speed=0, gear="P"),
                  1100.0 + config.TRIP_IDLE_END_S + 1)
    s = trips.summary(models.list_trips())
    assert s["trips"] == 1 and s["miles"] > 3 and s["max_speed"] == 55


# --- wiring + routes --------------------------------------------------------

def test_odometer_is_parsed_from_telemetry(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    tw._state.clear()
    tw.apply_signal("dad", "Odometer", 12_345.6)
    assert tw._state["dad"]["odometer"] == 12_345.6
    tw._state.clear()


def test_trip_failure_never_breaks_the_map(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    monkeypatch.setattr(trips, "observe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tw._state.clear()
    tw.apply_signal("dad", "Location", {"latitude": 1.0, "longitude": 2.0})
    caplog.set_level(logging.ERROR)
    tw.flush()
    assert {r["vehicle_key"] for r in models.all_vehicle_states()} >= {"dad"}
    tw._state.clear()


def test_trips_page_and_api(auth):
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 5000), 1100.0)
    trips.observe("dad", _drive("dad", d, 5000, speed=0, gear="P"),
                  1100.0 + config.TRIP_IDLE_END_S + 1)
    assert auth.get("/trips").status_code == 200
    body = auth.get("/api/trips").get_json()
    assert body["summary"]["trips"] == 1 and len(body["trips"]) == 1


def test_trip_path_endpoint(auth):
    d = owned_by("dad")
    trips.observe("dad", _drive("dad", d, 0), 1000.0)
    trips.observe("dad", _drive("dad", d, 2000), 1060.0)
    tid = models.get_open_trip("dad")["id"]
    body = auth.get(f"/api/trips/{tid}/path").get_json()
    assert len(body["path"]) >= 2
    assert body["trip"]["id"] == tid
    assert auth.get("/api/trips/99999/path").status_code == 404


def test_trips_require_login(client):
    assert client.get("/trips").status_code == 302
    assert client.get("/api/trips").status_code == 302
