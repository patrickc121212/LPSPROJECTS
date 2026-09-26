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


def actuate(door: config.GarageDoor, action: str) -> dict:
    """Open or close one door by whatever means that door has.

    Returns {"ok", "via", "detail"}. On the Shelly path `action` only
    affects the state we record — the pulse itself is identical.
    """
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


def startup_check() -> None:
    """Log each door's actuation path once at boot, and flag a Shelly whose
    pulse config could latch the opener button on."""
    for door in config.GARAGE_DOORS:
        if door.shelly_host:
            ok, detail = shelly.check_pulse_config(door.shelly_host, door.shelly_channel)
            level = log.info if ok else log.warning
            level("%s -> Shelly %s (%s)", door.label, door.shelly_host, detail)
        elif WEBHOOK_URL:
            log.info("%s -> Google Routine webhook", door.label)
        else:
            log.info("%s -> dry-run (no Shelly, no webhook)", door.label)
