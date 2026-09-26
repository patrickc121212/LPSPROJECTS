"""Local Shelly RPC client, the per-door actuation choice, and the
already-open guard that stops a toggle pulse closing a door on a car."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from urllib.error import HTTPError

import pytest

import config
import door_control
import geofence_worker as gw
import models
import shelly
from conftest import at, owned_by


# --- RPC client -------------------------------------------------------------

class FakeResp:
    def __init__(self, body: str):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    """Records requested URLs and replies per RPC method name."""

    def __init__(self):
        self.seen: list[str] = []
        self.replies: dict[str, object] = {}
        self.raise_on: dict[str, Exception] = {}

    def urlopen(self, url, timeout=None):
        self.seen.append(url)
        for name, exc in self.raise_on.items():
            if f"/rpc/{name}" in url and name in url or (f"/rpc/{name}" in url and not name):
                raise exc
        for name, rep in self.replies.items():
            if f"/rpc/{name}" in url:
                return FakeResp(json.dumps(rep))
        return FakeResp("{}")


@pytest.fixture
def urls(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(shelly.urllib.request, "urlopen", rec.urlopen)
    return rec


def test_pulse_sends_toggle_after_and_json_booleans(urls):
    ok, detail = shelly.pulse("1.2.3.4", channel=0, pulse_s=0.5)
    assert ok and "0.5" in detail
    url = urls.seen[0]
    assert url.startswith("http://1.2.3.4/rpc/Switch.Set?")
    # Shelly needs JSON literals: on=true, not on=True.
    assert "on=true" in url and "id=0" in url and "toggle_after=0.5" in url


def test_pulse_falls_back_when_toggle_after_unsupported(monkeypatch):
    seen: list[str] = []

    def fake(url, timeout=None):
        seen.append(url)
        if "toggle_after" in url:
            raise HTTPError(url, 400, "unknown key toggle_after", {}, None)  # type: ignore[arg-type]
        return FakeResp("{}")

    monkeypatch.setattr(shelly.urllib.request, "urlopen", fake)
    ok, _ = shelly.pulse("1.2.3.4")
    assert ok
    assert len(seen) == 2 and "toggle_after" not in seen[1]


def test_pulse_reports_failure(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(shelly.urllib.request, "urlopen", boom)
    ok, detail = shelly.pulse("1.2.3.4")
    assert ok is False and "no route" in detail


def test_get_status_shapes_payload(urls):
    urls.replies["Switch.GetStatus"] = {"output": False, "temperature": {"tC": 47.3},
                                        "counts": {"switch_on": 5}}
    urls.replies["Input.GetStatus"] = {"state": None}
    st = shelly.get_status("1.2.3.4")
    assert st == {"host": "1.2.3.4", "output": False, "input_state": None,
                  "temperature_c": 47.3, "switch_on_count": 5}


def test_get_status_none_when_unreachable(monkeypatch):
    monkeypatch.setattr(shelly.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert shelly.get_status("1.2.3.4") is None


def test_check_pulse_config_flags_latching_relay(urls):
    urls.replies["Switch.GetConfig"] = {"auto_off": False}
    ok, msg = shelly.check_pulse_config("1.2.3.4")
    assert ok is False and "latch" in msg.lower()

    urls.replies["Switch.GetConfig"] = {"auto_off": True, "auto_off_delay": 30.0}
    ok, msg = shelly.check_pulse_config("1.2.3.4")
    assert ok is False and "30" in msg

    urls.replies["Switch.GetConfig"] = {"auto_off": True, "auto_off_delay": 0.5}
    ok, _ = shelly.check_pulse_config("1.2.3.4")
    assert ok is True


# --- per-door routing -------------------------------------------------------

def _shelly_door(host="10.0.0.9"):
    return replace(config.GARAGE_DOORS[1], shelly_host=host)


def test_actuate_prefers_shelly(pulses):
    d = _shelly_door()
    res = door_control.actuate(d, "open")
    assert res["via"] == "shelly" and res["ok"]
    assert pulses == [("10.0.0.9", 0)]


def test_actuate_open_and_close_are_the_same_pulse(pulses):
    d = _shelly_door()
    door_control.actuate(d, "open")
    door_control.actuate(d, "close")
    assert pulses == [("10.0.0.9", 0), ("10.0.0.9", 0)]
    assert door_control.is_toggle(d) is True


def test_actuate_falls_back_to_dry_run_without_shelly_or_webhook(pulses):
    d = replace(config.GARAGE_DOORS[0], shelly_host="")
    res = door_control.actuate(d, "open")
    assert res["via"] == "dry-run" and res["detail"] == d.routine_open
    assert pulses == []
    assert door_control.is_toggle(d) is False


def test_actuate_uses_routine_webhook_when_configured(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(door_control, "WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setattr(door_control, "_fire_routine", lambda n: (sent.append(n), (True, n))[1])
    d = replace(config.GARAGE_DOORS[0], shelly_host="")
    assert door_control.actuate(d, "close")["via"] == "routine"
    assert sent == [d.routine_close]


def test_shelly_failure_does_not_record_door_as_open(auth, monkeypatch):
    """A relay we couldn't reach must not leave the UI claiming 'OPEN'."""
    d = _shelly_door()
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {**config.GARAGE_DOORS_BY_KEY, d.key: d})
    monkeypatch.setattr(shelly, "pulse", lambda *a, **k: (False, "timed out"))
    r = auth.post("/api/door", json={"door_key": d.key, "action": "open"})
    assert r.get_json()["ok"] is False
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] is None


# --- the already-open guard -------------------------------------------------

def _states(now, is_open, age_s=0):
    return {"garage2": {"door_key": "garage2", "is_open": is_open, "updated_at": now - age_s}}


def test_believed_open_only_while_fresh():
    now = 1_000_000.0
    assert door_control.believed_open("garage2", _states(now, 1, 10), now) is True
    assert door_control.believed_open("garage2", _states(now, 1, 10_000), now) is False
    assert door_control.believed_open("garage2", _states(now, 0, 10), now) is False
    assert door_control.believed_open("garage2", _states(now, None, 10), now) is False
    assert door_control.believed_open("garage2", {}, now) is False


def test_geofence_skips_door_believed_open():
    """The dangerous case: car arrives, door already up, a toggle pulse
    would shut it on them."""
    d = owned_by("dad")
    now = 1_000_000.0
    gw.evaluate({"dad": at(d, 500)}, {}, now)                      # baseline: outside
    fresh_open = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now}}
    assert gw.evaluate({"dad": at(d, 5)}, {}, now + 30, fresh_open) == []


def test_geofence_fires_when_belief_is_stale():
    d = owned_by("dad")
    now = 1_000_000.0
    gw.evaluate({"dad": at(d, 500)}, {}, now)
    stale = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now - 99_999}}
    assert gw.evaluate({"dad": at(d, 5)}, {}, now + 30, stale) == [("dad", d.key)]


def test_geofence_fires_when_door_state_unknown():
    d = owned_by("dad")
    now = 1_000_000.0
    gw.evaluate({"dad": at(d, 500)}, {}, now)
    unknown = {d.key: {"door_key": d.key, "is_open": None, "updated_at": now}}
    assert gw.evaluate({"dad": at(d, 5)}, {}, now + 30, unknown) == [("dad", d.key)]


def test_guard_does_not_leave_a_pending_fire_behind():
    """Once skipped, the crossing is consumed: the car must leave and come
    back to arm auto-open again rather than firing the instant the belief
    expires."""
    d = owned_by("dad")
    now = 1_000_000.0
    gw.evaluate({"dad": at(d, 500)}, {}, now)
    fresh = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now}}
    assert gw.evaluate({"dad": at(d, 5)}, {}, now + 30, fresh) == []
    # Belief expires while the car sits inside: still no fire (no new edge).
    stale = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now - 99_999}}
    assert gw.evaluate({"dad": at(d, 5)}, {}, now + 9_999, stale) == []


def test_tick_does_not_mark_open_when_actuation_fails(monkeypatch):
    d = owned_by("dad")
    monkeypatch.setattr(door_control, "actuate",
                        lambda door, action: {"ok": False, "via": "shelly", "detail": "timeout"})
    models.upsert_vehicle_state("dad", at(d, 500)["latitude"], d.longitude, 30, 80, True)
    gw._tick()
    models.upsert_vehicle_state("dad", d.latitude, d.longitude, 5, 80, True)
    gw._tick()
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] is None


def test_end_to_end_arrival_pulses_the_relay(pulses, monkeypatch):
    """Telemetry position -> geofence -> real Shelly pulse, once."""
    d = replace(owned_by("dad"), shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    models.upsert_vehicle_state("dad", at(d, 500)["latitude"], d.longitude, 30, 80, True)
    gw._tick()
    assert pulses == []
    models.upsert_vehicle_state("dad", d.latitude, d.longitude, 5, 80, True)
    gw._tick()
    assert pulses == [("10.0.0.9", 0)]
    # Sitting in the garage must not pulse again.
    for _ in range(5):
        gw._tick()
    assert len(pulses) == 1
    assert time.time() > 0  # keep the import honest


# --- departure clears the stale belief --------------------------------------

def _assume(states, door_states, now):
    return gw.departure_actions(states, door_states, now)[1]


def test_owner_leaving_clears_a_stale_open_belief():
    """A quick trip out and back must still auto-open: leaving the fence
    is our proxy for 'the door closed behind them'."""
    d = owned_by("dad")
    now = 1_000_000.0
    open_now = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now}}
    assert _assume({"dad": at(d, 500)}, open_now, now) == [d.key]
    gw._away_since.clear()
    assert _assume({"dad": at(d, 5)}, open_now, now) == []


def test_departure_reset_ignores_doors_not_believed_open():
    d = owned_by("dad")
    now = 1_000_000.0
    shut = {d.key: {"door_key": d.key, "is_open": 0, "updated_at": now}}
    assert _assume({"dad": at(d, 500)}, shut, now) == []


def test_departure_reset_ignores_vehicles_without_a_fix():
    d = owned_by("dad")
    now = 1_000_000.0
    open_now = {d.key: {"door_key": d.key, "is_open": 1, "updated_at": now}}
    no_fix = {"dad": {"latitude": None, "longitude": None}}
    assert _assume(no_fix, open_now, now) == []


def test_quick_trip_out_and_back_reopens(pulses, monkeypatch):
    """Full cycle through SQLite: arrive (pulse), leave (belief cleared),
    arrive again (pulse again) — all inside the belief TTL."""
    d = replace(owned_by("dad"), shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(config, "GARAGE_DOORS_BY_KEY", {d.key: d})
    monkeypatch.setattr(config, "GEOFENCE_DEBOUNCE_S", 0)

    base = time.time()
    clock = {"t": base}
    monkeypatch.setattr(gw.time, "time", lambda: clock["t"])

    def put(offset_m):
        models.upsert_vehicle_state("dad", at(d, offset_m)["latitude"], d.longitude, 20, 80, True)

    put(500); gw._tick()           # baseline outside
    put(5);   gw._tick()           # arrive -> pulse
    assert len(pulses) == 1
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 1
    # Past the 60 s action cooldown, but well inside DOOR_OPEN_TTL_S so the
    # "open" belief is still live and there is something to clear.
    clock["t"] = base + 120
    put(500); gw._tick()           # leave -> belief cleared
    assert {x["door_key"]: x["is_open"] for x in models.all_door_states()}[d.key] == 0
    clock["t"] = base + 240
    put(5);   gw._tick()           # arrive again -> pulses again
    assert len(pulses) == 2


def test_request_tick_sets_the_wake_event():
    gw._wake.clear()
    assert gw._wake.is_set() is False
    gw.request_tick()
    assert gw._wake.is_set() is True


def test_radius_is_configurable_per_door(monkeypatch):
    """A bigger fence buys approach time; it must come from env, not code."""
    import importlib
    monkeypatch.setenv("GARAGE2_RADIUS_M", "150")
    cfg = importlib.reload(config)
    try:
        assert cfg.GARAGE_DOORS_BY_KEY["garage2"].radius_m == 150.0
    finally:
        monkeypatch.delenv("GARAGE2_RADIUS_M", raising=False)
        importlib.reload(cfg)
