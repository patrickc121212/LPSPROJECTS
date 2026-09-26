"""Charging session detection, energy measurement and costing."""
from __future__ import annotations

import charging
import config
import models
import telemetry_worker as tw


def _snap(state="Charging", pct=50, lifetime=None, ac=None, dc=None, kw=None):
    return {"charge_state": state, "battery_pct": pct, "lifetime_kwh": lifetime,
            "ac_energy": ac, "dc_energy": dc, "power_kw": kw}


# --- state machine ----------------------------------------------------------

def test_session_opens_on_charging_and_closes_on_disconnect():
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 50, True)
    charging.observe("dad", _snap(pct=50, lifetime=1000.0), 100.0, at_home=True)
    live = models.get_open_charge_session("dad")
    assert live is not None and live["start_pct"] == 50 and live["at_home"] == 1

    charging.observe("dad", _snap(pct=70, lifetime=1012.5), 200.0, at_home=True)
    charging.observe("dad", _snap(state="Disconnected", pct=70, lifetime=1012.5), 300.0)
    assert models.get_open_charge_session("dad") is None
    done = models.list_charge_sessions()[0]
    assert done["ended_at"] == 300.0
    assert done["kwh"] == 12.5
    assert done["end_pct"] == 70
    assert done["source"] == "lifetime"


def test_cost_uses_the_configured_rate(monkeypatch):
    monkeypatch.setattr(config, "ELECTRICITY_RATE_PER_KWH", 0.13)
    charging.observe("dad", _snap(lifetime=100.0), 1.0)
    charging.observe("dad", _snap(lifetime=110.0), 2.0)
    charging.observe("dad", _snap(state="Complete", lifetime=110.0), 3.0)
    s = models.list_charge_sessions()[0]
    assert s["kwh"] == 10.0
    assert s["cost"] == round(10.0 * 0.13, 4) == 1.3


def test_starting_counts_as_active_and_does_not_reopen():
    charging.observe("dad", _snap(state="Starting", lifetime=5.0), 1.0)
    first = models.get_open_charge_session("dad")["id"]
    charging.observe("dad", _snap(state="Charging", lifetime=6.0), 2.0)
    assert models.get_open_charge_session("dad")["id"] == first
    assert len(models.list_charge_sessions()) == 1


def test_idle_states_never_open_a_session():
    for state in ("Disconnected", "Complete", "Stopped", "NoPower", None, "Unknown"):
        charging.observe("dad", _snap(state=state), 1.0)
    assert models.list_charge_sessions() == []


def test_two_separate_sessions_are_recorded():
    charging.observe("dad", _snap(lifetime=10.0), 1.0)
    charging.observe("dad", _snap(state="Complete", lifetime=15.0), 2.0)
    charging.observe("dad", _snap(lifetime=15.0), 10.0)
    charging.observe("dad", _snap(state="Disconnected", lifetime=18.0), 11.0)
    rows = models.list_charge_sessions()
    assert len(rows) == 2
    assert sorted(r["kwh"] for r in rows) == [3.0, 5.0]


def test_sessions_are_per_vehicle():
    charging.observe("dad", _snap(lifetime=10.0), 1.0)
    charging.observe("lp", _snap(lifetime=500.0), 1.0)
    assert models.get_open_charge_session("dad")["id"] != models.get_open_charge_session("lp")["id"]
    charging.observe("dad", _snap(state="Complete", lifetime=14.0), 2.0)
    assert models.get_open_charge_session("lp") is not None
    assert models.get_open_charge_session("dad") is None


# --- energy measurement -----------------------------------------------------

def test_lifetime_delta_is_preferred():
    assert charging.session_energy(100.0, 107.25, 999.0) == (7.25, "lifetime")


def test_falls_back_to_the_session_counter_without_lifetime():
    """Older cars may not report LifetimeEnergyChargedKwh at all."""
    assert charging.session_energy(None, None, 8.4) == (8.4, "counter")


def test_lifetime_counter_reset_falls_back_rather_than_going_negative():
    kwh, source = charging.session_energy(500.0, 3.0, 9.1)
    assert kwh == 9.1 and source == "counter"


def test_no_energy_data_records_none():
    assert charging.session_energy(None, None, None) == (None, None)
    charging.observe("dad", _snap(lifetime=None), 1.0)
    charging.observe("dad", _snap(state="Complete", lifetime=None), 2.0)
    s = models.list_charge_sessions()[0]
    assert s["kwh"] is None and s["cost"] is None


def test_ac_and_dc_counters_are_combined():
    charging.observe("dad", _snap(ac=4.0, dc=None), 1.0)
    charging.observe("dad", _snap(state="Complete", ac=4.0), 2.0)
    assert models.list_charge_sessions()[0]["kwh"] == 4.0


def test_counter_running_max_survives_a_dip():
    """A counter that resets mid-session must not shrink the total."""
    charging.observe("dad", _snap(ac=1.0), 1.0)
    charging.observe("dad", _snap(ac=6.0), 2.0)
    charging.observe("dad", _snap(ac=0.0), 3.0)
    charging.observe("dad", _snap(state="Complete", ac=0.0), 4.0)
    assert models.list_charge_sessions()[0]["kwh"] == 6.0


def test_peak_power_is_the_maximum_seen():
    charging.observe("dad", _snap(lifetime=1.0, kw=7.0), 1.0)
    charging.observe("dad", _snap(lifetime=2.0, kw=11.5), 2.0)
    charging.observe("dad", _snap(lifetime=3.0, kw=6.0), 3.0)
    charging.observe("dad", _snap(state="Complete", lifetime=3.0), 4.0)
    assert models.list_charge_sessions()[0]["peak_kw"] == 11.5


def test_open_session_survives_a_restart():
    """The session lives in SQLite, not memory, so a mid-charge restart
    still produces a complete record."""
    charging.observe("dad", _snap(lifetime=10.0), 1.0)
    charging.observe("dad", _snap(lifetime=13.0), 2.0)
    # ... process restarts here; nothing in-memory carries over ...
    charging.observe("dad", _snap(state="Disconnected", lifetime=13.0), 3.0)
    assert models.list_charge_sessions()[0]["kwh"] == 3.0


# --- summaries --------------------------------------------------------------

def test_summary_totals_ignore_the_in_progress_session():
    charging.observe("dad", _snap(lifetime=0.0), 1.0)
    charging.observe("dad", _snap(state="Complete", lifetime=10.0), 2.0)
    charging.observe("dad", _snap(lifetime=10.0), 3.0)      # still charging
    s = charging.summary(models.list_charge_sessions())
    assert s["sessions"] == 1 and s["kwh"] == 10.0
    assert s["cost"] == round(10.0 * config.ELECTRICITY_RATE_PER_KWH, 2)


def test_summary_counts_home_versus_away():
    charging.observe("dad", _snap(lifetime=0.0), 1.0, at_home=True)
    charging.observe("dad", _snap(state="Complete", lifetime=5.0), 2.0)
    charging.observe("lp", _snap(lifetime=0.0), 3.0, at_home=False)
    charging.observe("lp", _snap(state="Complete", lifetime=5.0), 4.0)
    s = charging.summary(models.list_charge_sessions())
    assert s["home"] == 1 and s["away"] == 1


def test_monthly_totals_group_by_vehicle_and_month():
    charging.observe("dad", _snap(lifetime=0.0), 1_700_000_000.0)
    charging.observe("dad", _snap(state="Complete", lifetime=20.0), 1_700_000_100.0)
    rows = models.charge_totals_by_month()
    assert len(rows) == 1
    assert rows[0]["vehicle_key"] == "dad" and rows[0]["kwh"] == 20.0


# --- wiring through telemetry ----------------------------------------------

def test_energy_fields_are_parsed_from_telemetry(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    tw._state.clear()
    for field, key, raw in [("ACChargingEnergyIn", "ac_energy", 3.5),
                            ("DCChargingEnergyIn", "dc_energy", "2.5"),
                            ("ACChargingPower", "power_kw", 7.2),
                            ("LifetimeEnergyChargedKwh", "lifetime_kwh", 12345.6)]:
        tw.apply_signal("dad", field, raw)
        assert tw._state["dad"][key] == float(raw)
    tw._state.clear()


def test_flush_drives_the_charging_machine(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    tw._state.clear()
    tw.apply_signal("dad", "Location", {"latitude": config.GARAGE_DOORS[0].latitude,
                                        "longitude": config.GARAGE_DOORS[0].longitude})
    tw.apply_signal("dad", "DetailedChargeState", "DetailedChargeStateCharging")
    tw.apply_signal("dad", "LifetimeEnergyChargedKwh", 100.0)
    tw.flush()
    live = models.get_open_charge_session("dad")
    assert live is not None and live["at_home"] == 1
    tw.apply_signal("dad", "LifetimeEnergyChargedKwh", 106.0)
    tw.apply_signal("dad", "DetailedChargeState", "DetailedChargeStateComplete")
    tw.flush()
    assert models.list_charge_sessions()[0]["kwh"] == 6.0
    tw._state.clear()


def test_charging_failure_never_breaks_the_map(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    monkeypatch.setattr(charging, "observe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tw._state.clear()
    tw.apply_signal("dad", "Location", {"latitude": 1.0, "longitude": 2.0})
    caplog.set_level(logging.ERROR)
    tw.flush()   # must not raise
    assert {r["vehicle_key"] for r in models.all_vehicle_states()} >= {"dad"}
    tw._state.clear()


# --- routes -----------------------------------------------------------------

def test_charging_page_renders(auth):
    charging.observe("dad", _snap(lifetime=0.0), 1.0, at_home=True)
    charging.observe("dad", _snap(state="Complete", lifetime=10.0), 2.0)
    r = auth.get("/charging")
    assert r.status_code == 200
    assert b"Charging" in r.data


def test_charging_api_shape(auth):
    charging.observe("dad", _snap(lifetime=0.0), 1.0, at_home=True)
    charging.observe("dad", _snap(state="Complete", lifetime=10.0), 2.0)
    body = auth.get("/api/charging").get_json()
    assert body["rate_per_kwh"] == config.ELECTRICITY_RATE_PER_KWH
    assert body["summary"]["kwh"] == 10.0
    assert len(body["sessions"]) == 1


def test_charging_api_filters_by_vehicle(auth):
    charging.observe("dad", _snap(lifetime=0.0), 1.0)
    charging.observe("dad", _snap(state="Complete", lifetime=4.0), 2.0)
    charging.observe("lp", _snap(lifetime=0.0), 3.0)
    charging.observe("lp", _snap(state="Complete", lifetime=9.0), 4.0)
    assert len(auth.get("/api/charging?as=lp").get_json()["sessions"]) == 1
    assert len(auth.get("/api/charging").get_json()["sessions"]) == 2
    # An unknown key must not be treated as a filter.
    assert len(auth.get("/api/charging?as=nobody").get_json()["sessions"]) == 2


def test_charging_requires_login(client):
    assert client.get("/charging").status_code == 302
    assert client.get("/api/charging").status_code == 302


def test_ac_and_dc_counters_are_not_summed():
    """Observed live: a car AC-charging reports AC 8.70 kWh (from the wall)
    and DC 7.16 kWh (into the battery) at the same time. Summing them bills
    ~1.8x. The grid-side figure is the one the meter charges for."""
    assert charging._counter({"ac_energy": 8.70, "dc_energy": 7.16}) == 8.70


def test_dc_fast_charging_uses_the_dc_counter():
    assert charging._counter({"ac_energy": 0.0, "dc_energy": 21.4}) == 21.4
    assert charging._counter({"ac_energy": None, "dc_energy": 21.4}) == 21.4


def test_counter_absent_versus_zero():
    assert charging._counter({}) is None
    assert charging._counter({"ac_energy": 0.0}) == 0.0


def test_full_ac_session_bills_the_grid_side_only():
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 50,
                             "ac_energy": 0.0, "dc_energy": 0.0}, 1.0)
    charging.observe("dad", {"charge_state": "Charging", "battery_pct": 70,
                             "ac_energy": 10.0, "dc_energy": 8.2}, 2.0)
    charging.observe("dad", {"charge_state": "Complete", "battery_pct": 70,
                             "ac_energy": 10.0, "dc_energy": 8.2}, 3.0)
    s = models.list_charge_sessions()[0]
    assert s["kwh"] == 10.0
    assert s["cost"] == round(10.0 * config.ELECTRICITY_RATE_PER_KWH, 4)
