"""Health checks, and the rule that they only speak when something changes.

These exist because three separate outages were invisible: the site served
pages perfectly while telemetry was dead for ten hours, while the relay had
moved to a new DHCP address, and while the WAN address had changed so the
cars could not reach us.
"""
from __future__ import annotations

import logging
import time

import pytest

import config
import health
import models
import notify
import shelly


@pytest.fixture(autouse=True)
def _reset():
    health._previous = set()
    health._wan.at, health._wan.value = 0.0, None
    yield
    health._previous = set()


@pytest.fixture
def pushed(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notify, "send",
                        lambda title, message, priority="default", tags="", click="":
                        (sent.append((title, message, priority)), True)[1])
    return sent


# --- MQTT: the ten-hour outage ----------------------------------------------

def test_broker_unreachable_is_critical(monkeypatch):
    """The ten-hour outage: Docker down, so nothing is listening."""
    monkeypatch.setattr(config, "VEHICLE_SOURCE", "telemetry")
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: False)
    issue = health.check_mqtt()
    assert issue is not None and issue.severity == health.SEV_CRITICAL
    assert "telemetry" in issue.message.lower()


def test_broker_up_and_subscribed_is_silent(monkeypatch):
    import telemetry_worker
    monkeypatch.setattr(config, "VEHICLE_SOURCE", "telemetry")
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: True)
    monkeypatch.setattr(telemetry_worker, "worker_running", lambda: True)
    monkeypatch.setattr(telemetry_worker, "mqtt_connected", lambda: True)
    assert health.check_mqtt() is None


def test_a_process_without_the_worker_is_not_told_it_is_disconnected(monkeypatch):
    """A console check has no MQTT connection of its own and should not
    report one missing."""
    import telemetry_worker
    monkeypatch.setattr(config, "VEHICLE_SOURCE", "telemetry")
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: True)
    monkeypatch.setattr(telemetry_worker, "worker_running", lambda: False)
    monkeypatch.setattr(telemetry_worker, "mqtt_connected", lambda: False)
    assert health.check_mqtt() is None


def test_broker_up_but_not_subscribed_is_only_a_warning(monkeypatch):
    """paho reconnects by itself, so this is worth saying but not alarming."""
    import telemetry_worker
    monkeypatch.setattr(config, "VEHICLE_SOURCE", "telemetry")
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: True)
    monkeypatch.setattr(telemetry_worker, "worker_running", lambda: True)
    monkeypatch.setattr(telemetry_worker, "mqtt_connected", lambda: False)
    assert health.check_mqtt().severity == health.SEV_WARNING


def test_broker_probe_does_not_use_in_process_state(monkeypatch):
    """A console check must not report a false outage just because it is not
    the process holding the MQTT connection."""
    import telemetry_worker
    telemetry_worker._mqtt_up.clear()
    monkeypatch.setattr(health.socket, "create_connection",
                        lambda *a, **k: __import__("contextlib").nullcontext())
    assert health.broker_reachable("127.0.0.1", 1883) is True


def test_mqtt_not_checked_for_other_sources(monkeypatch):
    """The simulator and the poller do not use MQTT; complaining would be
    noise."""
    monkeypatch.setattr(config, "VEHICLE_SOURCE", "sim")
    assert health.check_mqtt() is None


def test_mqtt_flag_tracks_the_connection():
    import telemetry_worker
    telemetry_worker._mqtt_up.clear()
    assert telemetry_worker.mqtt_connected() is False
    telemetry_worker._mqtt_up.set()
    assert telemetry_worker.mqtt_connected() is True
    telemetry_worker._mqtt_up.clear()


# --- relays: the moved DHCP lease -------------------------------------------

def test_unreachable_relay_is_critical(monkeypatch):
    from dataclasses import replace
    d = replace(config.GARAGE_DOORS[1], shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(shelly, "check_pulse_config",
                        lambda *a, **k: (False, "unreachable: timed out"))
    issues = health.check_doors()
    assert len(issues) == 1
    assert issues[0].severity == health.SEV_CRITICAL
    assert "10.0.0.9" in issues[0].detail


def test_misconfigured_relay_is_a_warning_not_critical(monkeypatch):
    from dataclasses import replace
    d = replace(config.GARAGE_DOORS[1], shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(shelly, "check_pulse_config",
                        lambda *a, **k: (False, "auto_off is DISABLED"))
    assert health.check_doors()[0].severity == health.SEV_WARNING


def test_healthy_relay_is_silent(monkeypatch):
    from dataclasses import replace
    d = replace(config.GARAGE_DOORS[1], shelly_host="10.0.0.9")
    monkeypatch.setattr(config, "GARAGE_DOORS", [d])
    monkeypatch.setattr(shelly, "check_pulse_config", lambda *a, **k: (True, "auto_off 0.5s"))
    assert health.check_doors() == []


def test_doors_without_a_relay_are_not_checked(monkeypatch):
    assert health.check_doors() == []


# --- vehicle silence: must not cry wolf -------------------------------------

def test_a_parked_fleet_overnight_is_not_an_alert():
    """Teslas report nothing while asleep. Eight hours of quiet is a normal
    night, not a fault."""
    # upsert stamps rows with real wall-clock, so anchor on that.
    now = time.time()
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, False)
    assert health.check_vehicle_data(now + 8 * 3600) is None


def test_implausibly_long_silence_is_flagged():
    now = time.time()
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, False)
    issue = health.check_vehicle_data(now + (health.NO_DATA_HOURS + 2) * 3600)
    assert issue is not None and issue.severity == health.SEV_WARNING


def test_one_recent_car_keeps_it_quiet():
    """One car reporting means the pipeline works, whatever the others do."""
    now = time.time()
    models.upsert_vehicle_state("dad", 1.0, 2.0, 0, 80, False)
    models.upsert_vehicle_state("lp", 1.0, 2.0, 0, 80, True)
    assert health.check_vehicle_data(now) is None


# --- WAN address: the cars could not reach us -------------------------------

def test_wan_mismatch_is_critical(monkeypatch):
    monkeypatch.setattr(health, "wan_matches_dns",
                        lambda now, force=False: ("174.85.214.133", "68.184.61.239"))
    issue = health.check_wan(1000.0)
    assert issue is not None and issue.severity == health.SEV_CRITICAL
    assert "174.85.214.133" in issue.detail and "68.184.61.239" in issue.detail


def test_wan_match_is_silent(monkeypatch):
    monkeypatch.setattr(health, "wan_matches_dns",
                        lambda now, force=False: ("1.2.3.4", "1.2.3.4"))
    assert health.check_wan(1000.0) is None


def test_wan_unknown_is_silent(monkeypatch):
    """No internet, or no TELEMETRY_HOST: say nothing rather than guess."""
    monkeypatch.setattr(health, "wan_matches_dns", lambda now, force=False: None)
    assert health.check_wan(1000.0) is None


def test_wan_lookup_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        class R:
            def read(self_inner): return b"1.2.3.4"
        return R()

    monkeypatch.setattr(health.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(health.socket, "gethostbyname", lambda h: "1.2.3.4")
    monkeypatch.setenv("TELEMETRY_HOST", "telemetry.example.com")
    health.wan_matches_dns(1000.0)
    health.wan_matches_dns(1000.0 + 10)
    assert calls["n"] == 1, "must not hit the network on every page load"


# --- notification discipline ------------------------------------------------

def test_new_problem_pushes_once(pushed):
    issue = health.Issue("mqtt", health.SEV_CRITICAL, "Telemetry down", "detail")
    assert health.notify_changes([issue]) == ["mqtt"]
    assert len(pushed) == 1 and pushed[0][2] == "high"
    # Still broken five minutes later: no second push.
    assert health.notify_changes([issue]) == []
    assert len(pushed) == 1


def test_recovery_is_announced(pushed):
    issue = health.Issue("mqtt", health.SEV_CRITICAL, "Telemetry down", "d")
    health.notify_changes([issue])
    sent = health.notify_changes([])
    assert "recovered" in sent
    assert any("healthy again" in t.lower() for t, _, _ in pushed)


def test_no_recovery_message_while_other_problems_remain(pushed):
    a = health.Issue("mqtt", health.SEV_CRITICAL, "Telemetry down", "d")
    b = health.Issue("wan", health.SEV_CRITICAL, "WAN changed", "d")
    health.notify_changes([a, b])
    sent = health.notify_changes([b])
    assert "recovered" not in sent


def test_a_second_distinct_problem_pushes(pushed):
    a = health.Issue("mqtt", health.SEV_CRITICAL, "Telemetry down", "d")
    b = health.Issue("door:garage2", health.SEV_CRITICAL, "Relay unreachable", "d")
    health.notify_changes([a])
    assert health.notify_changes([a, b]) == ["door:garage2"]
    assert len(pushed) == 2


def test_warnings_push_at_normal_priority(pushed):
    health.notify_changes([health.Issue("cert", health.SEV_WARNING, "Cert soon", "d")])
    assert pushed[0][2] == "default"


def test_problems_are_logged_even_without_push(caplog, monkeypatch):
    monkeypatch.setattr(notify, "TOPIC", "")
    caplog.set_level(logging.WARNING)
    health.notify_changes([health.Issue("mqtt", health.SEV_CRITICAL, "Telemetry down", "d")])
    assert any("HEALTH CRITICAL" in r.getMessage() for r in caplog.records)


# --- aggregation and routes -------------------------------------------------

def test_check_never_raises(monkeypatch):
    monkeypatch.setattr(health, "check_mqtt",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    health.check(1000.0)  # must not raise


def test_critical_sorts_before_warning():
    issues = [health.Issue("a", health.SEV_WARNING, "w"),
              health.Issue("b", health.SEV_CRITICAL, "c")]
    issues.sort(key=lambda i: 0 if i.severity == health.SEV_CRITICAL else 1)
    assert issues[0].severity == health.SEV_CRITICAL


def test_summary_shape():
    s = health.summary([health.Issue("a", health.SEV_CRITICAL, "c"),
                        health.Issue("b", health.SEV_WARNING, "w")])
    assert s["healthy"] is False and s["critical"] == 1 and s["warning"] == 1
    assert len(s["issues"]) == 2
    assert health.summary([])["healthy"] is True


def test_health_api(auth, monkeypatch):
    monkeypatch.setattr(health, "check", lambda now=None: [])
    body = auth.get("/api/health").get_json()
    assert body["healthy"] is True and body["issues"] == []


def test_health_api_requires_login(client):
    assert client.get("/api/health").status_code == 302


def test_banner_appears_on_every_page(auth, monkeypatch):
    monkeypatch.setattr(health, "check", lambda now=None: [
        health.Issue("mqtt", health.SEV_CRITICAL, "Not receiving vehicle telemetry", "Docker")])
    for path in ("/map", "/doors", "/charging"):
        assert b"Not receiving vehicle telemetry" in auth.get(path).data, path


def test_no_banner_when_healthy(auth, monkeypatch):
    monkeypatch.setattr(health, "check", lambda now=None: [])
    assert b'class="health' not in auth.get("/map").data


def test_banner_hidden_from_the_login_page(client, monkeypatch):
    monkeypatch.setattr(health, "check", lambda now=None: [
        health.Issue("mqtt", health.SEV_CRITICAL, "Not receiving vehicle telemetry", "d")])
    assert b"Not receiving" not in client.get("/login").data


# --- push transport ---------------------------------------------------------

def test_push_is_a_no_op_without_a_topic(monkeypatch, caplog):
    monkeypatch.setattr(notify, "TOPIC", "")
    caplog.set_level(logging.INFO)
    assert notify.send("t", "m") is True
    assert any("dry-run push" in r.getMessage() for r in caplog.records)


def test_push_posts_to_the_topic(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data
        seen["headers"] = dict(req.header_items())
        class R:
            def read(self_inner): return b"{}"
        return R()

    monkeypatch.setattr(notify, "TOPIC", "secret-topic")
    monkeypatch.setattr(notify, "SERVER", "https://ntfy.example")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.send("Title here", "Body here", priority="high", tags="warning")
    assert seen["url"] == "https://ntfy.example/secret-topic"
    assert seen["body"] == b"Body here"
    assert seen["headers"].get("Title") == "Title here"
    assert seen["headers"].get("Priority") == "high"


def test_push_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(notify, "TOPIC", "t")
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert notify.send("t", "m") is False  # reported, not raised


# --- self-healing: surviving a power cut and router reboot ------------------

def test_wan_change_is_corrected_not_merely_reported(monkeypatch):
    """A new WAN address used to break the cars silently. We hold a
    zone-scoped token, so fix the record instead of just complaining."""
    import ddns
    calls = []
    monkeypatch.setattr(health, "wan_matches_dns",
                        lambda now, force=False: ("5.6.7.8", "1.2.3.4"))
    monkeypatch.setattr(ddns, "reconcile",
                        lambda host, pub, dns: (calls.append((host, pub, dns)), "updated")[1])
    monkeypatch.setenv("TELEMETRY_HOST", "telemetry.example.com")
    issue = health.check_wan(1000.0)
    assert calls == [("telemetry.example.com", "5.6.7.8", "1.2.3.4")]
    # Still surfaced, because the router's forward may also need attention.
    assert issue.severity == health.SEV_WARNING
    assert "corrected automatically" in issue.message


def test_wan_change_that_cannot_be_fixed_stays_critical(monkeypatch):
    import ddns
    monkeypatch.setattr(health, "wan_matches_dns",
                        lambda now, force=False: ("5.6.7.8", "1.2.3.4"))
    monkeypatch.setattr(ddns, "reconcile", lambda host, pub, dns: "failed")
    assert health.check_wan(1000.0).severity == health.SEV_CRITICAL


def test_ddns_is_a_no_op_when_already_correct():
    import ddns
    assert ddns.reconcile("h", "1.2.3.4", "1.2.3.4") == "ok"


def test_ddns_respects_the_disable_switch(monkeypatch):
    import ddns
    monkeypatch.setattr(ddns, "ENABLED", False)
    assert ddns.reconcile("h", "5.6.7.8", "1.2.3.4") == "disabled"


def test_ddns_reports_failure_without_a_token(monkeypatch):
    import ddns
    monkeypatch.setattr(ddns, "ENABLED", True)
    monkeypatch.setattr(ddns, "token", lambda: "")
    assert ddns.reconcile("h", "5.6.7.8", "1.2.3.4") == "failed"


def test_ddns_reads_the_certbot_credentials_file(tmp_path, monkeypatch):
    """Reuse the credential that already issues the TLS certificate rather
    than asking for it twice."""
    import ddns
    ini = tmp_path / "cloudflare.ini"
    ini.write_text("# comment\ndns_cloudflare_api_token = abc123\n")
    monkeypatch.setattr(ddns, "CREDENTIALS", ini)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    assert ddns.token() == "abc123"


def test_ddns_prefers_the_environment_token(tmp_path, monkeypatch):
    import ddns
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "from-env")
    assert ddns.token() == "from-env"


def test_ddns_missing_credentials_is_survivable(tmp_path, monkeypatch):
    import ddns
    monkeypatch.setattr(ddns, "CREDENTIALS", tmp_path / "nope.ini")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    assert ddns.token() == ""
