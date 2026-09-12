"""
Tesla Fleet API poller. Runs every TESLA_POLL_INTERVAL_S, writes vehicle
state to SQLite, and publishes a 'vehicles' SSE event.

In production this hits the Tesla Fleet API with a registered partner
token. For local development we fall back to a deterministic simulator
that walks each vehicle around the house so the map and geofence code
have something realistic to chew on.

The simulator is gated on TRACKER_SIMULATE=1 (default ON when no
TESLA_ACCESS_TOKEN env var is set), so flipping one env var turns it
off and routes through the real Fleet API client.
"""
from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from typing import Any

import config
import models
from eventbus import bus

log = logging.getLogger("tesla_poller")

# Toggle the simulator off by setting TRACKER_SIMULATE=0 AND providing a
# real TESLA_ACCESS_TOKEN. Otherwise we simulate.
SIMULATE = os.getenv("TRACKER_SIMULATE", "1" if not os.getenv("TESLA_ACCESS_TOKEN") else "0") == "1"

# Simulator state: each vehicle orbits its own garage's geofence center.
_sim_state: dict[str, dict[str, float]] = {}


def _init_sim() -> None:
    for v in config.VEHICLES:
        # Park each car near its owner's garage so the geofence worker
        # has at least one owner-in-zone scenario at startup.
        owner_door = next((g for g in config.GARAGE_DOORS if g.owner_key == v.key), None)
        if owner_door is None:
            owner_door = config.GARAGE_DOORS[0]
        _sim_state[v.key] = {
            "lat": owner_door.latitude,
            "lon": owner_door.longitude,
            "heading": random.uniform(0, 2 * math.pi),
            "speed_mph": 0.0,
            "battery": random.randint(60, 95),
        }


def _step_sim() -> list[dict[str, Any]]:
    """One tick of the simulator. Returns a list of vehicle states."""
    now = time.time()
    out: list[dict[str, Any]] = []
    for v in config.VEHICLES:
        s = _sim_state[v.key]
        # Occasionally pick a new heading and drive for a bit, then stop.
        if s["speed_mph"] < 1 and random.random() < 0.15:
            s["heading"] = random.uniform(0, 2 * math.pi)
            s["speed_mph"] = random.uniform(15, 55)
        elif s["speed_mph"] > 0 and random.random() < 0.10:
            s["speed_mph"] = max(0.0, s["speed_mph"] - random.uniform(5, 25))

        # Convert mph + heading to a tiny lat/lon delta.
        # Good enough for visual jitter on a city-block scale.
        if s["speed_mph"] > 0:
            dlat, dlon = _mp_to_delta(s["speed_mph"], s["heading"])
            s["lat"] += dlat
            s["lon"] += dlon

        # Battery slowly drains while moving; tiny regen while stopped.
        if s["speed_mph"] > 0:
            s["battery"] = max(5, s["battery"] - 0.05)
        else:
            s["battery"] = min(100, s["battery"] + 0.01)

        out.append({
            "vehicle_key": v.key,
            "latitude": s["lat"],
            "longitude": s["lon"],
            "speed_mph": round(s["speed_mph"], 1),
            "battery_pct": int(s["battery"]),
            "online": True,
        })
    return out


def _mp_to_delta(mph: float, heading_rad: float) -> tuple[float, float]:
    """Rough conversion: 1 deg lat ~= 69 miles, 1 deg lon ~= 69*cos(lat) miles."""
    miles_per_tick = mph * (config.TESLA_POLL_INTERVAL_S / 3600.0)
    dlat = miles_per_tick * math.cos(heading_rad) / 69.0
    # Use a representative mid-latitude for the cos factor.
    dlon = miles_per_tick * math.sin(heading_rad) / (69.0 * math.cos(math.radians(37.775)))
    return dlat, dlon


# --- Real Tesla Fleet API path ---------------------------------------------

def _fetch_real() -> list[dict[str, Any]]:
    """Real Tesla Fleet API call. Lazy-imported so the simulator path
    doesn't require the tesla-python SDK installed."""
    try:
        from tesla_fleet_api import TeslaFleetApi  # type: ignore
    except ImportError:
        log.warning("tesla_fleet_api SDK not installed; staying in simulator mode.")
        return _step_sim()

    token = os.environ["TESLA_ACCESS_TOKEN"]
    out: list[dict[str, Any]] = []
    for v in config.VEHICLES:
        if not v.tesla_vin:
            continue
        try:
            client = TeslaFleetApi(token=token)
            data = client.vehicles.get(vin=v.tesla_vin)["vehicle_data"]
            loc = data.get("drive_state", {}) or {}
            charge = data.get("charge_state", {}) or {}
            out.append({
                "vehicle_key": v.key,
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
                "speed_mph": (loc.get("speed") or 0) * 0.621371,
                "battery_pct": charge.get("battery_level"),
                "online": data.get("state") == "online",
            })
        except Exception as exc:  # noqa: BLE001 — best-effort, back off
            log.warning("Tesla API error for %s: %s", v.key, exc)
            out.append({"vehicle_key": v.key, "online": False})
    return out


def _publish(states: list[dict[str, Any]]) -> None:
    for s in states:
        models.upsert_vehicle_state(
            s["vehicle_key"],
            s.get("latitude"),
            s.get("longitude"),
            s.get("speed_mph"),
            s.get("battery_pct"),
            s.get("online", False),
        )
    # Always publish the full snapshot so a fresh client gets everything.
    full = models.all_vehicle_states()
    bus.publish("vehicles", full)


def _loop() -> None:
    if SIMULATE:
        _init_sim()
        log.info("Tesla poller running in SIMULATOR mode (set TRACKER_SIMULATE=0 to disable).")
    else:
        log.info("Tesla poller running against the real Fleet API.")

    backoff = 1.0
    while True:
        try:
            states = _step_sim() if SIMULATE else _fetch_real()
            _publish(states)
            backoff = 1.0
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.exception("Poller iteration failed: %s", exc)
            backoff = min(backoff * 2, 60.0)
        time.sleep(config.TESLA_POLL_INTERVAL_S * backoff)


def start_background() -> None:
    t = threading.Thread(target=_loop, name="tesla-poller", daemon=True)
    t.start()
