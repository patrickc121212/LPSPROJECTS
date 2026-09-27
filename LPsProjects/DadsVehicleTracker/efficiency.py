"""
What the driving actually costs.

The trip log knows the miles and the charging log knows the kilowatt-hours
and the money; neither is interesting alone. Put them together and you get
cost per mile, miles per kWh, and how much of what you pay for never
reaches the battery.

A caveat worth stating plainly, because it bounds how far these numbers can
be trusted: energy is credited to the month it was *charged*, not the month
it was *driven*. A car charged on the 31st and driven on the 1st moves cost
from one month to the next. Over a month that mostly averages out; over a
day it would be meaningless, which is why nothing here reports a daily
figure. Months with no driving, or no charging, are reported as unknown
rather than as zero or infinity.
"""
from __future__ import annotations

import config
import models

# Below this, a month's numbers are too thin to mean anything.
MIN_MILES = 5.0
MIN_KWH = 1.0


def _ratio(numerator: float | None, denominator: float | None,
           floor: float = 0.0) -> float | None:
    if numerator is None or denominator is None or denominator <= floor:
        return None
    return numerator / denominator


def combine(trip_rows: list[dict], charge_rows: list[dict]) -> list[dict]:
    """Merge the two monthly aggregates into one row per vehicle-month."""
    merged: dict[tuple[str, str], dict] = {}
    for row in trip_rows:
        key = (row["vehicle_key"], row["month"])
        merged.setdefault(key, _blank(key))
        merged[key].update(
            trips=row.get("trips") or 0,
            miles=row.get("miles") or 0.0,
            hours=(row.get("seconds") or 0.0) / 3600,
        )
    for row in charge_rows:
        key = (row["vehicle_key"], row["month"])
        merged.setdefault(key, _blank(key))
        merged[key].update(
            sessions=row.get("sessions") or 0,
            kwh=row.get("kwh") or 0.0,
            cost=row.get("cost") or 0.0,
        )
    out = []
    for row in merged.values():
        # Both sides are needed. Miles with no charging would otherwise
        # report $0.00 per mile, which reads as "free" when it really means
        # the energy came from a charge in another month.
        charged = row["kwh"] >= MIN_KWH
        row["cost_per_mile"] = (_ratio(row["cost"], row["miles"], MIN_MILES)
                                if charged else None)
        row["miles_per_kwh"] = _ratio(row["miles"], row["kwh"], MIN_KWH)
        out.append(row)
    out.sort(key=lambda r: (r["month"], r["vehicle_key"]), reverse=True)
    return out


def _blank(key: tuple[str, str]) -> dict:
    vehicle_key, month = key
    return {"vehicle_key": vehicle_key, "month": month, "trips": 0,
            "miles": 0.0, "hours": 0.0, "sessions": 0, "kwh": 0.0, "cost": 0.0}


def by_month() -> list[dict]:
    return combine(models.trip_totals_by_month(), models.charge_totals_by_month())


def overall(rows: list[dict] | None = None) -> dict:
    """Totals across everything recorded so far."""
    rows = by_month() if rows is None else rows
    miles = sum(r["miles"] for r in rows)
    kwh = sum(r["kwh"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    return {
        "miles": round(miles, 1),
        "kwh": round(kwh, 1),
        "cost": round(cost, 2),
        "trips": sum(r["trips"] for r in rows),
        "sessions": sum(r["sessions"] for r in rows),
        "cost_per_mile": (_ratio(cost, miles, MIN_MILES)
                          if kwh >= MIN_KWH else None),
        "miles_per_kwh": _ratio(miles, kwh, MIN_KWH),
    }


def cost_per_mile(vehicle_key: str, rows: list[dict] | None = None) -> float | None:
    """One vehicle's rate, for estimating what a single trip cost."""
    rows = by_month() if rows is None else rows
    mine = [r for r in rows if r["vehicle_key"] == vehicle_key]
    miles = sum(r["miles"] for r in mine)
    cost = sum(r["cost"] for r in mine)
    kwh = sum(r["kwh"] for r in mine)
    if kwh < MIN_KWH:
        return None
    return _ratio(cost, miles, MIN_MILES)


def estimate_trip_cost(trip: dict, rates: dict[str, float | None]) -> float | None:
    rate = rates.get(trip.get("vehicle_key", ""))
    miles = trip.get("distance_mi")
    if rate is None or miles is None:
        return None
    return round(miles * rate, 2)


def charging_efficiency(sessions: list[dict] | None = None) -> dict:
    """How much of what you pay for reaches the battery.

    `kwh` is metered at the wall and `kwh_dc` at the pack, so the gap is the
    onboard charger's conversion loss. It is much worse at low power — a
    1.4 kW trickle wastes far more of every unit than a 240 V circuit does,
    which is the sort of thing worth knowing before paying for one.
    """
    sessions = models.list_charge_sessions(limit=500) if sessions is None else sessions
    usable = [s for s in sessions
              if s.get("kwh") and s.get("kwh_dc") and s["kwh"] > 0]
    if not usable:
        return {"known": False}
    wall = sum(s["kwh"] for s in usable)
    battery = sum(s["kwh_dc"] for s in usable)
    if wall <= 0:
        return {"known": False}
    pct = battery / wall * 100
    wasted_kwh = wall - battery
    return {
        "known": True,
        "sessions": len(usable),
        "wall_kwh": round(wall, 1),
        "battery_kwh": round(battery, 1),
        "efficiency_pct": round(pct, 1),
        "wasted_kwh": round(wasted_kwh, 1),
        "wasted_cost": round(wasted_kwh * config.ELECTRICITY_RATE_PER_KWH, 2),
    }
