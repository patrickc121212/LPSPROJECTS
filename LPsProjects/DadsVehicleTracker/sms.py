"""
Twilio SMS send + inbound webhook. In-app inbox is the source of truth;
SMS is the fallback push when a driver hasn't opened the inbox in
SMS_FALLBACK_AFTER_S seconds.

In dev we log instead of sending — set TWILIO_ACCOUNT_SID + AUTH_TOKEN +
FROM_NUMBER to switch on real sends.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import config
import models
from eventbus import bus

log = logging.getLogger("sms")

TW_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TW_FROM  = os.getenv("TWILIO_FROM_NUMBER", "")

# driver_key -> phone number (configure via env or models.py in v2)
DRIVER_PHONES = {
    "dad": os.getenv("PHONE_DAD", ""),
    "lp":  os.getenv("PHONE_LP",  ""),
    "mom": os.getenv("PHONE_MOM", ""),
}


def configured() -> bool:
    return bool(TW_SID and TW_TOKEN and TW_FROM)


def send_sms(to: str, body: str) -> bool:
    """Returns True if the message was handed to Twilio (or dry-run logged)."""
    if not configured():
        log.info("[dry-run sms] to=%s body=%r", to, body)
        return True
    try:
        from twilio.rest import Client  # type: ignore
        Client(TW_SID, TW_TOKEN).messages.create(to=to, from_=TW_FROM, body=body)
        log.info("SMS sent to %s", to)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("SMS send failed: %s", exc)
        return False


def verify_signature(url: str, form: dict[str, str], signature: str) -> bool:
    """Validate X-Twilio-Signature on the inbound webhook. When Twilio isn't
    configured (dev) there is no auth token to check against, so we accept;
    once TWILIO_AUTH_TOKEN is set every request must be signed."""
    if not TW_TOKEN:
        return True
    try:
        from twilio.request_validator import RequestValidator  # type: ignore
    except ImportError:
        log.warning("twilio package missing; cannot verify webhook signature")
        return False
    return RequestValidator(TW_TOKEN).validate(url, form, signature)


# --- Inbound webhook handler ----------------------------------------------

def handle_inbound(from_number: str, body: str) -> dict[str, Any]:
    """Called by the /sms Twilio webhook. We try to match the From-number
    to a known driver and store the message in their inbox. Replies are
    routed back to the most recent sender who messaged this driver; if
    there's no prior thread we drop a note in the sender's own inbox."""
    sender_key = next((k for k, n in DRIVER_PHONES.items() if n == from_number), None)
    if sender_key is None:
        log.info("Inbound SMS from unknown number %s; ignoring.", from_number)
        return {"status": "unknown"}

    # Naive reply routing: most recent inbox row FROM anyone TO this driver.
    inbox = models.list_inbox(recipient_key=sender_key, limit=1)
    recipient_key = inbox[0]["sender_key"] if inbox else sender_key

    msg_id = models.add_message(sender_key=sender_key, recipient_key=recipient_key, body=body)
    bus.publish("inbox", {"recipient_key": recipient_key})
    return {"status": "stored", "id": msg_id}


# --- Fallback sweeper ------------------------------------------------------

def _maybe_send_fallbacks(now: float | None = None) -> int:
    """Push unread, not-yet-pushed messages over SMS for drivers who haven't
    opened the inbox recently. Each message is pushed at most once and stays
    UNREAD in the app — the SMS is a nudge, not a read receipt. Returns the
    number of SMS sent."""
    now = time.time() if now is None else now
    cutoff = now - config.SMS_FALLBACK_AFTER_S
    sent = 0
    for v in config.VEHICLES:
        phone = DRIVER_PHONES.get(v.key, "")
        if not phone:
            continue
        last_seen = models.last_seen(v.key) or 0.0
        if last_seen > cutoff:
            continue  # they've been in the app recently; in-app is enough
        for m in models.unread_unpushed(v.key, older_than=cutoff):
            sender = config.VEHICLES_BY_KEY.get(m["sender_key"])
            label = sender.driver if sender else "Family"
            if send_sms(phone, f"[{label}] {m['body']}"):
                models.mark_sms_sent(m["id"])
                sent += 1
    return sent


def _loop() -> None:
    log.info("SMS fallback sweeper started.")
    while True:
        try:
            _maybe_send_fallbacks()
        except Exception as exc:  # noqa: BLE001
            log.exception("SMS sweeper failed: %s", exc)
        time.sleep(60)


def start_background() -> None:
    t = threading.Thread(target=_loop, name="sms-sweeper", daemon=True)
    t.start()
