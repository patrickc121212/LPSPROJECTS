"""Tesla's own vehicle alerts.

These were arriving on MQTT and being dropped. The care here is in *not*
showing everything: most carry a Service audience and engineering names, and
the "current" topic includes alerts that have already ended.
"""
from __future__ import annotations

import json

import alerts
import models
import telemetry_worker as tw


ENDED = {"Audiences": ["Customer", "Service"],
         "StartedAt": "2026-09-27T08:42:42Z",
         "EndedAt": "2026-09-27T08:42:43Z"}
LIVE = {"Audiences": ["Customer", "Service"],
        "StartedAt": "2026-09-27T08:42:42Z",
        "EndedAt": ""}
SERVICE_ONLY = {"Audiences": ["Service"],
                "StartedAt": "2026-09-27T08:42:42Z",
                "EndedAt": ""}


# --- reading the payload ----------------------------------------------------

def test_an_alert_with_an_end_time_is_not_active():
    """The 'current' topic carries ended alerts too — treating it as a list
    of live problems would show every historical fault."""
    assert alerts.is_active(ENDED) is False
    assert alerts.is_active(LIVE) is True


def test_customer_audience_is_what_we_surface():
    assert alerts.is_for_customer(["Customer", "Service"]) is True
    assert alerts.is_for_customer(["Service"]) is False
    assert alerts.is_for_customer([]) is False
    assert alerts.is_for_customer(None) is False


def test_timestamps_parse_to_epoch():
    assert alerts.parse_time("2026-09-27T08:42:42Z") == 1790498562.0
    assert alerts.parse_time("") is None
    assert alerts.parse_time(None) is None
    assert alerts.parse_time("not a date") is None
    # A real offset must be honoured, not overwritten with UTC.
    assert alerts.parse_time("2026-09-27T08:42:42+02:00") == alerts.parse_time("2026-09-27T06:42:42Z")


def test_names_are_made_readable():
    assert alerts.humanise("APP_w390_cabinCamVisDegraded") == "Cabin cam vis degraded"
    assert alerts.humanise("UI_a114_drivingVisualizationDegraded") == "Driving visualization degraded"
    assert alerts.humanise("CP_a044_lostCommsHVP") == "Lost comms HVP", "acronyms stay intact"
    assert alerts.humanise("somethingOdd") == "Something odd"
    assert alerts.humanise("APP_w1_aB") == "A b", "sentence case, not title case"
    assert alerts.humanise("") == ""


# --- recording --------------------------------------------------------------

def test_a_new_live_customer_alert_is_reported():
    fresh = alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE)
    assert fresh is not None
    assert fresh["label"] == "Cabin cam vis degraded"


def test_an_already_ended_alert_is_stored_but_silent():
    assert alerts.observe("dad", "CP_a044_lostCommsHVP", ENDED) is None
    assert len(models.list_alerts(active_only=False)) == 1


def test_a_service_only_alert_is_stored_but_silent():
    """Engineering noise: recorded for the log, never pushed."""
    assert alerts.observe("dad", "CP_a044_lostCommsHVP", SERVICE_ONLY) is None
    assert len(models.list_alerts(active_only=False)) == 1
    assert alerts.active() == []


def test_the_same_alert_is_only_reported_once():
    """The car republishes; a phone should buzz once, not every time."""
    assert alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE) is not None
    assert alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE) is None
    assert alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE) is None


def test_an_alert_that_ends_stops_being_active():
    alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE)
    assert len(alerts.active()) == 1
    alerts.observe("dad", "APP_w390_cabinCamVisDegraded",
                   {**LIVE, "EndedAt": "2026-09-27T09:00:00Z"})
    assert alerts.active() == []


def test_an_end_time_is_never_unset_by_a_later_republish():
    alerts.observe("dad", "X_a001_thing", {**LIVE, "EndedAt": "2026-09-27T09:00:00Z"})
    alerts.observe("dad", "X_a001_thing", LIVE)          # same start, no end
    assert alerts.active() == []


def test_the_same_fault_recurring_is_a_separate_alert():
    alerts.observe("dad", "X_a001_thing", LIVE)
    later = {**LIVE, "StartedAt": "2026-09-28T10:00:00Z"}
    assert alerts.observe("dad", "X_a001_thing", later) is not None
    assert len(models.list_alerts(active_only=False)) == 2


def test_alerts_are_per_vehicle():
    alerts.observe("dad", "X_a001_thing", LIVE)
    assert alerts.observe("mom", "X_a001_thing", LIVE) is not None
    assert {a["vehicle_key"] for a in alerts.active()} == {"dad", "mom"}
    assert len(alerts.active("dad")) == 1


def test_garbage_payloads_are_ignored():
    assert alerts.observe("dad", "X", "not a dict") is None
    assert alerts.observe("dad", "X", {}) is None
    assert alerts.observe("dad", "X", {"Audiences": ["Customer"]}) is None


# --- through the MQTT router ------------------------------------------------

def test_alert_topic_is_routed(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    pushed = []
    import notify
    monkeypatch.setattr(notify, "send",
                        lambda **kw: (pushed.append(kw.get("title")), True)[1])
    tw.handle_message("telemetry/VIN_DAD/alerts/APP_w390_cabinCamVisDegraded/current",
                      json.dumps(LIVE).encode())
    assert len(alerts.active()) == 1
    assert pushed and "Cabin cam vis degraded" in pushed[0]


def test_alert_does_not_trigger_a_vehicle_state_flush(monkeypatch):
    """Alerts change no position, so they must not masquerade as one."""
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    changed = tw.handle_message(
        "telemetry/VIN_DAD/alerts/APP_w390_cabinCamVisDegraded/current",
        json.dumps(LIVE).encode())
    assert changed is False


def test_history_topic_is_not_treated_as_current(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    tw.handle_message("telemetry/VIN_DAD/alerts/X_a001_thing/history",
                      json.dumps([LIVE]).encode())
    assert alerts.active() == []


def test_a_broken_alert_payload_does_not_break_the_stream(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad"})
    monkeypatch.setattr(alerts, "observe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    caplog.set_level(logging.WARNING)
    tw.handle_message("telemetry/VIN_DAD/alerts/X/current", b"{}")  # must not raise


# --- routes -----------------------------------------------------------------

def test_alerts_api(auth):
    alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE)
    alerts.observe("dad", "CP_a044_lostCommsHVP", ENDED)
    body = auth.get("/api/alerts").get_json()
    assert len(body["active"]) == 1
    assert body["active"][0]["label"] == "Cabin cam vis degraded"
    assert len(body["recent"]) == 2


def test_alerts_api_requires_login(client):
    assert client.get("/api/alerts").status_code == 302


def test_active_alerts_show_on_the_map(auth):
    alerts.observe("dad", "APP_w390_cabinCamVisDegraded", LIVE)
    page = auth.get("/map").data
    assert b"Vehicle alerts" in page
    assert b"Cabin cam vis degraded" in page


def test_no_alert_section_when_there_are_none(auth):
    assert b"Vehicle alerts" not in auth.get("/map").data
