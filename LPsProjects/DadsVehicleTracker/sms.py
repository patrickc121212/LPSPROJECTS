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


def send_sms(to: str, body: str) -> None:
    if not (TW_SID and TW_TOKEN and TW_FROM):
        log.info("[dry-run sms] to=%s body=%r", to, body)
        return
    try:
        from twilio.rest import Client  # type: ignore
        Client(TW_SID, TW_TOKEN).messages.create(to=to, from_=TW_FROM, body=body)
        log.info("SMS sent to %s", to)
    except Exception as exc:  # noqa: BLE001
        log.warning("SMS send failed: %s", exc)


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

def _maybe_send_fallbacks() -> None:
    now = time.time()
    for v in config.VEHICLES:
        phone = DRIVER_PHONES.get(v.key, "")
        if not phone:
            continue
        last_seen = models.last_seen(v.key) or 0.0
        # Unread messages older than the threshold AND not seen recently → SMS.
        for m in models.list_inbox(v.key, limit=20):
            if m["read_at"] is None and (now - m["created_at"]) > config.SMS_FALLBACK_AFTER_S \
                    and (now - last_seen) > config.SMS_FALLBACK_AFTER_S:
                sender = next((x.label for x in config.VEHICLES if x.key == m["sender_key"]), "Family")
                send_sms(phone, f"[{sender}] {m['body']}")
                # Mark as read so we don't re-send every tick.
                models.mark_read(v.key)
                break


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
