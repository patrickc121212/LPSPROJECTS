"""telemetry_worker: MQTT topic routing + field folding, without a broker.
Also the telemetry config builder in tesla_setup."""
from __future__ import annotations

import json

import pytest

import config
import models
import telemetry_worker as tw


@pytest.fixture(autouse=True)
def _vin_map(monkeypatch):
    monkeypatch.setattr(tw, "VIN_TO_KEY", {"VIN_DAD": "dad", "VIN_LP": "lp", "VIN_MOM": "mom"})
    tw._state.clear()
    yield
    tw._state.clear()


def _msg(vin, field, value):
    return tw.handle_message(f"telemetry/{vin}/v/{field}", json.dumps(value).encode())


def test_location_folds_into_state():
    assert _msg("VIN_DAD", "Location", {"latitude": 37.1, "longitude": -122.2})
    row = tw._state["dad"]
    assert (row["latitude"], row["longitude"]) == (37.1, -122.2)
    assert row["online"] is True


def test_location_wrapped_form_and_string_numbers():
    assert _msg("VIN_LP", "Location", {"locationValue": {"latitude": "1.5", "longitude": "2.5"}})
    assert (tw._state["lp"]["latitude"], tw._state["lp"]["longitude"]) == (1.5, 2.5)


def test_numeric_fields_coerce_from_number_string_and_wrapper():
    _msg("VIN_DAD", "VehicleSpeed", 42.7)
    assert tw._state["dad"]["speed_mph"] == 42.7
    _msg("VIN_DAD", "VehicleSpeed", "12.3")
    assert tw._state["dad"]["speed_mph"] == 12.3
    _msg("VIN_DAD", "BatteryLevel", {"doubleValue": 80.6})
    assert tw._state["dad"]["battery_pct"] == 81


def test_gear_park_zeroes_speed():
    _msg("VIN_DAD", "VehicleSpeed", 30)
    _msg("VIN_DAD", "Gear", "ShiftStateP")
    assert tw._state["dad"]["gear"] == "P"
    assert tw._state["dad"]["speed_mph"] == 0.0
    _msg("VIN_DAD", "Gear", "ShiftStateD")
    assert tw._state["dad"]["gear"] == "D"


def test_connectivity_sets_online_flag():
    _msg("VIN_MOM", "VehicleSpeed", 74)
    tw.handle_message("telemetry/VIN_MOM/connectivity", json.dumps({"Status": "DISCONNECTED"}).encode())
    assert tw._state["mom"]["online"] is False
    assert tw._state["mom"]["speed_mph"] == 0.0, "offline car must not show a stale speed"
    tw.handle_message("telemetry/VIN_MOM/connectivity", json.dumps({"Status": "CONNECTED"}).encode())
    assert tw._state["mom"]["online"] is True


def test_unknown_vin_topic_base_and_garbage_ignored():
    assert not tw.handle_message("telemetry/UNKNOWN/v/Location", b"{}")
    assert not tw.handle_message("other/VIN_DAD/v/Location", b"{}")
    assert not tw.handle_message("telemetry/VIN_DAD", b"{}")
    # Non-JSON payload on a known field shouldn't raise.
    assert tw.handle_message("telemetry/VIN_DAD/v/VehicleName", b"\xff\xfe")
    assert tw._state == {"dad": tw._state["dad"]}


def test_unknown_field_is_harmless():
    assert _msg("VIN_DAD", "SomeFutureSignal", 1)
    assert "SomeFutureSignal" not in tw._state["dad"]


def test_flush_writes_db_and_publishes(events):
    _msg("VIN_DAD", "Location", {"latitude": 37.0, "longitude": -122.0})
    _msg("VIN_DAD", "VehicleSpeed", 25)
    _msg("VIN_DAD", "BatteryLevel", 66)
    tw.flush()
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["latitude"] == 37.0 and row["speed_mph"] == 25.0 and row["battery_pct"] == 66 and row["online"] == 1
    ev = json.loads(events.get_nowait())
    assert ev["event"] == "vehicles"


def test_telemetry_drives_geofence(fired):
    """Location via MQTT -> flush -> geofence tick: entering home fires."""
    import geofence_worker as gw
    g1 = next(d for d in config.GARAGE_DOORS if d.owner_key == "dad")
    far = {"latitude": g1.latitude + 0.01, "longitude": g1.longitude}
    home = {"latitude": g1.latitude, "longitude": g1.longitude}
    _msg("VIN_DAD", "Location", far); tw.flush(); gw._tick()
    assert fired == []
    _msg("VIN_DAD", "Location", home); tw.flush(); gw._tick()
    assert fired == [g1.routine_open]


# --- config builder ---------------------------------------------------------

def test_telemetry_config_builder(tmp_path, monkeypatch):
    import tesla_setup as ts
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(ts, "TELEMETRY_CA_FILE", str(ca))
    monkeypatch.setattr(ts, "TELEMETRY_HOST", "telemetry.example.com")
    cfg = ts.build_telemetry_config()
    assert cfg["hostname"] == "telemetry.example.com" and cfg["port"] == 443
    assert cfg["ca"].startswith("-----BEGIN CERTIFICATE-----")
    assert {"Location", "VehicleSpeed", "BatteryLevel", "Gear"} <= set(cfg["fields"])
    assert cfg["fields"]["Location"]["minimum_delta"] == 10
    assert cfg["alert_types"] == ["service"]


def test_telemetry_config_requires_ca(tmp_path, monkeypatch):
    import tesla_setup as ts
    monkeypatch.setattr(ts, "TELEMETRY_CA_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit):
        ts.build_telemetry_config()


def test_signed_config_is_verifiable(tmp_path, monkeypatch):
    """The JWS we'd POST must verify under the hosted public key."""
    import base64
    import tesla_jws
    import tesla_setup as ts
    from cryptography.hazmat.primitives.asymmetric import ec
    ca = tmp_path / "ca.pem"; ca.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(ts, "TELEMETRY_CA_FILE", str(ca))
    key = ec.generate_private_key(ec.SECP256R1())
    tok = tesla_jws.sign_for_fleet(key, "TelemetryClient", ts.build_telemetry_config())
    h, p, s = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    assert tesla_jws.verify(tesla_jws.public_bytes(key), f"{h}.{p}".encode(), base64.urlsafe_b64decode(pad(s)))
    claims = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert claims["aud"] == "com.tesla.fleet.TelemetryClient" and "hostname" in claims


def test_flush_wakes_the_geofence_worker(monkeypatch):
    """A new position must trigger evaluation immediately, not wait for the
    worker's heartbeat — that 0-30 s wait was most of the auto-open lag."""
    import geofence_worker as gw
    gw._wake.clear()
    _msg("VIN_DAD", "Location", {"latitude": 37.0, "longitude": -122.0})
    tw._dirty.set()
    monkeypatch.setattr(tw, "PUBLISH_MIN_INTERVAL_S", 0.0)
    # Run one pass of the flusher body rather than the infinite loop.
    tw.flush()
    gw.request_tick()
    assert gw._wake.is_set()


def test_seatbelt_accepts_boolean_and_enum_encodings():
    """The proto declares a BuckleStatus enum but real cars send a JSON
    boolean; a mismatch here silently disables buckle-to-open."""
    for raw, expected in [
        (True, "Latched"), (False, "Unlatched"),
        ("BuckleStatusLatched", "Latched"), ("BuckleStatusUnlatched", "Unlatched"),
        ("true", "Latched"), ("false", "Unlatched"),
        (None, None),
    ]:
        assert tw._buckle(raw) == expected, raw
    # Anything unexpected is kept but must not read as latched.
    assert tw._buckle("BuckleStatusFaulted") == "Faulted"


def test_seatbelt_signal_reaches_the_db(events):
    _msg("VIN_DAD", "DriverSeatBelt", True)
    assert tw._state["dad"]["seatbelt"] == "Latched"
    tw.flush()
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["seatbelt"] == "Latched"


def test_seatbelt_is_not_wiped_by_a_source_that_omits_it():
    """The poller doesn't report seatbelt; it must not null out what
    telemetry stored."""
    _msg("VIN_DAD", "DriverSeatBelt", True)
    tw.flush()
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, True)   # no seatbelt kwarg
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["seatbelt"] == "Latched"


# --- a replayed value is not an event ---------------------------------------

def test_a_replayed_position_is_kept_but_not_treated_as_news():
    """Mosquitto replays the last retained value on connect. On 2026-09-30 a
    restart re-ingested a 13-hour-old position and recorded it as current,
    inventing a history point and making a sleeping car look freshly seen."""
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode(),
                      retained=True)
    row = tw._state["dad"]
    assert row["latitude"] == 37.0, "still worth showing as the last known spot"
    assert row["replayed"] is True
    assert row.get("position_at") is None, "we cannot know when it was true"


def test_a_live_message_clears_the_replayed_mark():
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode(),
                      retained=True)
    tw.handle_message("telemetry/VIN_DAD/v/VehicleSpeed", b"12", retained=False)
    assert tw._state["dad"]["replayed"] is False


def test_replayed_data_creates_no_trip_or_history(monkeypatch):
    import trips
    seen = []
    monkeypatch.setattr(trips, "observe", lambda *a, **k: seen.append(a))
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode(),
                      retained=True)
    tw.flush()
    assert seen == [], "a replay must not start a trip or store a breadcrumb"


def test_replayed_data_does_not_drive_the_charging_machine(monkeypatch):
    import charging
    seen = []
    monkeypatch.setattr(charging, "observe", lambda *a, **k: seen.append(a))
    tw.handle_message("telemetry/VIN_DAD/v/DetailedChargeState",
                      json.dumps("DetailedChargeStateCharging").encode(), retained=True)
    tw.flush()
    assert seen == []


def test_live_data_still_drives_everything(monkeypatch):
    import trips
    seen = []
    monkeypatch.setattr(trips, "observe", lambda *a, **k: seen.append(a))
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode(),
                      retained=False)
    tw.flush()
    assert len(seen) == 1


def test_position_age_is_tracked_separately_from_last_contact(monkeypatch):
    """A sleeping car still sends battery and charge state, which refreshes
    "last heard"; only a genuine move refreshes the position time."""
    # A fixed, advancing clock: two real calls can land in the same tick.
    ticks = iter([1000.0, 1001.0, 1002.0, 1003.0, 1004.0, 1005.0])
    monkeypatch.setattr(tw.time, "time", lambda: next(ticks))
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode())
    first = tw._state["dad"]["position_at"]
    assert first is not None
    tw.handle_message("telemetry/VIN_DAD/v/BatteryLevel", b"77")
    assert tw._state["dad"]["position_at"] == first, "battery is not movement"
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode())
    assert tw._state["dad"]["position_at"] == first, "same spot is not movement"
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.5, "longitude": -122.0}).encode())
    assert tw._state["dad"]["position_at"] > first


def test_position_age_reaches_the_database():
    tw.handle_message("telemetry/VIN_DAD/v/Location",
                      json.dumps({"latitude": 37.0, "longitude": -122.0}).encode())
    tw.flush()
    row = {r["vehicle_key"]: r for r in models.all_vehicle_states()}["dad"]
    assert row["position_at"] is not None
