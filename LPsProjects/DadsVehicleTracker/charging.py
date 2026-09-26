"""
Charging sessions and what they cost.

Fed from the telemetry stream: a session opens when a car reports
`DetailedChargeState` of Charging or Starting, and closes when it stops.
Sessions live in SQLite from the moment they open, so an app restart
mid-charge doesn't lose one.

Measuring the energy is the fiddly part. Two sources, in order of trust:

  1. `LifetimeEnergyChargedKwh` — monotonic, so end minus start is the
     session's energy no matter how the car chooses to reset anything.
     Not reported by every model.
  2. `ACChargingEnergyIn` + `DCChargingEnergyIn` — per-session counters.
     We track their running maximum, which is right whether they count up
     through the session or reset at its start.

Cost is energy times ELECTRICITY_RATE_PER_KWH. That's a flat rate; a
time-of-use tariff would need the rate applied per interval instead.
"""
from __future__ import annotations

import logging

import config
import models

log = logging.getLogger("charging")

# Charge states that mean current is (or is about to be) flowing.
ACTIVE_STATES = {"Charging", "Starting"}


def is_active(charge_state: str | None) -> bool:
    return (charge_state or "") in ACTIVE_STATES


def session_energy(lifetime_start: float | None, lifetime_last: float | None,
                   counter_last: float | None) -> tuple[float | None, str | None]:
    """Energy for a session, and which source produced it."""
    if lifetime_start is not None and lifetime_last is not None:
        delta = lifetime_last - lifetime_start
        if delta >= 0:
            return round(delta, 3), "lifetime"
        # A negative delta means the lifetime counter reset (or the car was
        # swapped); fall through to the per-session counter rather than
        # recording a negative charge.
        log.warning("lifetime energy went backwards (%.3f -> %.3f); using the session counter",
                    lifetime_start, lifetime_last)
    if counter_last is not None and counter_last >= 0:
        return round(counter_last, 3), "counter"
    return None, None


def cost_of(kwh: float | None) -> float | None:
    if kwh is None:
        return None
    return round(kwh * config.ELECTRICITY_RATE_PER_KWH, 4)


def observe(vehicle_key: str, snap: dict, now: float, at_home: bool | None = None) -> None:
    """Advance the session state machine for one vehicle.

    `snap` holds the latest known values: charge_state, battery_pct,
    lifetime_kwh, ac_energy, dc_energy, power_kw. Missing keys are fine —
    we only record what the car actually sent.
    """
    active = is_active(snap.get("charge_state"))
    open_session = models.get_open_charge_session(vehicle_key)
    counter = _counter(snap)

    if active and open_session is None:
        sid = models.open_charge_session(
            vehicle_key, now, snap.get("battery_pct"), at_home, snap.get("lifetime_kwh"))
        log.info("Charging started: %s at %s%% (%s)", vehicle_key,
                 snap.get("battery_pct"), "home" if at_home else "away" if at_home is not None else "?")
        models.update_charge_session(sid, end_pct=snap.get("battery_pct"),
                                     counter_last=counter, peak_kw=snap.get("power_kw"))
        return

    if active and open_session is not None:
        peak = max(snap.get("power_kw") or 0.0, open_session.get("peak_kw") or 0.0)
        models.update_charge_session(
            open_session["id"],
            end_pct=snap.get("battery_pct"),
            lifetime_last=snap.get("lifetime_kwh"),
            # Keep the running maximum: the counter may reset at the start of
            # a session, and must never appear to go down mid-session.
            counter_last=max(counter or 0.0, open_session.get("counter_last") or 0.0) or None,
            peak_kw=peak or None,
        )
        return

    if not active and open_session is not None:
        # The reading that says "Complete"/"Disconnected" usually carries the
        # final energy too, so fold it in before totting up — otherwise every
        # session records the energy as of the previous sample.
        models.update_charge_session(
            open_session["id"],
            end_pct=snap.get("battery_pct"),
            lifetime_last=snap.get("lifetime_kwh"),
            counter_last=max(counter or 0.0, open_session.get("counter_last") or 0.0) or None,
        )
        fresh = models.get_open_charge_session(vehicle_key) or open_session
        kwh, source = session_energy(fresh.get("lifetime_start"),
                                     fresh.get("lifetime_last"),
                                     fresh.get("counter_last"))
        models.close_charge_session(fresh["id"], now, kwh, cost_of(kwh), source)
        log.info("Charging ended: %s %s%% -> %s%%, %s kWh, %s%s",
                 vehicle_key, fresh.get("start_pct"), fresh.get("end_pct"),
                 kwh, config.CURRENCY_SYMBOL,
                 f"{cost_of(kwh):.2f}" if kwh is not None else "?")


def _counter(snap: dict) -> float | None:
    """Session energy to bill for.

    Observed on a real car mid-AC-charge: BOTH counters climb at once —
    ACChargingEnergyIn 8.70 kWh alongside DCChargingEnergyIn 7.16 kWh. They
    are not two halves of a total; AC is energy drawn from the wall and DC
    is what reaches the battery after the onboard charger's losses. Adding
    them would bill roughly 1.8x the truth.

    We want the grid side, because that is what the meter charges for. On a
    DC fast charger there is no AC figure and the DC meter is what is
    billed, so fall back to that.
    """
    ac, dc = snap.get("ac_energy"), snap.get("dc_energy")
    if ac is not None and ac > 0:
        return ac
    if dc is not None and dc > 0:
        return dc
    if ac is not None or dc is not None:
        return 0.0
    return None


def summary(sessions: list[dict]) -> dict:
    """Totals for a list of completed sessions."""
    done = [s for s in sessions if s.get("kwh") is not None]
    kwh = sum(s["kwh"] for s in done)
    return {
        "sessions": len(done),
        "kwh": round(kwh, 2),
        "cost": round(sum(s.get("cost") or 0.0 for s in done), 2),
        "home": sum(1 for s in done if s.get("at_home") == 1),
        "away": sum(1 for s in done if s.get("at_home") == 0),
    }
