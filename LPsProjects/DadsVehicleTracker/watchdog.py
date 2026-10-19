"""
Watch the watcher.

The health monitor lives inside the app, so it can report a broken relay or
a dead broker — but not the one failure that matters most: the app itself
not running. On 2026-10-14 the app and its supervisor died together and the
tracker sat dead for five days. Nothing noticed, because the only thing
watching was the thing that had died.

This runs as its own scheduled task, on a timer, outside the app. It does
three things and nothing else, so there is very little here that can fail:

  1. ask the app whether it is alive
  2. if not, start the scheduled task that runs it
  3. push a notification, since a human needs to know either way

Run it with pythonw so no console window appears:
    .venv\\Scripts\\pythonw.exe watchdog.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "data" / "watchdog.log"
TASK_NAME = "Dads Vehicle Tracker"
PORT = 5000
# How long to give the supervisor to start Python and bind the port, as a
# number of checks rather than a wall-clock deadline: pacing with sleep while
# timing with the clock busy-spins if sleep ever returns early.
START_CHECKS = 9
CHECK_INTERVAL_S = 5
START_GRACE_S = START_CHECKS * CHECK_INTERVAL_S
NO_WINDOW = 0x08000000


def log(message: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def serving(port: int = PORT, timeout: float = 5.0) -> bool:
    """Is the app actually answering, not merely holding the port?

    A process can own a socket while being too broken to serve, so ask it
    for a page rather than trusting the listen state.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/login", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — any failure means "not serving"
        return False


def start_task(name: str = TASK_NAME) -> bool:
    try:
        result = subprocess.run(["schtasks", "/Run", "/TN", name],
                                capture_output=True, text=True, timeout=60,
                                creationflags=NO_WINDOW)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log(f"could not start the task: {exc}")
        return False


def notify(title: str, message: str, priority: str = "high") -> None:
    """Push via the app's own notify module, which reads .env for the topic.

    Imported lazily and defensively: the watchdog must still restart the app
    even if notification is broken, because that is its actual job.
    """
    try:
        sys.path.insert(0, str(HERE))
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
        import notify as notifier
        notifier.send(title=title, message=message, priority=priority,
                      tags="rotating_light")
    except Exception as exc:  # noqa: BLE001
        log(f"could not push: {exc}")


def main() -> int:
    if serving():
        log("ok")
        return 0

    log("NOT SERVING — starting the task")
    started = start_task()
    for _ in range(START_CHECKS):
        time.sleep(CHECK_INTERVAL_S)
        if serving():
            log("recovered")
            notify("Tracker was down — restarted",
                   "The app had stopped and the watchdog restarted it. "
                   "Worth a look at data/app.log for why.",
                   priority="default")
            return 0

    log(f"STILL DOWN after {START_GRACE_S}s (schtasks ok={started})")
    notify("Tracker is down and will not restart",
           "The watchdog tried to restart it and it did not come back. "
           "Docker Desktop may not be running, which the app needs for "
           "telemetry.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
