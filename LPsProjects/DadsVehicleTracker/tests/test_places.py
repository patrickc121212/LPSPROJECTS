"""Named places, so trips read "Home → Work" rather than two dashes."""
from __future__ import annotations

import models
import places
import trips
from conftest import owned_by


def _home():
    d = owned_by("dad")
    return d.latitude, d.longitude


def _away(metres: float):
    d = owned_by("dad")
    return d.latitude + metres / 111_320.0, d.longitude


# --- Home comes from the garages -------------------------------------------

def test_a_garage_is_home_without_anyone_adding_it():
    assert places.name_for(*_home()) == "Home"


def test_somewhere_unknown_has_no_name():
    assert places.name_for(*_away(5000)) is None
    assert places.name_for(None, None) is None


def test_home_cannot_be_redefined():
    """Home is derived from the garage locations; a second definition would
    only disagree with them later."""
    assert places.add("Home", *_away(5000)) is None
    assert places.add("home", *_away(5000)) is None
    assert models.list_places() == []


# --- adding places ----------------------------------------------------------

def test_a_named_place_is_recognised():
    lat, lon = _away(3000)
    places.add("Work", lat, lon)
    assert places.name_for(lat, lon) == "Work"


def test_a_place_has_a_radius_not_just_a_point():
    lat, lon = _away(3000)
    places.add("Work", lat, lon, radius_m=200)
    near = (lat + 100 / 111_320.0, lon)
    far = (lat + 400 / 111_320.0, lon)
    assert places.name_for(*near) == "Work"
    assert places.name_for(*far) is None


def test_the_nearest_place_wins_when_they_overlap():
    """A school inside a town centre should read as the school."""
    lat, lon = _away(3000)
    places.add("Town", lat, lon, radius_m=2000)
    places.add("School", lat + 200 / 111_320.0, lon, radius_m=300)
    assert places.name_for(lat + 200 / 111_320.0, lon) == "School"
    assert places.name_for(lat + 1500 / 111_320.0, lon) == "Town"


def test_adding_the_same_name_moves_it_rather_than_duplicating():
    first = _away(3000)
    second = _away(4000)
    places.add("Work", *first)
    places.add("Work", *second)
    assert len(models.list_places()) == 1
    assert places.name_for(*second) == "Work"
    assert places.name_for(*first) is None


def test_names_are_case_insensitive_for_uniqueness():
    places.add("Work", *_away(3000))
    places.add("WORK", *_away(4000))
    assert len(models.list_places()) == 1


def test_a_blank_name_is_rejected():
    assert places.add("", *_away(3000)) is None
    assert places.add("   ", *_away(3000)) is None
    assert models.list_places() == []


def test_missing_coordinates_are_rejected():
    assert places.add("Work", None, None) is None


def test_radius_is_clamped_to_something_sensible():
    lat, lon = _away(3000)
    tiny = places.add("Tiny", lat, lon, radius_m=1)
    assert tiny["radius_m"] >= 20
    huge = places.add("Huge", lat, lon + 0.5, radius_m=999_999)
    assert huge["radius_m"] <= 5000


def test_deleting_a_place_forgets_the_name():
    lat, lon = _away(3000)
    saved = places.add("Work", lat, lon)
    models.delete_place(saved["id"])
    assert places.name_for(lat, lon) is None


# --- trips use them ---------------------------------------------------------

def test_trip_endpoints_are_labelled():
    lat, lon = _away(3000)
    places.add("Work", lat, lon)
    trip = {"start_lat": _home()[0], "start_lon": _home()[1],
            "end_lat": lat, "end_lon": lon}
    places.label_trip(trip)
    assert trip["from_name"] == "Home" and trip["to_name"] == "Work"


def test_trips_place_name_still_works():
    """trips.place_name is the older entry point; it must keep working."""
    assert trips.place_name(*_home()) == "Home"
    lat, lon = _away(3000)
    places.add("Work", lat, lon)
    assert trips.place_name(lat, lon) == "Work"


# --- routes -----------------------------------------------------------------

def test_places_api_round_trip(admin):
    lat, lon = _away(3000)
    r = admin.post("/api/places", json={"name": "Work", "latitude": lat,
                                        "longitude": lon, "radius_m": 250})
    assert r.status_code == 200 and r.get_json()["name"] == "Work"
    listed = admin.get("/api/places").get_json()["places"]
    assert [p["name"] for p in listed] == ["Work"]


def test_places_api_rejects_a_blank_name(admin):
    lat, lon = _away(3000)
    assert admin.post("/api/places", json={"name": "", "latitude": lat,
                                           "longitude": lon}).status_code == 400


def test_places_api_delete(admin):
    lat, lon = _away(3000)
    pid = admin.post("/api/places", json={"name": "Work", "latitude": lat,
                                          "longitude": lon}).get_json()["id"]
    assert admin.delete(f"/api/places/{pid}").status_code == 200
    assert admin.get("/api/places").get_json()["places"] == []


def test_places_are_admin_only(auth):
    """A named place says where someone routinely goes, so it belongs with
    the trip history it annotates."""
    assert auth.get("/api/places").status_code == 403
    assert auth.post("/api/places", json={"name": "x", "latitude": 1,
                                          "longitude": 2}).status_code == 403


def test_places_require_login(client):
    assert client.get("/api/places").status_code == 302
