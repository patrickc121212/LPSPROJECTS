"""
Geofence worker. Reads vehicle_state, computes haversine distance to each
garage door's center, and triggers the door's "open" Google Assistant
Routine the first time a permitted vehicle ENTERS the geofence.

Permissions:
  - The door's owner vehicle always opens it.
  - Other vehicles may open if the door's allowlist (in SQLite) contains them.

Firing is EDGE-triggered: we remember whether each (vehicle, door) pair was
inside on the previous tick and only fire on the outside -> inside
transition. A car parked in the garage all night therefore fires once, not
once per debounce window. GEOFENCE_DEBOUNCE_S is a second guard against
GPS jitter flapping a car across the fence line.

We trigger the routine by POSTing to a Google Home webhook
(GOOGLE_ROUTINE_WEBHOOK_URL). In v1 the body is just the routine name;
you set that webhook up once in IFTTT / Google Home webhooks / a small
App Script. The Shelly Cloud skill handles the actual relay flip.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import Any

import config
import models
from eventbus import bus

log = logging.getLogger("geofence_worker")

WEBHOOK_URL = os.getenv("GOOGLE_ROUTINE_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.getenv("GOOGLE_ROUTINE_WEBHOOK_TOKEN", "")

# (vehicle_key, door_key) -> last fire timestamp
_last_fire: dict[tuple[str, str], float] = {}
# (vehicle_key, door_key) -> was the vehicle inside the fence last tick?
# Missing key == unknown; we treat the first observation as a baseline and
# never fire on it, so a restart with a car already parked inside is silent.
_inside: dict[tuple[str, str], bool] = {}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6_371_000.0  # earth radius
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _trigger_routine(routine_name: str) -> None:
    """POST to the Google Home webhook. No-op in dev unless the URL is set."""
    if not WEBHOOK_URL:
        log.info("[dry-run] would fire routine: %s", routine_name)
        return
    try:
        import urllib.request
        body = json.dumps({"routine": routine_name}).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=4).read()
        log.info("Fired routine: %s", routine_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Routine webhook failed for %s: %s", routine_name, exc)


def evaluate(
    states: dict[str, dict[str, Any]],
    allowlists: dict[str, set[str]],
    now: float,
) -> list[tuple[str, str]]:
    """Pure decision step. Returns the (vehicle_key, door_key) pairs that
    should trigger an auto-open this tick, and updates the edge/debounce
    state. Split from _tick so it can be unit-tested without SQLite."""
    fire: list[tuple[str, str]] = []
    for door in config.GARAGE_DOORS:
        allow = allowlists.get(door.key, set())
        for v in config.VEHICLES:
            key = (v.key, door.key)
            s = states.get(v.key)
            if not s or s.get("latitude") is None or s.get("longitude") is None:
                # No fix: keep whatever we knew; don't flip to "outside",
                # otherwise a GPS dropout followed by a fix re-fires.
                continue
            dist = haversine_m(s["latitude"], s["longitude"], door.latitude, door.longitude)
            inside = dist <= door.radius_m
            was_inside = _inside.get(key)
            _inside[key] = inside

            if not inside or was_inside is None or was_inside:
                # Outside, first observation (baseline), or still inside.
                continue
            # Outside -> inside transition.
            if not config.allowed_for_door(v.key, door.key, allow):
                continue
            last = _last_fire.get(key)
            if last is not None and now - last < config.GEOFENCE_DEBOUNCE_S:
                continue
            _last_fire[key] = now
            fire.append(key)
    return fire


def _tick() -> None:
    states = {s["vehicle_key"]: s for s in models.all_vehicle_states()}
    allowlists = {d.key: models.get_allowlist(d.key) for d in config.GARAGE_DOORS}
    for vehicle_key, door_key in evaluate(states, allowlists, time.time()):
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        log.info("Geofence enter: %s -> %s", vehicle_key, door.label)
        _trigger_routine(door.routine_open)
        models.upsert_door_state(door.key, True)
        bus.publish("doors", models.all_door_states())


def _loop() -> None:
    log.info("Geofence worker started.")
    while True:
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001
            log.exception("Geofence tick failed: %s", exc)
        time.sleep(config.TESLA_POLL_INTERVAL_S)


def start_background() -> None:
    t = threading.Thread(target=_loop, name="geofence-worker", daemon=True)
    t.start()
