"""
Windowless supervisor for the tracker app (Windows).

Run by the Scheduled Task "Dads Vehicle Tracker" via .venv\\Scripts\\pythonw.exe
so there is NO console window anyone can close by accident (that's how the
first cmd-based launcher died). Starts app.py with CREATE_NO_WINDOW, appends
its output to data\\app.log, and restarts it 10 s after any exit. Rotates the
log at ~10 MB. Stop it from Task Scheduler (End) — that kills this process,
and the child app exits with it because we tear it down on the way out.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "app.log"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
RESTART_DELAY_S = 10
LOG_ROTATE_BYTES = 10 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000

_child: subprocess.Popen | None = None


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rotate() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > LOG_ROTATE_BYTES:
            old = LOG.with_suffix(".log.1")
            old.unlink(missing_ok=True)
            LOG.rename(old)
    except OSError:
        pass


def _kill_child() -> None:
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()


def main() -> None:
    global _child
    os.chdir(ROOT)
    LOG.parent.mkdir(exist_ok=True)
    atexit.register(_kill_child)
    while True:
        _rotate()
        with open(LOG, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"[{_stamp()}] supervisor: starting app.py\n")
            log.flush()
            try:
                _child = subprocess.Popen(
                    [str(PYTHON), "app.py"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                    creationflags=CREATE_NO_WINDOW,
                )
                rc = _child.wait()
            except Exception as exc:  # noqa: BLE001 — keep supervising no matter what
                rc = f"launch error: {exc}"
            log.write(f"[{_stamp()}] supervisor: app.py exited ({rc}); restarting in {RESTART_DELAY_S}s\n")
        time.sleep(RESTART_DELAY_S)


if __name__ == "__main__":
    main()
