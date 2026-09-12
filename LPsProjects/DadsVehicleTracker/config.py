"""
Static configuration for Dad's Tesla Vehicle Tracker.

Vehicles, garage doors (Shelly + Google Home Routine), geofences, and the
owner-per-door + allowlist model live here. Anything user-editable lives in
the SQLite DB (allowlist overrides, inbox, door state).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# --- Vehicles ---------------------------------------------------------------

@dataclass(frozen=True)
class Vehicle:
    key: str            # "dad", "lp", "mom"
    label: str          # "Dad's Cyberbeast"
    tesla_vin: str      # Fleet API VIN; left blank until paired
    driver: str         # display name for messaging
    color: str          # map marker color
    icon: str           # "cybertruck" | "model3" | "modely"


VEHICLES: list[Vehicle] = [
    Vehicle("dad", "Dad's Cyberbeast", os.getenv("TESLA_VIN_DAD", ""), "Dad",   "#e74c3c", "cybertruck"),
    Vehicle("lp",  "LP's Model 3",     os.getenv("TESLA_VIN_LP",  ""), "LP",    "#3498db", "model3"),
    Vehicle("mom", "Mom's Model Y",    os.getenv("TESLA_VIN_MOM", ""), "Mom",   "#2ecc71", "modely"),
]
VEHICLES_BY_KEY = {v.key: v for v in VEHICLES}


# --- Garage doors -----------------------------------------------------------
# Each door = one Shelly device, exposed to the web tier only through a
# Google Assistant Routine. We trigger the routine by name; Google Home
# does the Shelly round-trip on its end.

@dataclass(frozen=True)
class GarageDoor:
    key: str                # "garage1"
    label: str              # "Garage 1"
    routine_open: str       # Google Assistant routine name to OPEN
    routine_close: str      # Google Assistant routine name to CLOSE
    latitude: float         # geofence center
    longitude: float
    radius_m: float = 75.0  # default 75 m
    owner_key: str = ""     # vehicle key that always opens this door


GARAGE_DOORS: list[GarageDoor] = [
    GarageDoor(
        key="garage1", label="Garage 1",
        routine_open=os.getenv("GARAGE1_OPEN_ROUTINE",  "Open Garage 1"),
        routine_close=os.getenv("GARAGE1_CLOSE_ROUTINE", "Close Garage 1"),
        latitude=float(os.getenv("GARAGE1_LAT", "37.7749")),
        longitude=float(os.getenv("GARAGE1_LON", "-122.4194")),
        owner_key="dad",
    ),
    GarageDoor(
        key="garage2", label="Garage 2",
        routine_open=os.getenv("GARAGE2_OPEN_ROUTINE",  "Open Garage 2"),
        routine_close=os.getenv("GARAGE2_CLOSE_ROUTINE", "Close Garage 2"),
        latitude=float(os.getenv("GARAGE2_LAT", "37.7755")),
        longitude=float(os.getenv("GARAGE2_LON", "-122.4180")),
        owner_key="lp",
    ),
    GarageDoor(
        key="garage3", label="Garage 3",
        routine_open=os.getenv("GARAGE3_OPEN_ROUTINE",  "Open Garage 3"),
        routine_close=os.getenv("GARAGE3_CLOSE_ROUTINE", "Close Garage 3"),
        latitude=float(os.getenv("GARAGE3_LAT", "37.7760")),
        longitude=float(os.getenv("GARAGE3_LON", "-122.4170")),
        owner_key="mom",
    ),
]
GARAGE_DOORS_BY_KEY = {g.key: g for g in GARAGE_DOORS}


# --- Behavior knobs ---------------------------------------------------------

# Tesla Fleet API poll cadence (seconds). Keep conservative; back off on 429.
TESLA_POLL_INTERVAL_S = int(os.getenv("TESLA_POLL_INTERVAL_S", "30"))

# Debounce so we don't re-trigger the same routine in rapid succession
# (e.g. vehicle sits in the geofence for several poll cycles).
GEOFENCE_DEBOUNCE_S = int(os.getenv("GEOFENCE_DEBOUNCE_S", "120"))

# SMS fallback cadence — if a driver hasn't checked in for this long,
# the message is also pushed over Twilio SMS.
SMS_FALLBACK_AFTER_S = int(os.getenv("SMS_FALLBACK_AFTER_S", "300"))

# Shared family login (v1). Per-driver login is out of scope.
SHARED_LOGIN_USERNAME = os.getenv("APP_USERNAME", "family")
SHARED_LOGIN_PASSWORD = os.getenv("APP_PASSWORD", "changeme")

# Where the SQLite file lives.
DB_PATH = os.getenv("TRACKER_DB", "data/tracker.db")


# --- Geofence allowlist helpers ---------------------------------------------

def allowed_for_door(vehicle_key: str, door_key: str, db_allow: set[str]) -> bool:
    """Owner-per-door + allowlist override.

    Returns True if the vehicle may trigger this door's auto-open.
    db_allow is the set of vehicle keys the door owner has explicitly
    granted (already pulled from the SQLite allowlist table).
    """
    door = GARAGE_DOORS_BY_KEY[door_key]
    if vehicle_key == door.owner_key:
        return True
    return vehicle_key in db_allow
