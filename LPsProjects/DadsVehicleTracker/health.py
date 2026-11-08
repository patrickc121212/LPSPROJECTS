"""
Watch the things that fail silently.

Every outage so far looked healthy from the outside: the site served pages
perfectly while the telemetry receiver was down for ten hours, while the
Shelly sat at a new DHCP address, and while the cars could not reach us
because the WAN address had changed. Nothing noticed; a person did.

Each check answers "would this stop the app doing its job?" and each has to
be quiet when nothing is wrong — an alert that cries wolf gets ignored, and
then we are back where we started. In particular, a parked Tesla reports
nothing for hours quite legitimately, so silence alone is not a fault.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field

import config
import models
import notify

log = logging.getLogger("health")

CHECK_INTERVAL_S = int(os.getenv("HEALTH_INTERVAL_S", "300"))
# A parked car is silent for hours, so this is deliberately long: it catches
# "the whole pipeline is dead", not "nobody has driven today".
NO_DATA_HOURS = float(os.getenv("HEALTH_NO_DATA_HOURS", "18"))
CERT_WARN_DAYS = int(os.getenv("HEALTH_CERT_WARN_DAYS", "14"))
# Comparing our WAN address against DNS needs an outbound call; do it rarely.
WAN_CHECK_INTERVAL_S = int(os.getenv("HEALTH_WAN_INTERVAL_S", "900"))
# The public hostname the family actually uses, and how often to verify it is
# still published. Shelling out to tailscale is cheap but not free.
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "tracker.taild11993.ts.net")
PUBLIC_CHECK_INTERVAL_S = int(os.getenv("HEALTH_PUBLIC_INTERVAL_S", "900"))
# How many consecutive NXDOMAIN readings count as real, when Tailscale itself
# reports the Funnel is up. At the interval above, 2 means ~15 minutes.
DNS_FAILS_BEFORE_ALERT = int(os.getenv("HEALTH_DNS_FAILS", "2"))
TAILSCALE_EXE = os.getenv(
    "TAILSCALE_EXE", r"C:\Program Files\Tailscale\tailscale.exe")

SEV_CRITICAL = "critical"
SEV_WARNING = "warning"


@dataclass
class Issue:
    key: str
    severity: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "severity": self.severity,
                "message": self.message, "detail": self.detail}


@dataclass
class _WanCache:
    at: float = 0.0
    value: tuple[str, str] | None = None  # (public_ip, dns_ip)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _PublicCache:
    at: float = 0.0
    value: "tuple[bool, str] | None" = None   # (reachable, detail)
    # Consecutive NXDOMAIN readings while Tailscale says all is well. DNS
    # negative caching outlives a Funnel being restored, so one reading
    # proves nothing.
    dns_fails: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_wan = _WanCache()
_public = _PublicCache()
_previous: set[str] = set()
# True when a recovery message is owed but could not be delivered.
_recovery_owed = False
# Set when a Fleet API refresh is rejected; cleared by a successful one.
_tesla_refresh_failed = False


# --- individual checks -------------------------------------------------------

def broker_reachable(host: str | None = None, port: int | None = None) -> bool:
    """TCP probe of the MQTT broker.

    Deliberately not the worker's in-process connection flag: that is only
    meaningful inside the running app, so any other process — a console
    check, a script — would read it as disconnected and cry wolf. A probe
    tells the truth from anywhere, and the broker being gone is the actual
    failure we are hunting.
    """
    import telemetry_worker
    host = host or telemetry_worker.MQTT_HOST
    port = port or telemetry_worker.MQTT_PORT
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def check_mqtt() -> Issue | None:
    """Is the telemetry pipeline alive?

    This is the check that would have caught the ten-hour outage: the app
    served pages perfectly while its MQTT link had been refused every twelve
    seconds since the reboot.
    """
    if config.VEHICLE_SOURCE != "telemetry":
        return None
    import telemetry_worker
    if broker_reachable():
        if not telemetry_worker.worker_running():
            # Some other process (a console check, a test) — it has no MQTT
            # connection of its own and is not supposed to.
            return None
        if telemetry_worker.mqtt_connected():
            return None
        # Broker up but we are not subscribed: a real fault, though a milder
        # one — paho reconnects on its own, so only flag it, don't panic.
        return Issue("mqtt", SEV_WARNING,
                     "Not subscribed to vehicle telemetry",
                     f"The broker at {telemetry_worker.MQTT_HOST}:"
                     f"{telemetry_worker.MQTT_PORT} is reachable but this app "
                     "is not connected to it.")
    return Issue("mqtt", SEV_CRITICAL,
                 "Not receiving vehicle telemetry",
                 f"The MQTT broker at {telemetry_worker.MQTT_HOST}:"
                 f"{telemetry_worker.MQTT_PORT} is unreachable, so positions, "
                 "charging and auto-open are all frozen. Usually Docker is "
                 "not running.")


def check_doors() -> list[Issue]:
    """Can we still reach each relay? A moved DHCP lease breaks door control
    without anything else looking wrong."""
    import shelly
    out: list[Issue] = []
    for door in config.GARAGE_DOORS:
        if not door.shelly_host:
            continue
        host = shelly.locate(door.shelly_host, door.shelly_mac)
        ok, detail = shelly.check_pulse_config(host, door.shelly_channel)
        if not ok and "unreachable" in detail:
            out.append(Issue(
                f"door:{door.key}", SEV_CRITICAL,
                f"{door.label} relay is unreachable",
                f"No reply from {door.shelly_host}. Its address may have "
                f"changed; {detail}"))
        elif not ok:
            out.append(Issue(f"door:{door.key}", SEV_WARNING,
                             f"{door.label} relay is misconfigured", detail))
    return out


def check_vehicle_data(now: float) -> Issue | None:
    """Has *every* car been silent for implausibly long?

    One quiet car is a parked car. All of them quiet for most of a day means
    the pipeline is broken somewhere we are not otherwise seeing.
    """
    rows = models.all_vehicle_states()
    stamps = [r.get("updated_at") or 0 for r in rows]
    if not stamps:
        return None
    newest = max(stamps)
    hours = (now - newest) / 3600
    if hours < NO_DATA_HOURS:
        return None
    return Issue("no_data", SEV_WARNING,
                 f"No vehicle data for {hours:.0f} hours",
                 "Every car has been silent. Normal overnight, but not for "
                 "this long — check the receiver and the router forward.")


def wan_matches_dns(now: float, force: bool = False) -> tuple[str, str] | None:
    """(public_ip, dns_ip) or None if it could not be determined."""
    with _wan.lock:
        if not force and _wan.value and now - _wan.at < WAN_CHECK_INTERVAL_S:
            return _wan.value
    public = dns = ""
    try:
        public = urllib.request.urlopen(
            "https://api.ipify.org", timeout=8).read().decode().strip()
    except Exception:  # noqa: BLE001
        pass
    host = os.getenv("TELEMETRY_HOST", "")
    if host:
        try:
            dns = socket.gethostbyname(host)
        except Exception:  # noqa: BLE001
            pass
    value = (public, dns) if (public and dns) else None
    with _wan.lock:
        _wan.at, _wan.value = now, value
    return value


def check_wan(now: float) -> Issue | None:
    """Our WAN address changing silently stops the cars reaching the
    telemetry receiver — exactly what happened on 2026-09-27.

    We hold a zone-scoped Cloudflare token already, so rather than only
    complaining, put the record right and say what was done. The router's
    port forward still needs a person if the LAN address moved too, which is
    why this stays a visible warning even after a successful fix.
    """
    import ddns
    pair = wan_matches_dns(now)
    if not pair:
        return None
    public, dns = pair
    if public == dns:
        return None
    host = os.getenv("TELEMETRY_HOST", "")
    outcome = ddns.reconcile(host, public, dns)
    if outcome == "updated":
        # Force the next check to look again rather than trust the cache.
        with _wan.lock:
            _wan.at = 0.0
        return Issue("wan", SEV_WARNING,
                     "WAN address changed — DNS corrected automatically",
                     f"{host} now points at {public} (was {dns}). If this "
                     "machine's LAN address also changed, the router's 443 "
                     "forward needs updating by hand.")
    return Issue("wan", SEV_CRITICAL,
                 "Cars cannot reach the telemetry receiver",
                 f"This network's address is {public} but {host} still points "
                 f"at {dns}, and it could not be corrected ({outcome}). "
                 "Update the DNS record, and check the router's 443 forward.")


def public_url_published(now: float, force: bool = False) -> tuple[bool, str]:
    """Is the public URL actually published? Returns (ok, detail).

    This is the one failure the rest of the monitoring is blind to. Every
    other check runs inside the app and asks about things the app can see;
    the watchdog asks 127.0.0.1. All of them were reporting healthy on
    2026-11-08 while tracker.taild11993.ts.net did not resolve at all and
    the family could not reach the site from a phone or a car.

    Two independent questions, because either alone gives false comfort:

      1. Does the hostname resolve, according to a resolver outside this
         house? NXDOMAIN is how the outage presented, and a public resolver
         saying so is genuinely external evidence.
      2. Does Tailscale still hold a Funnel for us? The node had been
         logged out, which silently discards the serve config, and DNS can
         lag behind that by minutes.

    What it deliberately does NOT do is fetch the public URL from this
    machine and call a 200 success. On the node itself that request can be
    answered locally without ever crossing the internet -- measured at 50 ms
    on 2026-11-01 -- so a self-fetch would have passed happily throughout
    the outage. A check that cannot fail is worse than no check.
    """
    with _public.lock:
        if not force and _public.value and now - _public.at < PUBLIC_CHECK_INTERVAL_S:
            return _public.value

    reasons: list[str] = []

    # 1. Tailscale's own view: logged in, online, and serving a Funnel. Local
    #    and authoritative, with no propagation delay, so it alerts at once.
    state, funnel = _tailscale_state()
    if state is not None and state != "Running":
        reasons.append(f"Tailscale backend state is {state!r}, not Running — "
                       "the node is logged out, so its DNS record and Funnel "
                       "are withdrawn")
    if funnel is False:
        reasons.append("Tailscale has no Funnel configured, so nothing is "
                       "published even while the node is up")

    # 2. Resolution, from outside this network. Corroboration only, and it
    #    must persist before it counts.
    #
    #    Measured on 2026-11-08: after the Funnel was restored and the site
    #    was verifiably loading from outside, both public resolvers were
    #    still returning NXDOMAIN from their negative cache. Alerting on a
    #    single such reading would mean a CRITICAL push every time the
    #    Funnel is re-established -- and a monitor that cries wolf during
    #    normal propagation is one you learn to ignore.
    resolved = _resolves_publicly(PUBLIC_HOST)   # None => could not ask
    if resolved is False:
        _public.dns_fails += 1
        if _public.dns_fails >= DNS_FAILS_BEFORE_ALERT and not reasons:
            reasons.append(
                f"{PUBLIC_HOST} has not resolved (NXDOMAIN) from a public DNS "
                f"resolver on {_public.dns_fails} consecutive checks, while "
                "Tailscale believes it is published")
    elif resolved is True:
        _public.dns_fails = 0

    value = (not reasons, "; ".join(reasons))
    with _public.lock:
        _public.at, _public.value = now, value
    return value


def _resolves_publicly(host: str) -> bool | None:
    """True / False / None if we could not get an answer at all.

    Uses DNS-over-HTTPS so the answer comes from outside this network
    rather than from a local cache or the router, which is the whole point.
    """
    for url in ("https://cloudflare-dns.com/dns-query?name=%s&type=A",
                "https://dns.google/resolve?name=%s&type=A"):
        try:
            req = urllib.request.Request(
                url % host, headers={"Accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            status = data.get("Status")
            if status == 3:            # NXDOMAIN
                return False
            if status == 0:
                # NOERROR with no A record is still nothing to connect to.
                return any(a.get("type") == 1 for a in data.get("Answer") or [])
        except Exception:  # noqa: BLE001 — try the next resolver
            continue
    return None


def _tailscale_state() -> tuple[str | None, bool | None]:
    """(BackendState, funnel_configured). None for anything we cannot read,
    so a missing CLI never manufactures an alert."""
    state: str | None = None
    funnel: bool | None = None
    if not os.path.exists(TAILSCALE_EXE):
        return state, funnel
    no_window = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run([TAILSCALE_EXE, "status", "--json"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=no_window)
        if r.returncode == 0 and r.stdout.strip():
            state = (json.loads(r.stdout) or {}).get("BackendState")
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run([TAILSCALE_EXE, "serve", "status"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=no_window)
        if r.returncode == 0:
            funnel = "funnel on" in r.stdout.lower()
    except Exception:  # noqa: BLE001
        pass
    return state, funnel


def check_public_url(now: float) -> Issue | None:
    """The family reaches this from phones and from the cars' browsers, so
    an unreachable public URL is the app being down as far as they are
    concerned, however healthy it looks from inside the house."""
    ok, detail = public_url_published(now)
    if ok:
        return None
    return Issue("public_url", SEV_CRITICAL,
                 "The site is not reachable from outside",
                 f"{detail}. It still works on the local network. Most likely "
                 "the tailscale-ipn.exe tray client is not running: it holds "
                 "the signed-in profile, it is a user-session process like "
                 "Docker Desktop, and it has no autostart entry, so anything "
                 "that ends the session takes it down. Start it, then if the "
                 "node still will not come up run `tailscale up "
                 "--hostname=tracker` (the hostname MUST be given or the "
                 "public URL changes), and re-publish with `tailscale funnel "
                 "--bg 5000`.")


def check_tesla_credentials(now: float) -> Issue | None:
    """Can we still talk to Tesla's API?

    Telemetry does not need this — the cars push to us — so a dead token is
    invisible until someone tries to change what the cars report. Refresh
    tokens are single use and rotate, so two processes refreshing at once
    (as the duplicate instances on 2026-09-27 did) can leave the stored one
    stale.
    """
    import json
    path = os.getenv("TESLA_TOKENS_PATH", "data/tesla_tokens.json")
    try:
        with open(path, encoding="utf-8") as fh:
            tokens = json.load(fh)
    except (OSError, ValueError):
        return None   # not set up at all; not this check's business
    if not tokens.get("refresh_token"):
        return Issue("tesla_auth", SEV_WARNING, "No Tesla refresh token stored",
                     "Run: python tesla_setup.py login")
    if _tesla_refresh_failed:
        return Issue("tesla_auth", SEV_WARNING,
                     "Tesla API sign-in has expired",
                     "Live telemetry still works, but the cars' settings "
                     "cannot be changed until you run "
                     "`python tesla_setup.py login` again.")
    return None


def note_tesla_refresh_failure(failed: bool = True) -> None:
    """Called by whatever tries to use the API, so the check reports fact
    rather than re-testing the credential on every sweep."""
    global _tesla_refresh_failed
    _tesla_refresh_failed = failed


def check_certificate(now: float) -> Issue | None:
    path = os.path.join("telemetry", "certs", "archive",
                        os.getenv("TELEMETRY_HOST", ""))
    try:
        from cryptography import x509
        import glob
        certs = sorted(glob.glob(os.path.join(path, "cert*.pem")))
        if not certs:
            return None
        cert = x509.load_pem_x509_certificate(open(certs[-1], "rb").read())
        days = (cert.not_valid_after_utc.timestamp() - now) / 86400
    except Exception:  # noqa: BLE001 — informational only
        return None
    if days > CERT_WARN_DAYS:
        return None
    sev = SEV_CRITICAL if days <= 0 else SEV_WARNING
    return Issue("cert", sev,
                 f"Telemetry certificate expires in {days:.0f} days",
                 "The cars refuse an expired certificate. Check the weekly "
                 "renewal task.")


# --- aggregation -------------------------------------------------------------

def check(now: float | None = None) -> list[Issue]:
    """Everything, worst first. Never raises: a broken check must not take
    the app with it."""
    now = time.time() if now is None else now
    issues: list[Issue] = []
    for fn in (lambda: [check_mqtt()], check_doors,
               lambda: [check_vehicle_data(now)], lambda: [check_wan(now)],
               lambda: [check_certificate(now)],
               lambda: [check_public_url(now)],
               lambda: [check_tesla_credentials(now)]):
        try:
            issues.extend(i for i in fn() if i)
        except Exception as exc:  # noqa: BLE001
            log.exception("health check failed: %s", exc)
    issues.sort(key=lambda i: 0 if i.severity == SEV_CRITICAL else 1)
    return issues


def summary(issues: list[Issue]) -> dict:
    return {
        "healthy": not issues,
        "critical": sum(1 for i in issues if i.severity == SEV_CRITICAL),
        "warning": sum(1 for i in issues if i.severity == SEV_WARNING),
        "issues": [i.as_dict() for i in issues],
    }


def notify_changes(issues: list[Issue]) -> list[str]:
    """Push only when something changes, and say so when it recovers.

    Repeating an alert every five minutes trains people to ignore it, which
    is the same failure as having no alert at all.
    """
    global _previous, _recovery_owed
    current = {i.key for i in issues}
    by_key = {i.key: i for i in issues}
    new = current - _previous
    gone = _previous - current
    sent: list[str] = []
    undelivered: set[str] = set()

    for key in sorted(new):
        issue = by_key[key]
        log.warning("HEALTH %s: %s — %s", issue.severity.upper(),
                    issue.message, issue.detail)
        if notify.send(
            title=issue.message,
            message=issue.detail or issue.message,
            priority="high" if issue.severity == SEV_CRITICAL else "default",
            tags="rotating_light" if issue.severity == SEV_CRITICAL else "warning",
        ):
            sent.append(key)
        else:
            # The push did not get through — during the 2026-09-29 internet
            # outage every one of these failed and was still marked as
            # reported, so the fault, and its recovery, were never told to
            # anyone. Leave it unreported so the next sweep tries again.
            undelivered.add(key)

    for key in sorted(gone):
        log.info("HEALTH cleared: %s", key)
    if gone and not current:
        _recovery_owed = True
    if _recovery_owed and not current:
        if notify.send(title="Tracker is healthy again",
                       message="All checks passing.", tags="white_check_mark"):
            sent.append("recovered")
            _recovery_owed = False

    # Anything we could not deliver stays "new" for next time.
    _previous = current - undelivered
    return sent


def _loop() -> None:
    log.info("Health monitor started (every %ss; push %s).", CHECK_INTERVAL_S,
             "on" if notify.configured() else "in dry-run")
    while True:
        try:
            notify_changes(check())
        except Exception as exc:  # noqa: BLE001
            log.exception("health loop failed: %s", exc)
        time.sleep(CHECK_INTERVAL_S)


def start_background() -> None:
    threading.Thread(target=_loop, name="health-monitor", daemon=True).start()
