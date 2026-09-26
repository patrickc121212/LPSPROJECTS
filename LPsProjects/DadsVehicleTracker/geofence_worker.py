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
# vehicle_key -> (lat, lon, since) of where it has been sitting. Reset when
# it moves further than PARKED_JITTER_M from that reference.
_still_ref: dict[str, tuple[float, float, float]] = {}
# Doors closed because the owner parked; cleared when the car moves again.
_parked_closed: set[str] = set()
# Doors opened because the owner buckled up; cleared when they unbuckle or
# drive away, so one buckle produces one open.
_depart_opened: set[str] = set()

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
            if door_control.in_cooldown(door.key, now):
                log.info("Skipping auto-open of %s: commanded %.0fs ago", door.label,
                         door_control.seconds_since_action(door.key, now) or 0)
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
    """How long the owner has been outside the door's CLOSE fence
    (config.close_radius), or None if they are inside / have no fix. Also
    maintains the per-door away timer."""
    s = states.get(door.owner_key)
    if not s or s.get("latitude") is None or s.get("longitude") is None:
        return None  # no fix: don't start or advance the timer
    # Departure uses its own, usually tighter, fence: crossing it should be
    # visible in the mirror rather than happening a street away.
    inside = haversine_m(s["latitude"], s["longitude"],
                         door.latitude, door.longitude) <= config.close_radius(door)
    if inside:
        _away_since.pop(door.key, None)
        _auto_closed.discard(door.key)
        return None
    since = _away_since.setdefault(door.key, now)
    return max(0.0, now - since)


def stationary_seconds(vehicle_key: str, lat: float, lon: float, now: float) -> float:
    """How long this vehicle has been sitting within PARKED_JITTER_M of the
    spot we first saw it at. Any real move resets the reference and clock."""
    ref = _still_ref.get(vehicle_key)
    if ref is None or haversine_m(lat, lon, ref[0], ref[1]) > config.PARKED_JITTER_M:
        _still_ref[vehicle_key] = (lat, lon, now)
        return 0.0
    return max(0.0, now - ref[2])


def _buckled(state: dict[str, Any] | None) -> bool:
    return bool(state) and str(state.get("seatbelt") or "") == "Latched"


def doors_to_open_for_departure(
    states: dict[str, dict[str, Any]],
    door_states: dict[str, dict],
    now: float,
) -> list[str]:
    """Doors to open because their owner has buckled up at the garage.

    Buckling is the earliest signal that is unambiguous about intent — a
    driver in the seat might just be fetching something, but nobody belts in
    without leaving. It gives roughly the time a door needs to travel.
    """
    out: list[str] = []
    for door in config.GARAGE_DOORS:
        s = states.get(door.owner_key)
        if not s or s.get("latitude") is None or s.get("longitude") is None:
            continue
        at_garage = haversine_m(s["latitude"], s["longitude"],
                                door.latitude, door.longitude) <= config.DEPART_OPEN_RADIUS_M
        if not (at_garage and _buckled(s)):
            _depart_opened.discard(door.key)  # re-arm for the next buckle
            continue
        if door.key in _depart_opened:
            continue
        if door_control.in_cooldown(door.key, now):
            continue
        if door_control.is_open(door, door_states, now):
            continue
        _depart_opened.add(door.key)
        # Restart the dwell clock. Otherwise the parked-close rule, whose
        # timer has been running since the car arrived, fires the instant
        # anything lifts its seatbelt suppression — which is exactly how a
        # door once opened and then stopped four seconds later.
        _still_ref.pop(door.owner_key, None)
        _parked_closed.discard(door.key)
        out.append(door.key)
    return out


def doors_to_close_after_parking(
    states: dict[str, dict[str, Any]],
    door_states: dict[str, dict],
    now: float,
) -> list[str]:
    """Doors whose owner has pulled in and sat still long enough.

    Requires the car to be within PARKED_RADIUS_M of the door itself, not
    merely inside the geofence — otherwise sitting in the driveway about to
    leave would shut the door on them.
    """
    out: list[str] = []
    for door in config.GARAGE_DOORS:
        s = states.get(door.owner_key)
        if not s or s.get("latitude") is None or s.get("longitude") is None:
            continue
        if _buckled(s):
            # Belted in and sitting still: they are about to drive off, not
            # done for the day. Closing here would fight the departure-open
            # rule and shut the door on them.
            continue
        still_s = stationary_seconds(door.owner_key, s["latitude"], s["longitude"], now)
        at_garage = haversine_m(s["latitude"], s["longitude"],
                                door.latitude, door.longitude) <= config.PARKED_RADIUS_M
        if not at_garage:
            _parked_closed.discard(door.key)
            continue
        if still_s < config.PARKED_DWELL_S:
            _parked_closed.discard(door.key)
            continue
        if door.key in _parked_closed:
            continue
        if door_control.in_cooldown(door.key, now):
            continue
        if not door_control.is_open(door, door_states, now):
            continue
        _parked_closed.add(door.key)
        out.append(door.key)
    return out


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
        if door_control.in_cooldown(door.key, now):
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
    if config.PARKED_CLOSE_ENABLED:
        for key in doors_to_close_after_parking(states, door_states, now):
            if key not in to_close:
                to_close.append(key)

    for door_key in to_close:
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        how = "sensor" if door_control.has_sensor(door) else "inferred state"
        why = "owner parked at the garage" if door_key in _parked_closed else "owner away"
        log.info("Auto-closing %s: %s and door open per %s", door.label, why, how)
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

    if config.DEPART_OPEN_ENABLED:
        for door_key in doors_to_open_for_departure(states, door_states, now):
            door = config.GARAGE_DOORS_BY_KEY[door_key]
            log.info("Opening %s: %s buckled up at the garage", door.label, door.owner_key)
            result = door_control.actuate(door, "open")
            if result["ok"]:
                models.upsert_door_state(door_key, True)
                changed = True
            else:
                log.warning("Departure-open of %s FAILED (%s): %s",
                            door.label, result["via"], result["detail"])
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
