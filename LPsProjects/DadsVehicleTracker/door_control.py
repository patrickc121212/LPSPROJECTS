"""
One place that decides how a garage door is actuated.

Per door, in priority order:
  1. `shelly_host` set  -> local Shelly RPC pulse (fast, works offline)
  2. GOOGLE_ROUTINE_WEBHOOK_URL set -> Google Assistant Routine (the original
     plan; still used for doors whose Shelly isn't installed yet)
  3. neither -> dry-run log

Doors on the Shelly path are TOGGLE-only: one pulse presses the opener's
button, and the same press opens or closes. `is_toggle()` says which doors
behave that way, because it changes what "open" means to the rest of the app.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

import config
import shelly

log = logging.getLogger("door_control")

WEBHOOK_URL = os.getenv("GOOGLE_ROUTINE_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.getenv("GOOGLE_ROUTINE_WEBHOOK_TOKEN", "")

# How long we trust our own "this door is open" assumption for auto-open
# purposes. There is no sensor, so the belief is inferred from our last
# pulse and drifts the moment someone uses the wall button or HomeLink.
# A car can only re-trigger auto-open by leaving the geofence and coming
# back, and a door essentially always closes behind a departing car, so a
# stale "open" older than this is treated as unknown rather than blocking
# auto-open forever. Set DOOR_OPEN_TTL_S=0 to trust the belief indefinitely.
DOOR_OPEN_TTL_S = int(os.getenv("DOOR_OPEN_TTL_S", "600"))


def is_toggle(door: config.GarageDoor) -> bool:
    """True when open and close are the same physical action."""
    return bool(door.shelly_host)


def _fire_routine(routine_name: str) -> tuple[bool, str]:
    """POST to the Google Home webhook. No-op in dev unless the URL is set."""
    try:
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
        return True, routine_name
    except Exception as exc:  # noqa: BLE001
        log.warning("Routine webhook failed for %s: %s", routine_name, exc)
        return False, str(exc)


# door_key -> when it was last commanded, by any path.
_last_action: dict[str, float] = {}


def record_action(door_key: str, now: float | None = None) -> None:
    _last_action[door_key] = time.time() if now is None else now


def seconds_since_action(door_key: str, now: float | None = None) -> float | None:
    last = _last_action.get(door_key)
    if last is None:
        return None
    return (time.time() if now is None else now) - last


def in_cooldown(door_key: str, now: float | None = None) -> bool:
    """Was this door commanded too recently for another automatic pulse?

    A negative interval means the stored time is ahead of `now` — a clock
    that jumped backwards, say. Treat that as "not recently", so a bad clock
    cannot freeze every automatic rule indefinitely.
    """
    since = seconds_since_action(door_key, now)
    if since is None or since < 0:
        return False
    return since < config.DOOR_ACTION_COOLDOWN_S


def actuate(door: config.GarageDoor, action: str) -> dict:
    """Open or close one door by whatever means that door has.

    Returns {"ok", "via", "detail"}. On the Shelly path `action` only
    affects the state we record — the pulse itself is identical.
    """
    # Recorded for every path, manual included: a button press must also
    # hold off the automatic rules, or an auto-close can cut short a door
    # someone just opened by hand.
    record_action(door.key)

    if door.shelly_host:
        ok, detail = shelly.pulse(door.shelly_host, door.shelly_channel)
        return {"ok": ok, "via": "shelly", "detail": detail}

    routine = door.routine_open if action == "open" else door.routine_close
    if WEBHOOK_URL:
        ok, detail = _fire_routine(routine)
        return {"ok": ok, "via": "routine", "detail": detail}

    log.info("[dry-run] would fire routine: %s", routine)
    return {"ok": True, "via": "dry-run", "detail": routine}


def believed_open(door_key: str, door_states: dict[str, dict], now: float) -> bool:
    """Do we currently believe this door is open?

    Only counts if the belief is fresh (see DOOR_OPEN_TTL_S). `door_states`
    is keyed by door_key with the rows from models.all_door_states().
    """
    row = door_states.get(door_key)
    if not row or row.get("is_open") != 1:
        return False
    if DOOR_OPEN_TTL_S <= 0:
        return True
    return (now - (row.get("updated_at") or 0)) < DOOR_OPEN_TTL_S


# Sensor readings are cached briefly: the geofence now evaluates on every
# position update, and we don't want an HTTP call per door per tick.
SENSOR_CACHE_S = 2.0
_sensor_cache: dict[str, tuple[float, bool | None]] = {}


def sensed_open(door: config.GarageDoor, now: float | None = None) -> bool | None:
    """True/False from a wired position sensor, or None when there isn't one
    (or it can't be read). Contact closed = magnet present = door closed,
    unless the door's sensor_invert says otherwise."""
    if not (door.shelly_host and door.sensor_input):
        return None
    now = time.time() if now is None else now
    hit = _sensor_cache.get(door.key)
    if hit and now - hit[0] < SENSOR_CACHE_S:
        return hit[1]
    raw = shelly.get_input_state(door.shelly_host, int(door.sensor_input))
    if raw is None:
        result = None
    else:
        closed = (not raw) if door.sensor_invert else raw
        result = not closed
    _sensor_cache[door.key] = (now, result)
    return result


def is_open(door: config.GarageDoor, door_states: dict[str, dict], now: float) -> bool:
    """Best available answer to "is this door open?".

    A wired sensor is the truth. Without one we fall back to believed_open,
    which is only our memory of the last pulse we sent.
    """
    sensed = sensed_open(door, now)
    if sensed is not None:
        return sensed
    return believed_open(door.key, door_states, now)


def has_sensor(door: config.GarageDoor) -> bool:
    return bool(door.shelly_host and door.sensor_input)


def config_warnings() -> list[str]:
    """Settings that would quietly stop auto-close working."""
    out: list[str] = []
    if config.AUTO_CLOSE_ENABLED:
        sensorless = [d.label for d in config.GARAGE_DOORS
                      if d.shelly_host and not has_sensor(d)]
        if sensorless:
            out.append(
                "AUTO_CLOSE_ENABLED with no position sensor on "
                + ", ".join(sensorless)
                + ": a wrong belief will OPEN the door at an empty house")
        if DOOR_OPEN_TTL_S and config.AUTO_CLOSE_DELAY_S >= DOOR_OPEN_TTL_S:
            out.append(
                f"AUTO_CLOSE_DELAY_S ({config.AUTO_CLOSE_DELAY_S}s) >= DOOR_OPEN_TTL_S "
                f"({DOOR_OPEN_TTL_S}s): without a sensor the door's 'open' belief "
                "expires before the close fires, so auto-close would never run")
    if config.PARKED_CLOSE_ENABLED:
        overlap = [d.label for d in config.GARAGE_DOORS
                   if config.PARKED_RADIUS_M > config.close_radius(d)]
        if overlap:
            out.append(
                f"PARKED_RADIUS_M ({config.PARKED_RADIUS_M:.0f} m) reaches outside the close "
                f"fence on {', '.join(overlap)}: a car there counts as both parked at the "
                "garage and departed")
    return out


def startup_check() -> None:
    """Log each door's actuation path once at boot, and flag a Shelly whose
    pulse config could latch the opener button on."""
    for door in config.GARAGE_DOORS:
        if door.shelly_host:
            ok, detail = shelly.check_pulse_config(door.shelly_host, door.shelly_channel)
            level = log.info if ok else log.warning
            sensor = f"sensor input:{door.sensor_input}" if has_sensor(door) else "NO position sensor (state is inferred)"
            level("%s -> Shelly %s (%s, %s)", door.label, door.shelly_host, detail, sensor)
        elif WEBHOOK_URL:
            log.info("%s -> Google Routine webhook", door.label)
        else:
            log.info("%s -> dry-run (no Shelly, no webhook)", door.label)
    for warning in config_warnings():
        log.warning("%s", warning)
    if config.AUTO_CLOSE_ENABLED:
        log.info("Auto-close ON: closes %ss after the owner leaves the geofence.",
                 config.AUTO_CLOSE_DELAY_S)
