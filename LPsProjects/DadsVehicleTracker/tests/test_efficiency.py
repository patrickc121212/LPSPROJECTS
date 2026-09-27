"""Cost per mile, miles per kWh, and charging losses.

These numbers come from joining two logs that were built separately, so most
of the care here is about what happens when one side is missing or thin.
"""
from __future__ import annotations

import charging
import config
import efficiency
import models
import trips
from conftest import owned_by


def _trip_month(vehicle="dad", month="2026-09", trips_n=4, miles=100.0, seconds=7200.0):
    return {"vehicle_key": vehicle, "month": month, "trips": trips_n,
            "miles": miles, "seconds": seconds}


def _charge_month(vehicle="dad", month="2026-09", sessions=3, kwh=30.0, cost=3.90):
    return {"vehicle_key": vehicle, "month": month, "sessions": sessions,
            "kwh": kwh, "cost": cost}


# --- the join ---------------------------------------------------------------

def test_cost_per_mile_and_miles_per_kwh():
    rows = efficiency.combine([_trip_month(miles=100.0)], [_charge_month(kwh=25.0, cost=3.25)])
    assert len(rows) == 1
    assert rows[0]["cost_per_mile"] == 0.0325
    assert rows[0]["miles_per_kwh"] == 4.0


def test_driving_without_charging_yields_no_rate():
    """A month where nothing was charged cannot have a cost per mile; it is
    unknown, not zero."""
    rows = efficiency.combine([_trip_month(miles=80.0)], [])
    assert rows[0]["miles"] == 80.0
    assert rows[0]["cost_per_mile"] is None
    assert rows[0]["miles_per_kwh"] is None


def test_charging_without_driving_yields_no_rate():
    """Dividing by nearly zero miles would report an absurd cost per mile."""
    rows = efficiency.combine([], [_charge_month(kwh=40.0, cost=5.20)])
    assert rows[0]["kwh"] == 40.0
    assert rows[0]["cost_per_mile"] is None


def test_a_trivial_month_is_not_reported_as_a_rate():
    """Two miles and a full charge is not a meaningful efficiency figure."""
    rows = efficiency.combine([_trip_month(miles=2.0)], [_charge_month(kwh=40.0, cost=5.2)])
    assert rows[0]["cost_per_mile"] is None


def test_months_and_vehicles_stay_separate():
    rows = efficiency.combine(
        [_trip_month("dad", "2026-09"), _trip_month("mom", "2026-09"),
         _trip_month("dad", "2026-08")],
        [_charge_month("dad", "2026-09")])
    keys = {(r["vehicle_key"], r["month"]) for r in rows}
    assert keys == {("dad", "2026-09"), ("mom", "2026-09"), ("dad", "2026-08")}


def test_newest_month_first():
    rows = efficiency.combine(
        [_trip_month(month="2026-07"), _trip_month(month="2026-09"),
         _trip_month(month="2026-08")], [])
    assert [r["month"] for r in rows] == ["2026-09", "2026-08", "2026-07"]


def test_hours_are_converted_from_seconds():
    rows = efficiency.combine([_trip_month(seconds=5400.0)], [])
    assert rows[0]["hours"] == 1.5


# --- totals -----------------------------------------------------------------

def test_overall_totals_across_months_and_vehicles():
    rows = efficiency.combine(
        [_trip_month("dad", "2026-09", miles=100.0),
         _trip_month("mom", "2026-09", miles=50.0)],
        [_charge_month("dad", "2026-09", kwh=25.0, cost=3.25),
         _charge_month("mom", "2026-09", kwh=15.0, cost=1.95)])
    total = efficiency.overall(rows)
    assert total["miles"] == 150.0 and total["kwh"] == 40.0
    assert total["cost"] == 5.2
    assert round(total["cost_per_mile"], 5) == round(5.2 / 150.0, 5)


def test_overall_with_nothing_recorded():
    total = efficiency.overall([])
    assert total["miles"] == 0 and total["cost_per_mile"] is None


def test_per_vehicle_rate_ignores_other_vehicles():
    rows = efficiency.combine(
        [_trip_month("dad", "2026-09", miles=100.0),
         _trip_month("mom", "2026-09", miles=100.0)],
        [_charge_month("dad", "2026-09", cost=10.0),
         _charge_month("mom", "2026-09", cost=2.0)])
    assert efficiency.cost_per_mile("dad", rows) == 0.1
    assert efficiency.cost_per_mile("mom", rows) == 0.02
    assert efficiency.cost_per_mile("lp", rows) is None


# --- per-trip estimate ------------------------------------------------------

def test_trip_cost_estimate():
    rates = {"dad": 0.04}
    assert efficiency.estimate_trip_cost({"vehicle_key": "dad", "distance_mi": 12.5}, rates) == 0.5


def test_trip_cost_unknown_without_a_rate_or_distance():
    assert efficiency.estimate_trip_cost({"vehicle_key": "dad", "distance_mi": 10}, {}) is None
    assert efficiency.estimate_trip_cost({"vehicle_key": "dad", "distance_mi": None},
                                         {"dad": 0.04}) is None


# --- charging losses --------------------------------------------------------

def test_efficiency_from_wall_versus_battery():
    """The live cars showed 8.70 kWh from the wall for 7.16 into the pack."""
    result = efficiency.charging_efficiency([
        {"kwh": 8.70, "kwh_dc": 7.16}])
    assert result["known"] is True
    assert result["efficiency_pct"] == 82.3
    assert result["wasted_kwh"] == 1.5
    assert result["wasted_cost"] == round(1.54 * config.ELECTRICITY_RATE_PER_KWH, 2)


def test_efficiency_unknown_without_battery_side_figures():
    """Older sessions were recorded before kwh_dc existed."""
    assert efficiency.charging_efficiency([{"kwh": 10.0, "kwh_dc": None}])["known"] is False
    assert efficiency.charging_efficiency([])["known"] is False


def test_efficiency_ignores_incomplete_sessions():
    result = efficiency.charging_efficiency([
        {"kwh": 10.0, "kwh_dc": 9.0},
        {"kwh": None, "kwh_dc": None},
    ])
    assert result["sessions"] == 1


# --- through the database ---------------------------------------------------

def test_battery_side_energy_is_recorded(monkeypatch):
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 40,
                             "ac_energy": 0.0, "dc_energy": 0.0}, 1.0)
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 60,
                             "ac_energy": 10.0, "dc_energy": 8.2}, 2.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 60,
                             "ac_energy": 10.0, "dc_energy": 8.2}, 3.0)
    s = models.list_charge_sessions()[0]
    assert s["kwh"] == 10.0, "billed at the wall"
    assert s["kwh_dc"] == 8.2, "delivered to the pack"
    eff = efficiency.charging_efficiency(models.list_charge_sessions())
    assert eff["efficiency_pct"] == 82.0


def test_end_to_end_cost_per_mile():
    """A drive and a charge in the same month produce a real rate."""
    d = owned_by("dad")

    def snap(offset_m, speed=40.0, gear="D"):
        return {"latitude": d.latitude + offset_m / 111_320.0, "longitude": d.longitude,
                "speed_mph": speed, "battery_pct": 70, "gear": gear, "odometer": None}

    trips.observe("dad", snap(0), 1_700_000_000.0)
    trips.observe("dad", snap(16093), 1_700_000_600.0)          # ~10 miles
    trips.observe("dad", snap(16093, speed=0, gear="P"),
                  1_700_000_600.0 + config.TRIP_IDLE_END_S + 1)
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 60,
                             "ac_energy": 0.0}, 1_700_001_000.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 80,
                             "ac_energy": 4.0}, 1_700_002_000.0)

    rows = efficiency.by_month()
    assert rows, "a month with both driving and charging should appear"
    row = rows[0]
    assert row["miles"] > 9
    assert row["kwh"] == 4.0
    assert row["cost_per_mile"] is not None
    assert row["miles_per_kwh"] > 2


# --- routes -----------------------------------------------------------------

def test_charging_api_exposes_costs(auth):
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 50,
                             "ac_energy": 0.0, "dc_energy": 0.0}, 1.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 70,
                             "ac_energy": 9.0, "dc_energy": 7.4}, 2.0)
    body = auth.get("/api/charging").get_json()
    assert "costs_by_month" in body and "overall" in body
    assert body["charging_efficiency"]["known"] is True


def test_charging_page_renders_with_costs(auth):
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 50,
                             "ac_energy": 0.0, "dc_energy": 0.0}, 1.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 70,
                             "ac_energy": 9.0, "dc_energy": 7.4}, 2.0)
    page = auth.get("/charging").data
    assert b"Charging efficiency" in page
    assert b"reaches the battery" in page


def test_trips_page_shows_an_estimated_cost(admin):
    d = owned_by("dad")

    def snap(offset_m, speed=40.0, gear="D"):
        return {"latitude": d.latitude + offset_m / 111_320.0, "longitude": d.longitude,
                "speed_mph": speed, "battery_pct": 70, "gear": gear, "odometer": None}

    trips.observe("dad", snap(0), 1_700_000_000.0)
    trips.observe("dad", snap(16093), 1_700_000_600.0)
    trips.observe("dad", snap(16093, speed=0, gear="P"),
                  1_700_000_600.0 + config.TRIP_IDLE_END_S + 1)
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 60,
                             "ac_energy": 0.0}, 1_700_001_000.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 80,
                             "ac_energy": 4.0}, 1_700_002_000.0)
    assert b"Est. cost" in admin.get("/trips").data


def test_zero_charging_is_unknown_not_free():
    """Explicitly: no charging means no rate, never $0.00 per mile."""
    rows = efficiency.combine([_trip_month(miles=500.0)], [])
    assert rows[0]["cost_per_mile"] is None
    assert efficiency.overall(rows)["cost_per_mile"] is None
    assert efficiency.cost_per_mile("dad", rows) is None
