"""
Tesla's own vehicle alerts, which we were already receiving and throwing away.

The cars publish to `telemetry/<VIN>/alerts/<Name>/current`, and we had only
ever subscribed to positions and connectivity. The payload looks like:

    {"Audiences": ["Customer", "Service"],
     "StartedAt": "2026-09-27T08:42:42Z",
     "EndedAt":   "2026-09-27T08:42:43Z"}

`EndedAt` is present even on the "current" topic, so it is emphatically not
a list of live problems: an alert is only active while that field is empty.
Most also carry a "Service" audience and are engineering noise — names like
`CP_a044_lostCommsHVP` mean nothing to a driver — so only the ones Tesla
labels for the Customer are surfaced.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import models

log = logging.getLogger("alerts")

CUSTOMER = "Customer"
# Names arrive as APP_w390_cabinCamVisDegraded, CP_a044_lostCommsHVP, etc.
_PREFIX = re.compile(r"^[A-Z]+_[a-z]\d+_")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def parse_time(value: str | None) -> float | None:
    """ISO-8601 to epoch seconds; empty or unparsable means "not set"."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Only assume UTC when the string carried no offset. Overriding a real
    # offset would shift the timestamp by hours.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def humanise(name: str) -> str:
    """`APP_w390_cabinCamVisDegraded` -> `Cabin cam vis degraded`.

    The raw names are for Tesla's engineers. Strip the code prefix and split
    the camel case; acronyms are left alone because "HVP" is more useful to
    a service centre than any expansion we could guess at.
    """
    stem = _PREFIX.sub("", name) or name
    parts = _CAMEL.sub(" ", stem).strip().split()
    if not parts:
        return name
    # Sentence case, not title case — but leave acronyms like HVP alone.
    words = [parts[0][:1].upper() + parts[0][1:]]
    words += [w if w.isupper() and len(w) > 1 else w.lower() for w in parts[1:]]
    return " ".join(words)


def is_for_customer(audiences: list | None) -> bool:
    return CUSTOMER in (audiences or [])


def is_active(payload: dict) -> bool:
    return not payload.get("EndedAt")


def observe(vehicle_key: str, name: str, payload: dict) -> dict | None:
    """Record one alert. Returns the row if it is newly active, else None.

    Only a fresh, customer-facing, currently-active alert is worth telling
    anyone about — everything else is stored for the record and stays quiet.
    """
    if not isinstance(payload, dict):
        return None
    audiences = payload.get("Audiences") or []
    started = parse_time(payload.get("StartedAt"))
    ended = parse_time(payload.get("EndedAt"))
    if started is None:
        return None

    existed = models.get_alert(vehicle_key, name, started) is not None
    models.upsert_alert(vehicle_key, name, started, ended, ",".join(audiences))

    if existed or ended is not None or not is_for_customer(audiences):
        return None
    return {"vehicle_key": vehicle_key, "name": name,
            "label": humanise(name), "started_at": started}


def active(vehicle_key: str | None = None) -> list[dict]:
    """Customer-facing alerts that have not ended."""
    rows = models.list_alerts(active_only=True, vehicle_key=vehicle_key)
    out = []
    for row in rows:
        if not is_for_customer((row.get("audiences") or "").split(",")):
            continue
        row["label"] = humanise(row["name"])
        out.append(row)
    return out


def recent(limit: int = 50, vehicle_key: str | None = None) -> list[dict]:
    rows = models.list_alerts(active_only=False, vehicle_key=vehicle_key, limit=limit)
    for row in rows:
        row["label"] = humanise(row["name"])
        row["for_customer"] = is_for_customer((row.get("audiences") or "").split(","))
    return rows
