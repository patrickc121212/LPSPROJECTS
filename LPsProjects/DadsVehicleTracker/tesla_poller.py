"""
Tesla Fleet API poller. Runs every TESLA_POLL_INTERVAL_S, writes vehicle
state to SQLite, and publishes a 'vehicles' SSE event.

In production this hits the Tesla Fleet API with a registered partner
token. For local development we fall back to a deterministic simulator
that drives each vehicle on a short round trip from its garage so the map
and geofence code have something realistic to chew on.

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

# Simulator: each vehicle runs a scripted round trip from its owner's
# garage — park, drive ~600 m out, park, drive back, park — so the map
# always shows movement and the geofence auto-open fires on every return.
# Vehicles are phase-offset so the three doors don't all fire at once.
_sim_state: dict[str, dict[str, Any]] = {}

SIM_TRIP_M = 600.0          # how far each car drives from home
SIM_SPEED_MPH = 25.0        # cruising speed while driving
SIM_PARK_TICKS = 6          # ticks parked at each end of the trip


def _init_sim() -> None:
    for i, v in enumerate(config.VEHICLES):
        # Park each car at its owner's garage so the geofence worker sees
        # the owner-in-zone baseline at startup.
        owner_door = next((g for g in config.GARAGE_DOORS if g.owner_key == v.key), None)
        if owner_door is None:
            owner_door = config.GARAGE_DOORS[0]
        _sim_state[v.key] = {
            "home_lat": owner_door.latitude,
            "home_lon": owner_door.longitude,
            "lat": owner_door.latitude,
            "lon": owner_door.longitude,
            "heading": (i * 2 * math.pi / 3) + 0.4,   # each car leaves a different way
            "speed_mph": 0.0,
            "battery": float(random.randint(60, 95)),
            # Trip phase machine: parked_home -> out -> parked_away -> back
            "phase": "parked_home",
            "ticks_in_phase": -i * 3,                  # stagger departures
        }


def _step_per_tick_m() -> float:
    return SIM_SPEED_MPH * 1609.34 * (config.TESLA_POLL_INTERVAL_S / 3600.0)


def _step_sim() -> list[dict[str, Any]]:
    """One tick of the simulator. Returns a list of vehicle states."""
    out: list[dict[str, Any]] = []
    step_m = _step_per_tick_m()
    for v in config.VEHICLES:
        s = _sim_state[v.key]
        s["ticks_in_phase"] += 1
        phase = s["phase"]

        if phase in ("parked_home", "parked_away"):
            s["speed_mph"] = 0.0
            if s["ticks_in_phase"] >= SIM_PARK_TICKS:
                s["phase"] = "out" if phase == "parked_home" else "back"
                s["ticks_in_phase"] = 0
        else:
            s["speed_mph"] = SIM_SPEED_MPH
            direction = s["heading"] if phase == "out" else s["heading"] + math.pi
            dlat, dlon = _m_to_delta(step_m, direction, s["home_lat"])
            s["lat"] += dlat
            s["lon"] += dlon
            dist_home = _approx_dist_m(s["lat"], s["lon"], s["home_lat"], s["home_lon"])
            if phase == "out" and dist_home >= SIM_TRIP_M:
                s["phase"], s["ticks_in_phase"] = "parked_away", 0
            elif phase == "back" and (dist_home < step_m or dist_home < 5.0):
                # Snap to the driveway so we don't overshoot past the fence.
                s["lat"], s["lon"] = s["home_lat"], s["home_lon"]
                s["phase"], s["ticks_in_phase"] = "parked_home", 0

        # Battery drains while moving; tiny regen while parked.
        s["battery"] = max(5.0, s["battery"] - 0.05) if s["speed_mph"] > 0 else min(100.0, s["battery"] + 0.01)

        out.append({
            "vehicle_key": v.key,
            "latitude": s["lat"],
            "longitude": s["lon"],
            "speed_mph": round(s["speed_mph"], 1),
            "battery_pct": int(s["battery"]),
            "online": True,
        })
    return out


def _m_to_delta(meters: float, heading_rad: float, at_lat: float) -> tuple[float, float]:
    """Metres along a heading -> (dlat, dlon) in degrees."""
    dlat = meters * math.cos(heading_rad) / 111_320.0
    dlon = meters * math.sin(heading_rad) / (111_320.0 * math.cos(math.radians(at_lat)))
    return dlat, dlon


def _approx_dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance; plenty accurate at neighbourhood scale."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.sqrt(x * x + y * y) * 6_371_000.0


# --- Real Tesla Fleet API path ---------------------------------------------

class RateLimitedError(Exception):
    """Raised out of _fetch_real when Tesla answered 429 so the loop can
    back off the whole poll cycle instead of just logging per-vehicle."""


# Fields we ask the Fleet API for. Firmware 2023.38+ won't return a
# position unless location_data is explicitly requested.
_ENDPOINTS = ["drive_state", "charge_state", "location_data"]


async def _fetch_real_async(token: str, region: str) -> list[dict[str, Any]]:
    import aiohttp
    from tesla_fleet_api import TeslaFleetApi
    from tesla_fleet_api.exceptions import RateLimited, TeslaFleetError

    out: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        api = TeslaFleetApi(session, access_token=token, region=region)
        for v in config.VEHICLES:
            if not v.tesla_vin:
                continue
            try:
                resp = await api.vehicles.createFleet(v.tesla_vin).vehicle_data(_ENDPOINTS)
                data = resp.get("response") or {}
                drive = data.get("drive_state") or {}
                charge = data.get("charge_state") or {}
                speed_kph = drive.get("speed")
                out.append({
                    "vehicle_key": v.key,
                    "latitude": drive.get("latitude"),
                    "longitude": drive.get("longitude"),
                    "speed_mph": None if speed_kph is None else speed_kph * 0.621371,
                    "battery_pct": charge.get("battery_level"),
                    "online": data.get("state") == "online",
                })
            except RateLimited as exc:
                # Stop hitting the API this cycle; the loop backs off.
                raise RateLimitedError(str(exc)) from exc
            except TeslaFleetError as exc:
                # Asleep / offline vehicles raise here; mark offline and move on.
                log.warning("Tesla API error for %s: %s", v.key, exc)
                out.append({"vehicle_key": v.key, "online": False})
    return out


def _fetch_real() -> list[dict[str, Any]]:
    """Real Tesla Fleet API call. The SDK is asyncio-based, so we spin a
    private event loop for each poll; fine at a 30s cadence."""
    import asyncio
    token = os.environ["TESLA_ACCESS_TOKEN"]
    return asyncio.run(_fetch_real_async(token, config.TESLA_REGION))


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


MAX_BACKOFF = 16.0  # multiplier on TESLA_POLL_INTERVAL_S (30s * 16 = 8 min)


def next_backoff(current: float, rate_limited: bool) -> float:
    """Exponential backoff multiplier; 429s back off harder than generic
    errors because Tesla's limits are per-day and we don't want to burn them."""
    step = 4.0 if rate_limited else 2.0
    return min(current * step, MAX_BACKOFF)


def _loop() -> None:
    if SIMULATE:
        _init_sim()
        log.info("Tesla poller running in SIMULATOR mode (set TRACKER_SIMULATE=0 to disable).")
    else:
        if not os.getenv("TESLA_ACCESS_TOKEN"):
            log.error("TRACKER_SIMULATE=0 but TESLA_ACCESS_TOKEN is empty; poller idle.")
            return
        log.info("Tesla poller running against the real Fleet API (region=%s).", config.TESLA_REGION)

    backoff = 1.0
    while True:
        try:
            states = _step_sim() if SIMULATE else _fetch_real()
            _publish(states)
            backoff = 1.0
        except RateLimitedError as exc:
            backoff = next_backoff(backoff, rate_limited=True)
            log.warning("Tesla rate limited (%s); backing off x%.0f", exc, backoff)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            backoff = next_backoff(backoff, rate_limited=False)
            log.exception("Poller iteration failed (backoff x%.0f): %s", backoff, exc)
        time.sleep(config.TESLA_POLL_INTERVAL_S * backoff)


def start_background() -> None:
    t = threading.Thread(target=_loop, name="tesla-poller", daemon=True)
    t.start()
