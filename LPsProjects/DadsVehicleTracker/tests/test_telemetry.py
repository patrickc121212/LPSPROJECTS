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
    tw.handle_message("telemetry/VIN_MOM/connectivity", json.dumps({"Status": "DISCONNECTED"}).encode())
    assert tw._state["mom"]["online"] is False
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
    g1 = config.GARAGE_DOORS_BY_KEY["garage1"]
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
