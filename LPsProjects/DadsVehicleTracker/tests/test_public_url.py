"""Is the site reachable from outside the house?

The gap this closes, found on 2026-11-08: the app was serving happily on
127.0.0.1, every health check reported clear, the watchdog had logged "ok"
every five minutes for days -- and tracker.taild11993.ts.net did not resolve
at all. The Tailscale node had been logged out, which silently discards the
Funnel config. Nobody could reach the site from a phone or a car, and
nothing in the system had any way to notice, because every existing check
asks a question from inside the box.
"""
from __future__ import annotations

import json
import time

import pytest

import health


@pytest.fixture(autouse=True)
def _clear_cache():
    health._public.at = 0.0
    health._public.value = None
    yield
    health._public.at = 0.0
    health._public.value = None


def _fake(monkeypatch, resolves, state, funnel):
    monkeypatch.setattr(health, "_resolves_publicly", lambda host: resolves)
    monkeypatch.setattr(health, "_tailscale_state", lambda: (state, funnel))


# --- the verdict ------------------------------------------------------------

def test_all_well_is_silent(monkeypatch):
    _fake(monkeypatch, True, "Running", True)
    assert health.check_public_url(time.time()) is None


def test_one_nxdomain_reading_is_tolerated(monkeypatch):
    """Measured on 2026-11-08: after the Funnel was restored and the site was
    verifiably loading from outside, both public resolvers still served a
    cached NXDOMAIN. Alerting on one reading would push a CRITICAL every
    time the Funnel is re-established.
    """
    _fake(monkeypatch, False, "Running", True)
    assert health.check_public_url(time.time()) is None


def test_persistent_nxdomain_is_critical(monkeypatch):
    """Tolerated once, not forever. At the 900 s cache these are 15 minutes
    apart, far longer than propagation needs."""
    _fake(monkeypatch, False, "Running", True)
    for _ in range(health.DNS_FAILS_BEFORE_ALERT):
        i = health.check_public_url(time.time())
        health._public.at = 0.0          # next sweep, cache expired
    assert i and i.severity == health.SEV_CRITICAL
    assert "NXDOMAIN" in i.detail


def test_dns_recovering_resets_the_counter(monkeypatch):
    _fake(monkeypatch, False, "Running", True)
    health.check_public_url(time.time())
    health._public.at = 0.0
    _fake(monkeypatch, True, "Running", True)
    health.check_public_url(time.time())
    health._public.at = 0.0
    assert health._public.dns_fails == 0
    _fake(monkeypatch, False, "Running", True)
    assert health.check_public_url(time.time()) is None, "counter restarted"


def test_a_logged_out_node_is_critical(monkeypatch):
    """DNS can lag a logout by minutes, so this must not wait for NXDOMAIN."""
    _fake(monkeypatch, True, "NoState", True)
    i = health.check_public_url(time.time())
    assert i and "logged out" in i.detail


def test_a_missing_funnel_is_critical(monkeypatch):
    """The node can be up and online with nothing published."""
    _fake(monkeypatch, True, "Running", False)
    i = health.check_public_url(time.time())
    assert i and "no Funnel" in i.detail


def test_the_real_outage_alerts_immediately_on_the_tailscale_signals(monkeypatch):
    """What the live system returned on 2026-11-08. Tailscale's own state is
    local and authoritative with no propagation delay, so it must alert on
    the first reading rather than waiting out the DNS counter."""
    _fake(monkeypatch, False, "NoState", False)
    i = health.check_public_url(time.time())
    assert i and i.severity == health.SEV_CRITICAL
    assert "logged out" in i.detail and "no Funnel" in i.detail


def test_a_known_cause_is_not_padded_with_the_dns_reading(monkeypatch):
    """When Tailscale says it is down, that IS the cause. Adding "and DNS
    does not resolve either" is noise -- of course it does not."""
    _fake(monkeypatch, False, "NoState", False)
    i = health.check_public_url(time.time())
    assert "NXDOMAIN" not in i.detail


def test_the_alert_says_how_to_fix_it_with_the_current_cli(monkeypatch):
    """The first draft of this alert carried `tailscale funnel --bg 443 on`,
    which the 1.102 CLI rejects outright -- advice that wastes the reader's
    time at the moment they are least able to afford it.
    """
    _fake(monkeypatch, None, "NoState", False)
    detail = health.check_public_url(time.time()).detail
    assert "tailscale funnel --bg 5000" in detail
    assert "--hostname=tracker" in detail, "omitting it changes the public URL"
    assert "tailscale-ipn.exe" in detail, "the actual root cause on 2026-11-08"
    assert "443 on" not in detail, "that is the old syntax and it fails"


# --- what must NOT raise an alarm ------------------------------------------

def test_being_unable_to_ask_is_not_an_outage(monkeypatch):
    """An internet blip must not be reported as the site being unpublished.
    check_wan already covers losing the connection, and two alerts for one
    cause is how alert fatigue starts.
    """
    _fake(monkeypatch, None, None, None)
    assert health.check_public_url(time.time()) is None


def test_a_missing_tailscale_cli_does_not_manufacture_an_alert(monkeypatch):
    monkeypatch.setattr(health, "TAILSCALE_EXE", r"C:\nope\tailscale.exe")
    assert health._tailscale_state() == (None, None)


def test_dns_alone_is_enough_when_tailscale_cannot_be_read(monkeypatch):
    _fake(monkeypatch, False, None, None)
    i = health.check_public_url(time.time())
    assert i and "NXDOMAIN" in i.detail


# --- it must not be a check that cannot fail -------------------------------

def test_the_verdict_does_not_come_from_fetching_our_own_url(monkeypatch):
    """The trap this check exists to avoid.

    From the node itself, a request to the Funnel hostname can be answered
    locally without crossing the internet -- measured at 50 ms on
    2026-11-01. A self-fetch would therefore have returned 200 throughout
    the outage. If anyone later "improves" this by curling the public URL,
    this test should start failing.
    """
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("must not fetch the public URL to decide")

    monkeypatch.setattr(health.urllib.request, "urlopen", boom)
    monkeypatch.setattr(health, "_resolves_publicly", lambda host: True)
    monkeypatch.setattr(health, "_tailscale_state", lambda: ("Running", True))
    assert health.check_public_url(time.time()) is None
    assert calls == []


# --- the resolver helper ---------------------------------------------------

def _resolver(monkeypatch, payload):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(health.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())


def test_nxdomain_status_is_read_as_absent(monkeypatch):
    _resolver(monkeypatch, {"Status": 3})
    assert health._resolves_publicly("x.example") is False


def test_an_a_record_is_read_as_present(monkeypatch):
    _resolver(monkeypatch, {"Status": 0,
                            "Answer": [{"type": 1, "data": "1.2.3.4"}]})
    assert health._resolves_publicly("x.example") is True


def test_noerror_with_no_a_record_is_still_nothing_to_connect_to(monkeypatch):
    """A CNAME-only or empty NOERROR answer gives a browser nowhere to go."""
    _resolver(monkeypatch, {"Status": 0, "Answer": [{"type": 5}]})
    assert health._resolves_publicly("x.example") is False


def test_both_resolvers_failing_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(health.urllib.request, "urlopen", boom)
    assert health._resolves_publicly("x.example") is None


# --- caching ---------------------------------------------------------------

def test_the_answer_is_cached_rather_than_asked_every_sweep(monkeypatch):
    """The health loop runs every 300 s; this makes outbound DNS calls and
    shells out twice, so it must not run on every pass."""
    calls = []
    monkeypatch.setattr(health, "_resolves_publicly",
                        lambda host: calls.append(1) or True)
    monkeypatch.setattr(health, "_tailscale_state", lambda: ("Running", True))
    now = time.time()
    health.public_url_published(now)
    health.public_url_published(now + 60)
    health.public_url_published(now + 120)
    assert len(calls) == 1
    health.public_url_published(now + health.PUBLIC_CHECK_INTERVAL_S + 1)
    assert len(calls) == 2


def test_force_bypasses_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(health, "_resolves_publicly",
                        lambda host: calls.append(1) or True)
    monkeypatch.setattr(health, "_tailscale_state", lambda: ("Running", True))
    now = time.time()
    health.public_url_published(now)
    health.public_url_published(now, force=True)
    assert len(calls) == 2


# --- it is wired into the sweep -------------------------------------------

def test_the_check_runs_as_part_of_check(monkeypatch):
    """Registering it in check() is the step that makes it real, and the one
    a unit test of the function alone would miss."""
    _fake(monkeypatch, False, "NoState", False)
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: True)
    keys = {i.key for i in health.check(time.time())}
    assert "public_url" in keys


def test_a_broken_check_cannot_take_the_sweep_down(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tailscale exploded")

    monkeypatch.setattr(health, "check_public_url", boom)
    monkeypatch.setattr(health, "broker_reachable", lambda *a, **k: True)
    health.check(time.time())   # must not raise
