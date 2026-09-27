"""
Push notifications via ntfy.

Free, no phone numbers, no account: each person installs the ntfy app and
subscribes to a topic. Set NTFY_TOPIC in .env to switch it on; unset, every
call is a no-op that still logs, so nothing here can break the app.

The topic name is effectively the password on the public server — anyone who
knows it can read and post — so use a long random one, or self-host.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("notify")

SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TOPIC = os.getenv("NTFY_TOPIC", "")
TOKEN = os.getenv("NTFY_TOKEN", "")
TIMEOUT_S = 6.0


def configured() -> bool:
    return bool(TOPIC)


def send(title: str, message: str, priority: str = "default",
         tags: str = "", click: str = "") -> bool:
    """Push one notification. Returns True if it was sent (or dry-run)."""
    if not configured():
        log.info("[dry-run push] %s — %s", title, message)
        return True
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        req = urllib.request.Request(
            f"{SERVER}/{TOPIC}", data=message.encode("utf-8"),
            headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
        log.info("Pushed: %s", title)
        return True
    except Exception as exc:  # noqa: BLE001 — a failed push must not propagate
        log.warning("Push failed (%s): %s", title, exc)
        return False


def send_json(payload: dict) -> bool:
    """Escape hatch for the full ntfy message format."""
    if not configured():
        log.info("[dry-run push] %s", json.dumps(payload)[:200])
        return True
    try:
        body = json.dumps({**payload, "topic": TOPIC}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
        req = urllib.request.Request(SERVER, data=body, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=TIMEOUT_S).read()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Push failed: %s", exc)
        return False
