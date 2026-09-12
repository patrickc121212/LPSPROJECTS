"""In-app inbox (source of truth), SMS fallback sweeper, and the Twilio
inbound webhook."""
from __future__ import annotations

import time

import models
import sms


def test_add_and_list_inbox():
    mid = models.add_message("dad", "lp", "leaving now")
    rows = models.list_inbox("lp")
    assert [r["id"] for r in rows] == [mid]
    assert rows[0]["read_at"] is None
    assert rows[0]["sms_sent_at"] is None
    assert models.list_inbox("dad") == []


def test_mark_read_sets_receipt():
    models.add_message("dad", "lp", "hi")
    assert models.last_seen("lp") is None
    models.mark_read("lp")
    assert models.list_inbox("lp")[0]["read_at"] is not None
    assert models.last_seen("lp") is not None


# --- SMS fallback ----------------------------------------------------------

def _setup_phone(monkeypatch, key="lp", phone="+15551234567"):
    monkeypatch.setitem(sms.DRIVER_PHONES, key, phone)
    return phone


def test_fallback_skips_fresh_messages(monkeypatch, sms_out):
    _setup_phone(monkeypatch)
    models.add_message("dad", "lp", "fresh")
    assert sms._maybe_send_fallbacks(now=time.time()) == 0
    assert sms_out == []


def test_fallback_sends_old_unread_once_and_keeps_unread(monkeypatch, sms_out):
    phone = _setup_phone(monkeypatch)
    mid = models.add_message("dad", "lp", "old one")
    later = time.time() + 600  # past SMS_FALLBACK_AFTER_S=300
    assert sms._maybe_send_fallbacks(now=later) == 1
    assert sms_out == [(phone, "[Dad] old one")]
    row = models.list_inbox("lp")[0]
    assert row["id"] == mid
    assert row["read_at"] is None, "SMS push must not mark the message read"
    assert row["sms_sent_at"] is not None
    # Second sweep: nothing new to send.
    assert sms._maybe_send_fallbacks(now=later + 60) == 0
    assert len(sms_out) == 1


def test_fallback_sends_each_old_message_not_just_first(monkeypatch, sms_out):
    _setup_phone(monkeypatch)
    models.add_message("dad", "lp", "one")
    models.add_message("mom", "lp", "two")
    assert sms._maybe_send_fallbacks(now=time.time() + 600) == 2
    assert [b for _, b in sms_out] == ["[Dad] one", "[Mom] two"]


def test_fallback_suppressed_if_driver_recently_active(monkeypatch, sms_out):
    _setup_phone(monkeypatch)
    models.add_message("dad", "lp", "old")
    models.add_message("mom", "lp", "new")
    models.mark_read("lp")  # driver opens inbox now -> everything read
    models.add_message("dad", "lp", "after")  # unread, but driver was just active
    now = time.time()
    # Pretend 'after' is old enough, but last_seen is still recent.
    assert sms._maybe_send_fallbacks(now=now + 100) == 0
    assert sms_out == []


def test_fallback_no_phone_no_sms(monkeypatch, sms_out):
    monkeypatch.setitem(sms.DRIVER_PHONES, "lp", "")
    models.add_message("dad", "lp", "x")
    assert sms._maybe_send_fallbacks(now=time.time() + 600) == 0


def test_fallback_failed_send_is_retried(monkeypatch):
    _setup_phone(monkeypatch)
    monkeypatch.setattr(sms, "send_sms", lambda to, body: False)
    models.add_message("dad", "lp", "x")
    assert sms._maybe_send_fallbacks(now=time.time() + 600) == 0
    assert models.list_inbox("lp")[0]["sms_sent_at"] is None


# --- Inbound SMS -----------------------------------------------------------

def test_inbound_unknown_number_ignored(events):
    assert sms.handle_inbound("+19990000000", "hello")["status"] == "unknown"
    assert events.empty()


def test_inbound_replies_to_last_sender(monkeypatch, events):
    import json
    monkeypatch.setitem(sms.DRIVER_PHONES, "lp", "+15551234567")
    models.add_message("dad", "lp", "where are you?")
    res = sms.handle_inbound("+15551234567", "5 min out")
    assert res["status"] == "stored"
    dad_inbox = models.list_inbox("dad")
    assert dad_inbox[0]["body"] == "5 min out"
    assert dad_inbox[0]["sender_key"] == "lp"
    assert json.loads(events.get_nowait())["data"] == {"recipient_key": "dad"}


def test_inbound_no_thread_lands_in_own_inbox(monkeypatch):
    monkeypatch.setitem(sms.DRIVER_PHONES, "mom", "+15559999999")
    sms.handle_inbound("+15559999999", "note to self")
    assert models.list_inbox("mom")[0]["body"] == "note to self"


def test_signature_accepts_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sms, "TW_TOKEN", "")
    assert sms.verify_signature("http://x/sms", {}, "") is True


def test_signature_enforced_when_configured(monkeypatch):
    from twilio.request_validator import RequestValidator
    monkeypatch.setattr(sms, "TW_TOKEN", "secret")
    url = "https://tracker.example.ts.net/sms"
    form = {"From": "+15551234567", "Body": "hi"}
    good = RequestValidator("secret").compute_signature(url, form)
    assert sms.verify_signature(url, form, good) is True
    assert sms.verify_signature(url, form, "bogus") is False
    assert sms.verify_signature(url, {**form, "Body": "tampered"}, good) is False
