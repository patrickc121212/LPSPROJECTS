"""
Is there a person in the vehicle?

Tesla gives one direct answer and several hints. `DriverSeatOccupied` is
described by Tesla as "driver presence, determined by combination of sources
depending on platform", so it is the driver's seat only and its accuracy
varies by model. `PassengerSeatBelt` is deliberately not used: Tesla's own
documentation says it "improperly reports if the 2nd row center seat belt is
buckled", so it answers nothing about the front passenger.

That shapes what this module may honestly claim. It can say someone IS
present, with good confidence, from several independent signals. It can
never say a car is empty, because:

  - the signal covers the driver's seat, so a passenger, a child or a dog in
    the back is invisible to it;
  - a sleeping car transmits nothing at all, so silence is not absence;
  - `DriverSeatOccupied` is platform-dependent and unverified on these cars.

So the negative verdict is "no driver detected", never "empty". The
difference matters if anyone ever glances at this to decide whether it is
safe to move a door.
"""
from __future__ import annotations

import time
from typing import Any

# Beyond this, we are describing history rather than the present.
STALE_S = 15 * 60

PRESENT = "present"
LIKELY = "likely"
NO_DRIVER = "no_driver"
UNKNOWN = "unknown"


def summarise(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Fold the presence signals into one verdict for display.

    Returns {state, label, detail, since}. `state` is one of PRESENT,
    LIKELY, NO_DRIVER or UNKNOWN.
    """
    now = time.time() if now is None else now
    updated = row.get("updated_at") or 0
    age = now - updated

    occupied = row.get("occupied")
    if occupied is not None:
        occupied = bool(occupied)
    display_on = row.get("display_on")
    if display_on is not None:
        display_on = bool(display_on)
    belted = str(row.get("seatbelt") or "") == "Latched"
    gear = row.get("gear")
    moving = (row.get("speed_mph") or 0) > 1 or gear in ("D", "R")

    def out(state: str, label: str, detail: str) -> dict[str, Any]:
        return {"state": state, "label": label, "detail": detail,
                "since": updated or None}

    # Nothing recent enough to describe the present moment. Checked before
    # the positive cases so a day-old "someone is aboard" cannot persist.
    if age > STALE_S:
        mins = int(age / 60)
        return out(UNKNOWN, "Unknown",
                   f"No report for {mins} min — the car is probably asleep, "
                   "and a sleeping car tells us nothing either way.")

    # Strongest first. Each of these means a person, independently.
    if moving:
        return out(PRESENT, "Someone is driving",
                   "The car is in gear and moving, so somebody is at the wheel.")
    if belted:
        return out(PRESENT, "Driver belted in",
                   "The driver's seat belt is latched.")
    if occupied:
        return out(PRESENT, "Someone in the driver's seat",
                   "The car reports driver presence.")
    if display_on:
        return out(LIKELY, "Probably someone inside",
                   "The centre screen is awake, which does not happen by "
                   "itself, but nobody is in the driver's seat.")

    if occupied is False:
        return out(NO_DRIVER, "No driver detected",
                   "Nobody in the driver's seat. This cannot see a passenger "
                   "or anyone in the back, so it is not the same as empty.")

    return out(UNKNOWN, "Unknown",
               "The car has not reported driver presence. It may not support "
               "the signal.")


def supported(row: dict[str, Any]) -> bool:
    """Has this car ever reported presence? Lets the UI distinguish "nobody
    aboard" from "this model never tells us", instead of showing a confident
    blank for a car that cannot answer."""
    return row.get("occupied") is not None
