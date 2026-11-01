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
import notify
from eventbus import bus

log = logging.getLogger("geofence_worker")

# Set by request_tick() when a fresh position lands, so the worker reacts to
# a car arriving instead of waiting out its next scheduled sweep.
# Gears that mean the car is under way. Defined here rather than imported
# from trips, which imports this module for haversine_m.
MOVING_GEARS = {"D", "R"}

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
# vehicle_key -> was it plugged in last tick. None (absent) means we have
# no baseline yet, which must never count as an edge: on startup the car is
# usually already plugged in, and reading that as "just plugged in" would
# close the door the moment the app restarts.
_plugged_prev: dict[str, bool] = {}
# door_key -> when it first sat parked-but-unplugged, for the reminder push.
_awaiting_plug: dict[str, float] = {}
# door_keys we have already nagged about, so the reminder fires once.
_plug_alerted: set[str] = set()
# Doors opened because the owner buckled up; cleared when they unbuckle or
# drive away, so one buckle produces one open.
_depart_opened: set[str] = set()
# Doors opened because their owner buckled up, until that owner actually
# leaves. The parked rule must not shut a door we opened seconds ago
# *because* someone was departing: on 2026-09-28 the driver buckled at
# 04:37, the door opened, and the parked rule closed it at 04:39 while they
# were still on the driveway.
_departing: dict[str, float] = {}

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


def _has_driven_off(state: dict[str, Any], distance_m: float) -> bool:
    """Is this really a departure, or just a parked car's GPS wandering?

    Measured scatter on a stationary car reaches 9 m, and positions only
    arrive every 10 m, so a tight fence cannot tell the two apart on
    position alone — and a false departure fires a close, which on an
    already-shut door OPENS it at an empty house.

    So near the fence we want corroboration that the car is actually under
    way: out of Park, or reporting speed. Far enough out, position alone
    settles it, which also covers a stale or missing gear reading.
    """
    if distance_m > config.DEPART_CONFIRM_M:
        return True
    if (state.get("speed_mph") or 0) > 0:
        return True
    gear = str(state.get("gear") or "")
    return gear in MOVING_GEARS


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
    distance = haversine_m(s["latitude"], s["longitude"], door.latitude, door.longitude)
    inside = distance <= config.close_radius(door) or not _has_driven_off(s, distance)
    if inside:
        _away_since.pop(door.key, None)
        _auto_closed.discard(door.key)
        return None
    # Outside: they have actually gone, so the departure is over.
    _departing.pop(door.key, None)
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


def _plugged_in(state: dict[str, Any] | None) -> bool:
    """Is the charge cable connected, per the car?

    Deliberately an allowlist of known-plugged states. A missing or
    unrecognised DetailedChargeState means we do not know, and not knowing
    must never be the thing that moves a door.
    """
    if not state:
        return False
    return str(state.get("charge_state") or "") in config.PLUGGED_IN_STATES


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
        _departing[door.key] = now
        # Restart the dwell clock. Otherwise the parked-close rule, whose
        # timer has been running since the car arrived, fires the instant
        # anything lifts its seatbelt suppression — which is exactly how a
        # door once opened and then stopped four seconds later.
        _still_ref.pop(door.owner_key, None)
        _parked_closed.discard(door.key)
        out.append(door.key)
    return out


def doors_to_close_on_plug_in(
    states: dict[str, dict[str, Any]],
    door_states: dict[str, dict],
    now: float,
) -> list[str]:
    """Doors to close because their owner has just plugged the car back in.

    Connecting the cable says "finished" with no ambiguity, so this overrides
    the departure grace: buckle up, change your mind, plug back in, and the
    door shuts instead of standing open for the full grace period. It needs
    no dwell either -- a car being plugged in is a car that has stopped.

    Strictly the transition into plugged, never the state of being plugged.
    Otherwise opening the door from the app to fetch something, while the car
    sits on the charger, would close it again immediately.
    """
    # Detect the edge once per vehicle, before looking at doors, so two doors
    # owned by one car cannot consume each other's transition.
    just_plugged: set[str] = set()
    for v in config.VEHICLES:
        st = states.get(v.key)
        if not st:
            continue
        plugged = _plugged_in(st)
        was = _plugged_prev.get(v.key)
        _plugged_prev[v.key] = plugged
        if plugged and was is False:
            just_plugged.add(v.key)

    out: list[str] = []
    for door in config.GARAGE_DOORS:
        if door.owner_key not in just_plugged:
            continue
        s = states.get(door.owner_key)
        if not s or s.get("latitude") is None or s.get("longitude") is None:
            continue
        if _buckled(s):
            # Plugged in but still belted: contradictory, and the departure
            # rule may be about to open this door. Leave it alone.
            continue
        if not _parked(s):
            # Gear must say P. A car that is plugged in while not in park is
            # something we do not understand, and we do not move doors on
            # things we do not understand.
            continue
        if haversine_m(s["latitude"], s["longitude"],
                       door.latitude, door.longitude) > config.PARKED_RADIUS_M:
            continue
        if door_control.in_cooldown(door.key, now):
            continue
        if not door_control.is_open(door, door_states, now):
            continue
        # The departure is over; cancel its grace and the unplugged reminder.
        _departing.pop(door.key, None)
        _depart_opened.discard(door.key)
        _awaiting_plug.pop(door.key, None)
        _plug_alerted.discard(door.key)
        # Mark it closed for the parked rule too, so that rule cannot fire a
        # second pulse -- two pulses seconds apart is what stops a door
        # halfway, and it has happened here before.
        _parked_closed.add(door.key)
        out.append(door.key)
    return out


def doors_waiting_for_plug(now: float) -> list[str]:
    """Doors held open only because the car is not plugged in yet, and which
    have been waiting long enough to be worth mentioning. Returns each door
    once; call it from the tick, which owns the notification."""
    if not config.PLUGGED_IN_ALERT_S:
        return []
    out = []
    for door_key, since in _awaiting_plug.items():
        if door_key in _plug_alerted:
            continue
        if (now - since) >= config.PLUGGED_IN_ALERT_S:
            _plug_alerted.add(door_key)
            out.append(door_key)
    return out


def _parked(state: dict[str, Any] | None) -> bool:
    """Is the car actually parked, per the car itself?

    Position alone cannot answer this: the cars only report Location after
    moving 10 m (telemetry minimum_delta), so shuffling back and forth into
    a garage transmits nothing at all and looks identical to sitting still.
    That is how a door once began closing on a driver mid-manoeuvre. Gear is
    unambiguous — you cannot be backing in while in Park.

    A car that has never reported its gear returns False, so the rule stays
    off rather than guessing.
    """
    return bool(state) and str(state.get("gear") or "") == "P"


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
        started = _departing.get(door.key)
        if started is not None and (now - started) < config.DEPART_GRACE_S:
            # We opened this door because they buckled up. Unbuckling for a
            # moment - to fetch something, or a flicker in the signal - must
            # not be read as "finished for the day".
            continue
        if not _parked(s):
            # Still in D or R, or the gear is unknown: the driver is not
            # finished. Restart the dwell clock so the countdown begins when
            # the car is genuinely parked.
            _still_ref.pop(door.owner_key, None)
            _parked_closed.discard(door.key)
            _awaiting_plug.pop(door.key, None)
            _plug_alerted.discard(door.key)
            continue
        still_s = stationary_seconds(door.owner_key, s["latitude"], s["longitude"], now)
        at_garage = haversine_m(s["latitude"], s["longitude"],
                                door.latitude, door.longitude) <= config.PARKED_RADIUS_M
        if not at_garage:
            _parked_closed.discard(door.key)
            _awaiting_plug.pop(door.key, None)
            _plug_alerted.discard(door.key)
            continue
        if still_s < config.PARKED_DWELL_S:
            _parked_closed.discard(door.key)
            _awaiting_plug.pop(door.key, None)
            continue
        if config.REQUIRE_PLUGGED_IN_TO_CLOSE and not _plugged_in(s):
            # Parked, still, done driving — but the cable is not in yet, so
            # someone is probably still walking round the car. Hold the door
            # and remember since when, so the tick can say so out loud.
            _awaiting_plug.setdefault(door.key, now)
            _parked_closed.discard(door.key)
            continue
        _awaiting_plug.pop(door.key, None)
        if door.key in _parked_closed:
            continue
        if door_control.in_cooldown(door.key, now):
            continue
        if not door_control.is_open(door, door_states, now):
            continue
        _parked_closed.add(door.key)
        _plug_alerted.discard(door.key)
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
    plugged_in_closes: set[str] = set()
    if config.PLUG_IN_CLOSES_ENABLED:
        # Before the parked rule, so plugging in wins the reason in the log.
        plugged_in_closes = set(doors_to_close_on_plug_in(states, door_states, now))
        for key in plugged_in_closes:
            if key not in to_close:
                to_close.append(key)
    if config.PARKED_CLOSE_ENABLED:
        for key in doors_to_close_after_parking(states, door_states, now):
            if key not in to_close:
                to_close.append(key)

    for door_key in to_close:
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        how = "sensor" if door_control.has_sensor(door) else "inferred state"
        if door_key in plugged_in_closes:
            why = "owner plugged the car back in"
        elif door_key in _parked_closed:
            why = "owner parked at the garage"
        else:
            why = "owner away"
        log.info("Auto-closing %s: %s and door open per %s", door.label, why, how)
        new_state = door_control.state_after_pulse(door, "close", door_states, now)
        result = door_control.actuate(door, "close")
        if result["ok"]:
            models.upsert_door_state(door_key, new_state)
            changed = True
        else:
            log.warning("Auto-close of %s FAILED (%s): %s",
                        door.label, result["via"], result["detail"])

    for door_key in to_assume:
        log.info("Assuming %s closed: its owner has left the geofence",
                 config.GARAGE_DOORS_BY_KEY[door_key].label)
        models.upsert_door_state(door_key, False)
        changed = True

    for door_key in doors_waiting_for_plug(now):
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        mins = int(config.PLUGGED_IN_ALERT_S / 60)
        log.info("%s held open: %s is parked but not plugged in", door.label, door.owner_key)
        notify.send(
            title=f"{door.label} is still open",
            message=(f"{door.label} has been open {mins} min waiting for the car "
                     "to be plugged in. Plug it in and it will close, or close "
                     "it from the app."),
            tags="warning")

    if changed:
        door_states = {d["door_key"]: d for d in models.all_door_states()}

    if config.DEPART_OPEN_ENABLED:
        for door_key in doors_to_open_for_departure(states, door_states, now):
            door = config.GARAGE_DOORS_BY_KEY[door_key]
            log.info("Opening %s: %s buckled up at the garage", door.label, door.owner_key)
            new_state = door_control.state_after_pulse(door, "open", door_states, now)
            result = door_control.actuate(door, "open")
            if result["ok"]:
                models.upsert_door_state(door_key, new_state)
                changed = True
            else:
                log.warning("Departure-open of %s FAILED (%s): %s",
                            door.label, result["via"], result["detail"])
        if changed:
            door_states = {d["door_key"]: d for d in models.all_door_states()}

    for vehicle_key, door_key in evaluate(states, allowlists, now, door_states):
        door = config.GARAGE_DOORS_BY_KEY[door_key]
        log.info("Geofence enter: %s -> %s", vehicle_key, door.label)
        new_state = door_control.state_after_pulse(door, "open", door_states, now)
        result = door_control.actuate(door, "open")
        if result["ok"]:
            models.upsert_door_state(door.key, new_state)
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
