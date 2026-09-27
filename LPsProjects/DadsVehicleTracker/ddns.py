"""
Keep the telemetry hostname pointing at this network.

A power cut reboots the router, the ISP hands out a different WAN address,
and the cars silently lose the receiver — the DNS record still names
yesterday's address. Nothing in the house notices.

We already hold a Cloudflare token scoped to this one zone (it is what
issues the TLS certificate), so the record can simply be corrected. Reuses
`telemetry/cloudflare.ini` rather than asking for the credential twice.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from pathlib import Path

log = logging.getLogger("ddns")

API = "https://api.cloudflare.com/client/v4"
CREDENTIALS = Path(os.getenv("CLOUDFLARE_INI", "telemetry/cloudflare.ini"))
ENABLED = os.getenv("DDNS_ENABLED", "1") == "1"
TIMEOUT_S = 10.0


def token() -> str:
    """The zone-scoped API token, or "" if it isn't available."""
    env = os.getenv("CLOUDFLARE_API_TOKEN", "")
    if env:
        return env
    try:
        text = CREDENTIALS.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"dns_cloudflare_api_token\s*=\s*(\S+)", text)
    return match.group(1) if match else ""


def _call(method: str, path: str, body: dict | None = None) -> dict | None:
    tok = token()
    if not tok:
        return None
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — a DNS fix must never take the app down
        log.warning("Cloudflare %s %s failed: %s", method, path, exc)
        return None


def find_record(hostname: str) -> tuple[str, str, str] | None:
    """(zone_id, record_id, current_ip) for an A record, or None."""
    zone_name = ".".join(hostname.split(".")[-2:])
    zones = _call("GET", f"/zones?name={zone_name}")
    if not zones or not zones.get("result"):
        return None
    zone_id = zones["result"][0]["id"]
    recs = _call("GET", f"/zones/{zone_id}/dns_records?type=A&name={hostname}")
    if not recs or not recs.get("result"):
        return None
    rec = recs["result"][0]
    return zone_id, rec["id"], rec["content"]


def update(hostname: str, ip: str) -> bool:
    found = find_record(hostname)
    if not found:
        log.warning("No A record for %s to update", hostname)
        return False
    zone_id, record_id, current = found
    if current == ip:
        return True
    result = _call("PATCH", f"/zones/{zone_id}/dns_records/{record_id}",
                   {"content": ip})
    if result and result.get("success"):
        log.warning("Updated %s: %s -> %s (WAN address changed)",
                    hostname, current, ip)
        return True
    return False


def reconcile(hostname: str, public_ip: str, dns_ip: str) -> str:
    """Bring DNS back in line. Returns what happened, for the health check.

    "ok"        already correct
    "updated"   record corrected
    "disabled"  automatic updates switched off
    "failed"    tried and could not
    """
    if public_ip == dns_ip:
        return "ok"
    if not ENABLED:
        return "disabled"
    if not token():
        log.warning("WAN address changed but no Cloudflare token is available")
        return "failed"
    return "updated" if update(hostname, public_ip) else "failed"
