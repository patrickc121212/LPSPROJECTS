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


def _adopt(child: subprocess.Popen) -> None:
    """Tie the child's lifetime to ours via a Windows job object.

    Killing the supervisor skips atexit, which previously left app.py running
    and listening — so a restart produced two instances both driving the
    garage door. A job object with KILL_ON_JOB_CLOSE makes the child die with
    us however we are stopped.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return

        class _LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _LIMIT),
                        ("IoInfo", ctypes.c_byte * 48),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        handle = k32.OpenProcess(0x001F0FFF, False, child.pid)
        if handle:
            k32.AssignProcessToJobObject(job, handle)
            k32.CloseHandle(handle)
        globals()["_job"] = job  # keep the handle alive for our lifetime
    except Exception:  # noqa: BLE001 — supervision must not depend on this
        pass


def _kill_child() -> None:
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()


def port_is_taken(port: int = 5000) -> bool:
    """Is something already serving? Windows lets a second process bind the
    same port, so two app instances can run side by side — and both would
    drive the garage door, producing exactly the double-pulse that leaves a
    door stuck half open. Refuse to add another rather than risk that."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    global _child
    os.chdir(ROOT)
    LOG.parent.mkdir(exist_ok=True)
    atexit.register(_kill_child)
    if port_is_taken():
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"[{_stamp()}] supervisor: port 5000 already served; "
                      "another instance is running. Exiting rather than "
                      "starting a second one.\n")
        return
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
                _adopt(_child)
                rc = _child.wait()
            except Exception as exc:  # noqa: BLE001 — keep supervising no matter what
                rc = f"launch error: {exc}"
            log.write(f"[{_stamp()}] supervisor: app.py exited ({rc}); restarting in {RESTART_DELAY_S}s\n")
        time.sleep(RESTART_DELAY_S)


if __name__ == "__main__":
    main()
