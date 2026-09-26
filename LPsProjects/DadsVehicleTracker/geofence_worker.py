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

How a door is actually actuated (local Shelly pulse, Google Routine
webhook, or dry-run) is door_control's job.

Doors on the Shelly path are toggle-only, so firing at one that is already
open would CLOSE it on a car pulling in. `evaluate` therefore skips any
door we currently believe to be open — see door_control.believed_open.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import config
import door_control
import models
from eventbus import bus

log = logging.getLogger("geofence_worker")

# Set by request_tick() when a fresh position lands, so the worker reacts to
# a car arriving instead of waiting out its next scheduled sweep.
_wake = threading.Event()
# Set to ask the loop to exit; lets tests run the real loop without leaving
# a daemon thread ticking against a torn-down database.
_stop = threading.Event()

# door_key -> when its owner was first seen outside the fence. Reset the
# moment they come back, so "away for N seconds" means this trip, not ever.
_away_since: dict[str, float] = {}
# Doors already auto-closed for the current departure; cleared on return so
# one trip produces at most one close pulse.
_auto_closed: set[str] = set()

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


def evaluate(
    states: dict[str, dict[str, Any]],
    allowlists: dict[str, set[str]],
    now: float,
    door_states: dict[str, dict] | None = None,
) -> list[tuple[str, str]]:
    """Pure decision step. Returns the (vehicle_key, door_key) pairs that
    should trigger an auto-open this tick, and updates the edge/debounce
    state. Split from _tick so it can be unit-tested without SQLite."""
    fire: list[tuple[str, str]] = []
    door_states = door_states or {}
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
            if door_control.is_open(door, door_states, now):
                # A Shelly-wired opener is toggle-only, so pulsing a door
                # that is already up would shut it on the arriving car.
                # The crossing is already consumed by _inside above, so the
                # car must leave and return to arm auto-open again.
                log.info("Skipping auto-open of %s for %s: believed already open",
                         door.label, v.key)
                continue
            _last_fire[key] = now
            fire.append(key)
    return fire


def _owner_away_seconds(door: config.GarageDoor, states: dict[str, dict[str, Any]],
                        now: float) -> float | None:
    """How long the door's owner has been outside the fence, or None if they
    are inside / have no fix. Also maintains the per-door away timer."""
    s = states.get(door.owner_key)
    if not s or s.get("latitude") is None or s.get("longitude") is None:
        return None  # no fix: don't start or advance the timer
    inside = haversine_m(s["latitude"], s["longitude"],
                         door.latitude, door.longitude) <= door.radius_m
    if inside:
        _away_since.pop(door.key, None)
        _auto_closed.discard(door.key)
        return None
    since = _away_since.setdefault(door.key, now)
    return max(0.0, now - since)


def departure_actions(
    states: dict[str, dict[str, Any]],
    door_states: dict[str, dict],
    now: float,
) -> tuple[list[str], list[str]]:
    """What to do about doors whose owner has driven away.

    Returns (to_close, to_assume_closed).

    With AUTO_CLOSE_ENABLED we pulse the door shut once the owner has been
    gone for AUTO_CLOSE_DELAY_S. Without a position sensor that pulse acts on
    a belief, and a wrong belief OPENS the door at an empty house — the
    accepted trade-off recorded in Plan.md.

    With auto-close off we only clear the stale belief, so the owner's next
    arrival can auto-open (a door essentially always closes behind a
    departing car).
    """
    to_close: list[str] = []
    to_assume: list[str] = []
    for door in config.GARAGE_DOORS:
        away_s = _owner_away_seconds(door, states, now)
        if away_s is None:
            continue
        if not door_control.is_open(door, door_states, now):
            continue
        if not config.AUTO_CLOSE_ENABLED:
            to_assume.append(door.key)
            continue
        if door.key in _auto_closed:
            continue
        if away_s >= config.AUTO_CLOSE_DELAY_S:
            # Mark here, not at the call site: one attempt per departure
            # holds even if the caller retries or the pulse fails.
            _auto_closed.add(door.key)
            to_close.append(door.key)
    return to_close, to_assume


def _tick() -> None:
    states = {s["vehicle_key"]: s for s in models.all_vehicle_states()}
    allowlists = {d.key: models.get_allowlist(d.key) for d in config.GARAGE_DOORS}
    door_states = {d["door_key"]: d for d in models.all_door_states()}
    now = time.time()
    changed = False

    to_close, to_assume = departure_actions(states, door_states, now)

    for door_key in to_close:
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        how = "sensor" if door_control.has_sensor(door) else "inferred state"
        log.info("Auto-closing %s: owner away and door open per %s", door.label, how)
        result = door_control.actuate(door, "close")
        if result["ok"]:
            models.upsert_door_state(door_key, False)
            changed = True
        else:
            log.warning("Auto-close of %s FAILED (%s): %s",
                        door.label, result["via"], result["detail"])

    for door_key in to_assume:
        log.info("Assuming %s closed: its owner has left the geofence",
                 config.GARAGE_DOORS_BY_KEY[door_key].label)
        models.upsert_door_state(door_key, False)
        changed = True

    if changed:
        door_states = {d["door_key"]: d for d in models.all_door_states()}

    for vehicle_key, door_key in evaluate(states, allowlists, now, door_states):
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        log.info("Geofence enter: %s -> %s", vehicle_key, door.label)
        result = door_control.actuate(door, "open")
        if result["ok"]:
            models.upsert_door_state(door.key, True)
            changed = True
        else:
            log.warning("Auto-open of %s FAILED (%s): %s",
                        door.label, result["via"], result["detail"])
    if changed:
        bus.publish("doors", models.all_door_states())


def stop() -> None:
    """Ask the worker loop to exit after its current pass."""
    _stop.set()
    _wake.set()


def request_tick() -> None:
    """Ask the worker to evaluate now. Called by whichever source supplies
    positions (telemetry flush, poller publish) so a car arriving home is
    acted on within milliseconds rather than at the next sweep."""
    _wake.set()


def _loop() -> None:
    log.info("Geofence worker started (event-driven, %ss heartbeat).",
             config.GEOFENCE_INTERVAL_S)
    while not _stop.is_set():
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001
            log.exception("Geofence tick failed: %s", exc)
        # Wake on a new position, or sweep anyway on the heartbeat so a
        # stale belief still expires when no car is reporting.
        _wake.wait(timeout=config.GEOFENCE_INTERVAL_S)
        _wake.clear()
    log.info("Geofence worker stopped.")


def start_background() -> None:
    t = threading.Thread(target=_loop, name="geofence-worker", daemon=True)
    t.start()
