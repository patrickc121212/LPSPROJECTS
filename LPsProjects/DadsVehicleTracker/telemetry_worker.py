"""
Fleet Telemetry consumer. The cars stream to the fleet-telemetry server
(telemetry/docker-compose.yml), which republishes every signal to MQTT:

    telemetry/<VIN>/v/<Field>        JSON value   e.g. {"latitude":..,"longitude":..}
    telemetry/<VIN>/connectivity     {"Status": "CONNECTED"|"DISCONNECTED", ...}

We subscribe, map VIN -> vehicle key, fold the per-field messages into the
vehicle_state row, and publish a 'vehicles' SSE snapshot. Compared to the
poller this is push (sub-second from the car), costs ~nothing at Tesla's
per-signal pricing, and never keeps a parked car awake.

Field handling is in `apply_signal()` so it can be unit-tested without a
broker. Values arrive as JSON; per Tesla's note the car may send a number
as 12.3 or "12.3" depending on firmware, so we coerce.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import config
import models
from eventbus import bus

log = logging.getLogger("telemetry_worker")

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "telemetry")

# Publish an SSE snapshot at most this often; Location can tick every 5s per
# car and each field is its own MQTT message, so coalesce.
PUBLISH_MIN_INTERVAL_S = float(os.getenv("TELEMETRY_PUBLISH_MIN_S", "1.0"))

VIN_TO_KEY: dict[str, str] = {v.tesla_vin: v.key for v in config.VEHICLES if v.tesla_vin}

# Latest known values per vehicle key, merged from individual field messages.
_state: dict[str, dict[str, Any]] = {}
_dirty = threading.Event()
_lock = threading.Lock()


def _num(v: Any) -> float | None:
    """Telemetry numbers may be JSON numbers, numeric strings, or wrapped
    like {"doubleValue": 12.3} on some firmware. Return a float or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    if isinstance(v, dict):
        for k in ("doubleValue", "floatValue", "intValue", "longValue", "value"):
            if k in v:
                return _num(v[k])
    return None


def _location(v: Any) -> tuple[float | None, float | None]:
    if isinstance(v, dict):
        inner = v.get("locationValue", v)
        return _num(inner.get("latitude")), _num(inner.get("longitude"))
    return None, None


def apply_signal(vehicle_key: str, field: str, value: Any) -> dict[str, Any]:
    """Fold one telemetry field into the in-memory state for a vehicle and
    return the updated row. Unknown fields are ignored (we only subscribe
    to what we configured, but a firmware may add extras)."""
    with _lock:
        row = _state.setdefault(vehicle_key, {"vehicle_key": vehicle_key, "online": True})
        if field == "Location":
            lat, lon = _location(value)
            if lat is not None and lon is not None:
                row["latitude"], row["longitude"] = lat, lon
        elif field == "VehicleSpeed":
            mph = _num(value)
            row["speed_mph"] = None if mph is None else max(0.0, mph)
        elif field == "BatteryLevel":
            pct = _num(value)
            row["battery_pct"] = None if pct is None else int(round(pct))
        elif field == "Gear":
            row["gear"] = str(value).replace("ShiftState", "") if value is not None else None
            # A car in Park isn't moving even if the last speed sample lingered.
            if row["gear"] in ("P", "Invalid", "None"):
                row["speed_mph"] = 0.0
        elif field == "DetailedChargeState":
            row["charge_state"] = str(value).replace("DetailedChargeState", "") if value is not None else None
        elif field == "VehicleName":
            row["name"] = str(value)
        row["updated_at"] = time.time()
        return dict(row)


def apply_connectivity(vehicle_key: str, payload: dict[str, Any]) -> None:
    with _lock:
        row = _state.setdefault(vehicle_key, {"vehicle_key": vehicle_key})
        row["online"] = str(payload.get("Status", "")).upper() == "CONNECTED"
        if not row["online"]:
            # The car stops reporting when it sleeps; its last speed sample
            # would otherwise linger on the map ("74 mph" while parked).
            row["speed_mph"] = 0.0
        row["updated_at"] = time.time()


def handle_message(topic: str, payload: bytes) -> bool:
    """Route one MQTT message. Returns True if it changed vehicle state."""
    parts = topic.split("/")
    # telemetry/<VIN>/v/<Field>   or   telemetry/<VIN>/connectivity
    if len(parts) < 3 or parts[0] != TOPIC_BASE:
        return False
    vin = parts[1]
    key = VIN_TO_KEY.get(vin)
    if key is None:
        return False
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = payload.decode("utf-8", "replace")
    if parts[2] == "v" and len(parts) >= 4:
        apply_signal(key, parts[3], value)
        return True
    if parts[2] == "connectivity" and isinstance(value, dict):
        apply_connectivity(key, value)
        return True
    return False


def flush() -> None:
    """Write merged state to SQLite and push one SSE snapshot."""
    with _lock:
        rows = [dict(r) for r in _state.values()]
    for r in rows:
        models.upsert_vehicle_state(
            r["vehicle_key"],
            r.get("latitude"), r.get("longitude"),
            r.get("speed_mph"), r.get("battery_pct"),
            bool(r.get("online", False)),
        )
    bus.publish("vehicles", models.all_vehicle_states())


def _flusher() -> None:
    while True:
        _dirty.wait()
        time.sleep(PUBLISH_MIN_INTERVAL_S)  # coalesce a burst of fields
        _dirty.clear()
        try:
            flush()
        except Exception as exc:  # noqa: BLE001
            log.exception("telemetry flush failed: %s", exc)


def _run_mqtt() -> None:
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected to %s:%s (rc=%s); subscribing %s/+/#", MQTT_HOST, MQTT_PORT, reason_code, TOPIC_BASE)
        client.subscribe(f"{TOPIC_BASE}/+/v/#", qos=1)
        client.subscribe(f"{TOPIC_BASE}/+/connectivity", qos=1)

    def on_message(client, userdata, msg):
        try:
            if handle_message(msg.topic, msg.payload):
                _dirty.set()
        except Exception as exc:  # noqa: BLE001
            log.warning("bad telemetry message on %s: %s", msg.topic, exc)

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        log.warning("MQTT disconnected (rc=%s); paho will reconnect", reason_code)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dads-tracker", clean_session=False)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT connect failed (%s); retrying in 10s", exc)
            time.sleep(10)


def start_background() -> None:
    if not VIN_TO_KEY:
        log.error("No TESLA_VIN_* set; telemetry worker has nothing to map. Idle.")
        return
    log.info("Telemetry worker: %d VINs mapped, broker %s:%s", len(VIN_TO_KEY), MQTT_HOST, MQTT_PORT)
    threading.Thread(target=_flusher, name="telemetry-flush", daemon=True).start()
    threading.Thread(target=_run_mqtt, name="telemetry-mqtt", daemon=True).start()
