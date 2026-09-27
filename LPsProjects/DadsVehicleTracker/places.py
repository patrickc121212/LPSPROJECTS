"""
Named places, so a trip reads "Home → Work" instead of two dashes.

Places live in the database rather than config because they are something
you add while looking at a trip you just took, not something you edit a file
for. The garages are always known as "Home" without anyone entering them.

Matching is nearest-first: two places can overlap (a school inside a town
centre, say) and the closer one is the better answer.
"""
from __future__ import annotations

import logging

import config
import models
from geofence_worker import haversine_m

log = logging.getLogger("places")

DEFAULT_RADIUS_M = 150.0
HOME = "Home"


def home_points() -> list[tuple[float, float, float]]:
    """The garages, as (lat, lon, radius). Always present, never editable —
    they come from the door configuration."""
    return [(d.latitude, d.longitude, d.radius_m) for d in config.GARAGE_DOORS]


def at_home(lat: float, lon: float) -> bool:
    return any(haversine_m(lat, lon, plat, plon) <= radius
               for plat, plon, radius in home_points())


def name_for(lat: float | None, lon: float | None) -> str | None:
    """The best label for a point, or None if nowhere known.

    Home wins outright; otherwise the nearest place whose radius contains
    the point, so a smaller place inside a larger one still wins.
    """
    if lat is None or lon is None:
        return None
    if at_home(lat, lon):
        return HOME
    best: tuple[float, str] | None = None
    for place in models.list_places():
        distance = haversine_m(lat, lon, place["latitude"], place["longitude"])
        if distance > (place["radius_m"] or DEFAULT_RADIUS_M):
            continue
        if best is None or distance < best[0]:
            best = (distance, place["name"])
    return best[1] if best else None


def add(name: str, lat: float, lon: float,
        radius_m: float = DEFAULT_RADIUS_M) -> dict | None:
    """Add or rename a place. Returns the stored row, or None if rejected."""
    name = (name or "").strip()
    if not name or lat is None or lon is None:
        return None
    if name.lower() == HOME.lower():
        # Home is defined by the garages; a second definition would only
        # disagree with them later.
        log.info("Refusing to add a place called %r; Home comes from the garages", name)
        return None
    radius = float(radius_m or DEFAULT_RADIUS_M)
    radius = min(max(radius, 20.0), 5000.0)
    place_id = models.upsert_place(name, float(lat), float(lon), radius)
    log.info("Place saved: %s (%.0f m)", name, radius)
    return models.get_place(place_id)


def label_trip(trip: dict) -> dict:
    trip["from_name"] = name_for(trip.get("start_lat"), trip.get("start_lon"))
    trip["to_name"] = name_for(trip.get("end_lat"), trip.get("end_lon"))
    return trip
