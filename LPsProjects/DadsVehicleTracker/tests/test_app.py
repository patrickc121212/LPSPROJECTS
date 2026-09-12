"""Flask routes: auth, pages, JSON API, SSE, Twilio webhook."""
from __future__ import annotations

import json

import models


# --- Auth ------------------------------------------------------------------

def test_pages_require_login(client):
    for path in ("/map", "/inbox", "/doors", "/api/vehicles", "/api/doors", "/stream"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["Location"]


def test_bad_login_stays_on_form(client):
    r = client.post("/login", data={"username": "family", "password": "nope"})
    assert r.status_code == 200
    assert b"Invalid credentials" in r.data


def test_good_login_redirects_to_next(client):
    r = client.post("/login?next=/doors", data={"username": "family", "password": "testpw"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/doors")


def test_login_rejects_open_redirect(client):
    r = client.post("/login?next=https://evil.example/", data={"username": "family", "password": "testpw"})
    assert r.status_code == 302
    assert "evil" not in r.headers["Location"]
    assert r.headers["Location"].endswith("/map")


def test_logout_clears_session(auth):
    assert auth.get("/map").status_code == 200
    auth.get("/logout")
    assert auth.get("/map").status_code == 302


def test_session_cookie_flags(client):
    r = client.post("/login", data={"username": "family", "password": "testpw"})
    cookie = r.headers.get("Set-Cookie", "")
    assert "SameSite=Lax" in cookie
    assert "HttpOnly" in cookie


# --- Pages -----------------------------------------------------------------

def test_pages_render(auth):
    for path in ("/map", "/inbox", "/inbox?as=mom", "/doors"):
        r = auth.get(path)
        assert r.status_code == 200, path


def test_inbox_unknown_driver_404_not_500(auth):
    assert auth.get("/inbox?as=bogus").status_code == 404


def test_map_does_not_leak_vins(auth, monkeypatch):
    r = auth.get("/map")
    assert b"tesla_vin" not in r.data


def test_inbox_renders_messages_with_readable_time(auth):
    models.add_message("dad", "lp", "dinner at 6")
    r = auth.get("/inbox?as=lp")
    assert b"dinner at 6" in r.data
    assert b"<time></time>" not in r.data


# --- JSON API --------------------------------------------------------------

def test_api_vehicles_shape(auth):
    rows = auth.get("/api/vehicles").get_json()
    assert {r["vehicle_key"] for r in rows} == {"dad", "lp", "mom"}
    assert set(rows[0]) >= {"latitude", "longitude", "speed_mph", "battery_pct", "online", "updated_at"}


def test_api_doors_seeded_unknown(auth):
    rows = auth.get("/api/doors").get_json()
    assert {r["door_key"] for r in rows} == {"garage1", "garage2", "garage3"}
    assert all(r["is_open"] is None for r in rows)


def test_api_message_roundtrip(auth, events):
    r = auth.post("/api/message", json={"sender_key": "dad", "recipient_key": "lp", "body": "  hi  "})
    assert r.status_code == 200 and r.get_json()["ok"]
    inbox = auth.get("/api/inbox/lp").get_json()
    assert inbox[0]["body"] == "hi"
    ev = json.loads(events.get_nowait())
    assert ev["event"] == "inbox" and ev["data"] == {"recipient_key": "lp"}


def test_api_message_validation(auth):
    assert auth.post("/api/message", json={"sender_key": "x", "recipient_key": "lp", "body": "hi"}).status_code == 400
    assert auth.post("/api/message", json={"sender_key": "dad", "recipient_key": "lp", "body": "   "}).status_code == 400
    assert auth.get("/api/inbox/nobody").status_code == 404


def test_api_inbox_read(auth):
    models.add_message("dad", "lp", "x")
    assert auth.post("/api/inbox/read", json={"recipient_key": "lp"}).status_code == 200
    assert models.list_inbox("lp")[0]["read_at"] is not None
    assert auth.post("/api/inbox/read", json={"recipient_key": "zz"}).status_code == 400


def test_api_door_fires_routine_and_publishes(auth, fired, events):
    r = auth.post("/api/door", json={"door_key": "garage2", "action": "open"})
    assert r.get_json() == {"ok": True, "routine": "Open Garage 2"}
    assert fired == ["Open Garage 2"]
    state = {d["door_key"]: d["is_open"] for d in models.all_door_states()}
    assert state["garage2"] == 1
    assert json.loads(events.get_nowait())["event"] == "doors"

    auth.post("/api/door", json={"door_key": "garage2", "action": "close"})
    assert fired[-1] == "Close Garage 2"
    assert {d["door_key"]: d["is_open"] for d in models.all_door_states()}["garage2"] == 0


def test_api_door_validation(auth, fired):
    assert auth.post("/api/door", json={"door_key": "garage9", "action": "open"}).status_code == 400
    assert auth.post("/api/door", json={"door_key": "garage1", "action": "wiggle"}).status_code == 400
    assert fired == []


def test_api_allowlist_filters_unknown_keys(auth):
    r = auth.post("/api/allowlist", json={"door_key": "garage1", "vehicle_keys": ["lp", "bogus"]})
    assert r.get_json()["allowlist"] == ["lp"]
    assert models.get_allowlist("garage1") == {"lp"}
    # Replacing the list clears old entries.
    auth.post("/api/allowlist", json={"door_key": "garage1", "vehicle_keys": []})
    assert models.get_allowlist("garage1") == set()
    assert auth.post("/api/allowlist", json={"door_key": "nope", "vehicle_keys": []}).status_code == 400


def test_api_requires_login_even_for_post(client):
    r = client.post("/api/door", json={"door_key": "garage1", "action": "open"})
    assert r.status_code == 302


# --- SSE -------------------------------------------------------------------

def test_stream_hello_and_event(auth):
    from eventbus import bus
    r = auth.get("/stream")
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    gen = r.response
    first = next(gen)
    assert first.startswith(b"event: hello")
    bus.publish("doors", [{"door_key": "garage1", "is_open": 1}])
    second = next(gen)
    assert second.startswith(b"data: ")
    payload = json.loads(second[len(b"data: "):].strip())
    assert payload["event"] == "doors"
    gen.close()


# --- Twilio webhook --------------------------------------------------------

def test_sms_webhook_returns_twiml(client, monkeypatch):
    import sms
    monkeypatch.setitem(sms.DRIVER_PHONES, "lp", "+15551234567")
    r = client.post("/sms", data={"From": "+15551234567", "Body": "omw"})
    assert r.status_code == 200
    assert r.mimetype == "text/xml"
    assert b"<Response></Response>" in r.data
    assert models.list_inbox("lp")[0]["body"] == "omw"


def test_sms_webhook_rejects_bad_signature_when_configured(client, monkeypatch):
    import sms
    monkeypatch.setattr(sms, "TW_TOKEN", "secret")
    r = client.post("/sms", data={"From": "+1", "Body": "x"}, headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403
