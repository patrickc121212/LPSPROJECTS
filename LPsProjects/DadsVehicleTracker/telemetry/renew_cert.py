"""
Weekly Let's Encrypt renewal for the telemetry receiver.

Run by the Scheduled Task "Dads Vehicle Tracker - cert renewal" (windowless,
via pythonw). `certbot renew` is a no-op until the cert is inside 30 days of
expiry, so running this weekly is cheap and safe.

On a successful renewal certbot runs the deploy hook (fixes file ownership
for the non-root receiver) and we restart fleet-telemetry so it picks up the
new key. Appends to telemetry/renew.log.

NOTE: needs Docker Desktop running, so this task is an at-logon-session task.
If the PC sits at the sign-in screen for weeks, the cert will not renew.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "renew.log"
ARCHIVE = HERE / "certs" / "archive" / "telemetry.dowdsgarage.com"
NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True,
                          creationflags=NO_WINDOW, timeout=600)


def days_left() -> int | None:
    """Days until the newest issued cert expires.

    Reads from archive/ and not live/: certbot's live/ entries are POSIX
    symlinks that Windows Python cannot follow, which silently returned None
    here and meant a real renewal was never detected.
    """
    try:
        from cryptography import x509
        certs = sorted(ARCHIVE.glob("cert*.pem"),
                       key=lambda q: int("".join(c for c in q.stem if c.isdigit()) or 0))
        if not certs:
            return None
        cert = x509.load_pem_x509_certificate(certs[-1].read_bytes())
        return (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
    except Exception as exc:  # noqa: BLE001 — informational only
        log(f"days_left failed: {exc}")
        return None


def main() -> None:
    before = days_left()
    log(f"renew check: {before} days left")
    r = run(["docker", "compose", "--profile", "cert", "run", "--rm", "certbot-renew"])
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        log(f"certbot FAILED (rc={r.returncode}): {out[-800:]}")
        return
    after = days_left()
    # Trust certbot's own words as well as the date check — if either says a
    # new cert landed, restart the receiver. Restarting needlessly is ~2 s of
    # downtime; failing to restart leaves the cars talking to an expired cert.
    renewed = "no action taken" not in out.lower() and (
        "congratulations" in out.lower()
        or "successfully renewed" in out.lower()
        or (after is not None and before is not None and after > before)
    )
    if renewed:
        log(f"renewed: now {after} days left; restarting fleet-telemetry")
        rr = run(["docker", "compose", "restart", "fleet-telemetry"])
        log(f"restart rc={rr.returncode}")
    else:
        log(f"no renewal needed ({after} days left)")


if __name__ == "__main__":
    main()
