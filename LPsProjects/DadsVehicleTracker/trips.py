"""
Trip detection and position history.

Until now the app kept only each car's *current* position — every update
overwrote the last and the previous one was gone. This records the
breadcrumbs and groups them into trips.

A trip starts when a car is moving and ends once it has been stationary for
TRIP_IDLE_END_S. Trips shorter than TRIP_MIN_DISTANCE_MI are discarded, so
shuffling a car on the driveway doesn't fill the list with noise.

Distance, like charging energy, has two possible sources:
  1. `Odometer` — the car's own figure, exact. End minus start.
  2. Summed great-circle hops between recorded points — always available,
     but slightly under-reads on bends and over-reads on GPS noise.
"""
from __future__ import annotations

import logging
import time

import config
import models
from geofence_worker import haversine_m

log = logging.getLogger("trips")

M_PER_MILE = 1609.344

# Below this the car is parked as far as trips are concerned.
MOVING_SPEED_MPH = 1.0
# Gears that mean the driver intends to move, even at a standstill.
MOVING_GEARS = {"D", "R"}

_last_point: dict[str, tuple[float, float]] = {}
_last_prune = 0.0


def _moving(snap: dict) -> bool:
    if (snap.get("speed_mph") or 0.0) >= MOVING_SPEED_MPH:
        return True
    return str(snap.get("gear") or "") in MOVING_GEARS


def _changed(vehicle_key: str, lat: float, lon: float) -> bool:
    """Has the car moved far enough to be worth another breadcrumb?

    The cars only report Location after moving 10 m, so this mostly guards
    against re-storing the same fix when some other field triggers a flush.
    """
    prev = _last_point.get(vehicle_key)
    if prev is None:
        return True
    return haversine_m(lat, lon, prev[0], prev[1]) >= config.HISTORY_MIN_MOVE_M


def distance_of(trip: dict, gps_miles: float | None) -> tuple[float | None, str | None]:
    """Trip distance and which source produced it."""
    start, end = trip.get("odo_start"), trip.get("odo_end")
    if start is not None and end is not None:
        delta = end - start
        if delta >= 0:
            return round(delta, 2), "odometer"
        log.warning("odometer went backwards (%.1f -> %.1f); using GPS", start, end)
    if gps_miles is not None:
        return round(gps_miles, 2), "gps"
    return None, None


def observe(vehicle_key: str, snap: dict, now: float | None = None) -> None:
    """Advance trip state for one vehicle and record its position."""
    now = time.time() if now is None else now
    lat, lon = snap.get("latitude"), snap.get("longitude")
    if lat is None or lon is None:
        return

    trip = models.get_open_trip(vehicle_key)
    moving = _moving(snap)

    if moving and trip is None:
        trip_id = models.open_trip(vehicle_key, now, lat, lon,
                                   snap.get("battery_pct"), snap.get("odometer"))
        trip = models.get_open_trip(vehicle_key)
        log.info("Trip started: %s", vehicle_key)
    elif trip is not None:
        trip_id = trip["id"]
    else:
        trip_id = None

    if _changed(vehicle_key, lat, lon):
        models.add_position(vehicle_key, now, lat, lon, snap.get("speed_mph"),
                            snap.get("battery_pct"), trip_id)
        _last_point[vehicle_key] = (lat, lon)

    if trip is None:
        return

    # Accumulate GPS distance from the last point we counted.
    gps_mi = trip.get("distance_mi") or 0.0
    last = (trip.get("last_lat"), trip.get("last_lon"))
    if last[0] is not None and last[1] is not None:
        step_m = haversine_m(lat, lon, last[0], last[1])
        if step_m >= config.HISTORY_MIN_MOVE_M:
            gps_mi += step_m / M_PER_MILE
        else:
            lat, lon = last  # don't advance the reference on noise

    models.update_trip(
        trip["id"],
        end_lat=snap.get("latitude"), end_lon=snap.get("longitude"),
        last_lat=lat, last_lon=lon,
        distance_mi=gps_mi,
        max_speed_mph=max(snap.get("speed_mph") or 0.0, trip.get("max_speed_mph") or 0.0),
        end_pct=snap.get("battery_pct"),
        odo_end=snap.get("odometer"),
        last_moved_at=now if moving else None,
    )

    if not moving:
        idle_for = now - (trip.get("last_moved_at") or trip["started_at"])
        if idle_for >= config.TRIP_IDLE_END_S:
            _close(vehicle_key, trip["id"], now, gps_mi)


def _close(vehicle_key: str, trip_id: int, now: float, gps_mi: float) -> None:
    fresh = models.get_trip(trip_id) or {}
    miles, source = distance_of(fresh, gps_mi)
    if miles is not None and miles < config.TRIP_MIN_DISTANCE_MI:
        log.info("Discarding %s trip of %.2f mi (below the %.2f mi floor)",
                 vehicle_key, miles, config.TRIP_MIN_DISTANCE_MI)
        models.delete_trip(trip_id)
        return
    # Credit the trip to when it last moved, not when we noticed it stopped.
    ended = fresh.get("last_moved_at") or now
    models.close_trip(trip_id, ended, miles, source)
    log.info("Trip ended: %s %.1f mi in %.0f min (%s)", vehicle_key, miles or 0.0,
             (ended - fresh.get("started_at", ended)) / 60, source)


def maybe_prune(now: float | None = None) -> int:
    """Drop breadcrumbs past the retention window. Cheap to call often."""
    global _last_prune
    now = time.time() if now is None else now
    if now - _last_prune < 3600:
        return 0
    _last_prune = now
    if config.HISTORY_RETENTION_DAYS <= 0:
        return 0
    cutoff = now - config.HISTORY_RETENTION_DAYS * 86400
    n = models.prune_history(cutoff)
    if n:
        log.info("Pruned %d position rows older than %d days",
                 n, config.HISTORY_RETENTION_DAYS)
    return n


def place_name(lat: float | None, lon: float | None) -> str | None:
    """Label a point as a known garage, else None."""
    if lat is None or lon is None:
        return None
    for door in config.GARAGE_DOORS:
        if haversine_m(lat, lon, door.latitude, door.longitude) <= door.radius_m:
            return "Home"
    return None


def summary(trips: list[dict]) -> dict:
    done = [t for t in trips if t.get("ended_at")]
    return {
        "trips": len(done),
        "miles": round(sum(t.get("distance_mi") or 0.0 for t in done), 1),
        "hours": round(sum((t["ended_at"] - t["started_at"]) for t in done) / 3600, 1),
        "max_speed": round(max((t.get("max_speed_mph") or 0.0 for t in done), default=0.0)),
    }
