"""Failed-login recording, lockout and throttling.

Before this existed a brute-force attempt against the public URL left no
trace whatsoever, so these tests care as much about the logging as the
blocking.
"""
from __future__ import annotations

import logging

import pytest

import login_guard
from login_guard import LoginGuard


@pytest.fixture(autouse=True)
def _fresh_guard():
    login_guard.guard.reset()
    yield
    login_guard.guard.reset()


# --- identifying the caller -------------------------------------------------

def test_forwarded_address_is_preferred_over_the_proxy_address():
    """Everything through Tailscale Serve/Funnel arrives from 127.0.0.1, so
    remote_addr alone would lump the whole internet in with the family."""
    headers = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    assert login_guard.client_key(headers, "127.0.0.1") == "203.0.113.9"


def test_tailnet_identity_used_when_there_is_no_forwarded_address():
    headers = {"Tailscale-User-Login": "patrick@example.com"}
    assert login_guard.client_key(headers, "127.0.0.1") == "tailnet:patrick@example.com"


def test_falls_back_to_remote_address():
    assert login_guard.client_key({}, "192.168.1.55") == "192.168.1.55"
    assert login_guard.client_key({}, None) == "unknown"


def test_describe_collects_useful_context():
    d = login_guard.describe({"User-Agent": "curl/8", "X-Forwarded-For": "203.0.113.9"})
    assert "curl/8" in d and "203.0.113.9" in d
    assert login_guard.describe({}) == ""


# --- per-key lockout --------------------------------------------------------

def test_lockout_after_the_threshold():
    g = LoginGuard()
    now = 1000.0
    for i in range(login_guard.MAX_FAILURES - 1):
        assert g.record_failure("k", now=now + i) == 0.0
        assert g.locked_out("k", now=now + i) == 0.0
    assert g.record_failure("k", now=now + 10) == login_guard.LOCKOUT_S
    assert g.locked_out("k", now=now + 10) == login_guard.LOCKOUT_S


def test_lockout_expires():
    g = LoginGuard()
    now = 1000.0
    last = now
    for i in range(login_guard.MAX_FAILURES):
        last = now + i
        g.record_failure("k", now=last)
    # The clock starts at the LAST failure, not the first.
    assert g.locked_out("k", now=last + login_guard.LOCKOUT_S - 1) > 0
    assert g.locked_out("k", now=last + login_guard.LOCKOUT_S + 1) == 0.0


def test_failures_age_out_of_the_window():
    """Four wrong guesses spread over an hour must not add up to a lockout."""
    g = LoginGuard()
    for i in range(10):
        g.record_failure("k", now=1000.0 + i * (login_guard.WINDOW_S + 1))
        assert g.locked_out("k", now=1000.0 + i * (login_guard.WINDOW_S + 1)) == 0.0


def test_lockout_is_per_key():
    g = LoginGuard()
    for i in range(login_guard.MAX_FAILURES):
        g.record_failure("attacker", now=1000.0 + i)
    assert g.locked_out("attacker", now=1005.0) > 0
    assert g.locked_out("family", now=1005.0) == 0.0


def test_success_clears_the_record():
    g = LoginGuard()
    for i in range(login_guard.MAX_FAILURES - 1):
        g.record_failure("k", now=1000.0 + i)
    g.record_success("k")
    for i in range(login_guard.MAX_FAILURES - 1):
        assert g.record_failure("k", now=2000.0 + i) == 0.0


# --- global tier ------------------------------------------------------------

def test_global_tier_delays_rather_than_locking_everyone_out():
    """A global lockout would let anyone shut the family out, so the shared
    response is a delay."""
    g = LoginGuard()
    now = 1000.0
    for i in range(login_guard.GLOBAL_THRESHOLD):
        g.record_failure(f"key{i}", now=now + i * 0.1)
    assert g.under_attack(now=now + 1) is True
    assert g.delay_for_failure(now=now + 1) > login_guard.FAILURE_DELAY_S
    # No individual key is locked out by the global tier alone.
    assert g.locked_out("key0", now=now + 1) == 0.0


def test_global_pressure_decays():
    g = LoginGuard()
    now = 1000.0
    for i in range(login_guard.GLOBAL_THRESHOLD):
        g.record_failure(f"key{i}", now=now + i * 0.1)
    assert g.under_attack(now=now + login_guard.GLOBAL_WINDOW_S + 1) is False


def test_every_failure_costs_time_even_the_first():
    g = LoginGuard()
    assert g.delay_for_failure(now=1000.0) >= login_guard.FAILURE_DELAY_S


# --- logging ----------------------------------------------------------------

def test_failures_are_logged_with_context(caplog):
    g = LoginGuard()
    caplog.set_level(logging.WARNING)
    g.record_failure("203.0.113.9", "(User-Agent=curl/8)", now=1000.0)
    rec = [r for r in caplog.records if "Failed login" in r.message]
    assert rec, "a failed login must leave a trace"
    assert "203.0.113.9" in rec[0].getMessage()
    assert "curl/8" in rec[0].getMessage()


def test_lockout_is_logged(caplog):
    g = LoginGuard()
    caplog.set_level(logging.WARNING)
    for i in range(login_guard.MAX_FAILURES):
        g.record_failure("k", now=1000.0 + i)
    assert any("LOCKED OUT" in r.getMessage() for r in caplog.records)


# --- through the app --------------------------------------------------------

def test_wrong_password_is_rejected_and_counted(client, monkeypatch):
    monkeypatch.setattr(login_guard, "FAILURE_DELAY_S", 0.0)
    r = client.post("/login", data={"username": "family", "password": "nope"})
    assert r.status_code == 200
    assert b"Invalid credentials" in r.data
    assert login_guard.guard.locked_out("unknown") == 0.0


def test_repeated_wrong_passwords_lock_the_route(client, monkeypatch):
    monkeypatch.setattr(login_guard, "FAILURE_DELAY_S", 0.0)
    monkeypatch.setattr(login_guard, "GLOBAL_DELAY_S", 0.0)
    for _ in range(login_guard.MAX_FAILURES):
        client.post("/login", data={"username": "family", "password": "nope"})
    r = client.post("/login", data={"username": "family", "password": "nope"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert b"Too many failed attempts" in r.data


def test_lockout_blocks_even_the_correct_password(client, monkeypatch):
    """Otherwise an attacker who finds the password mid-lockout walks in."""
    monkeypatch.setattr(login_guard, "FAILURE_DELAY_S", 0.0)
    monkeypatch.setattr(login_guard, "GLOBAL_DELAY_S", 0.0)
    for _ in range(login_guard.MAX_FAILURES):
        client.post("/login", data={"username": "family", "password": "nope"})
    r = client.post("/login", data={"username": "family", "password": "testpw"})
    assert r.status_code == 429


def test_separate_clients_are_not_punished_together(client, monkeypatch):
    monkeypatch.setattr(login_guard, "FAILURE_DELAY_S", 0.0)
    monkeypatch.setattr(login_guard, "GLOBAL_DELAY_S", 0.0)
    for _ in range(login_guard.MAX_FAILURES):
        client.post("/login", data={"username": "family", "password": "nope"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    blocked = client.post("/login", data={"username": "family", "password": "nope"},
                          headers={"X-Forwarded-For": "203.0.113.9"})
    assert blocked.status_code == 429
    ok = client.post("/login", data={"username": "family", "password": "testpw"},
                     headers={"X-Forwarded-For": "198.51.100.4"})
    assert ok.status_code == 302, "the family must still be able to sign in"


def test_successful_login_still_works_and_clears_failures(client, monkeypatch):
    monkeypatch.setattr(login_guard, "FAILURE_DELAY_S", 0.0)
    client.post("/login", data={"username": "family", "password": "nope"},
                headers={"X-Forwarded-For": "203.0.113.9"})
    r = client.post("/login", data={"username": "family", "password": "testpw"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 302
    for _ in range(login_guard.MAX_FAILURES - 1):
        r = client.post("/login", data={"username": "family", "password": "nope"},
                        headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200, "counter should have been reset by the success"
