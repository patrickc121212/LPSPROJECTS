"""
Slow down and record password guessing at /login.

The public URL is swept by automated scanners (660 probes for /.env, /.git
and friends in a fortnight), and a single shared password is all that stands
between the internet and a garage door. Nothing here stops a determined
attacker; it makes grinding slow and, more importantly, makes it *visible* —
before this, a brute-force attempt left no trace at all.

Two tiers, because identifying the client is unreliable:

  per-key   5 failures in 5 minutes locks that key out for 15 minutes.
            The key is the best identity available (see client_key), which
            behind a proxy may be spoofable.
  global    once failures across all keys pass a threshold, every login
            attempt gets a delay. A delay rather than a lockout on purpose:
            a global lockout would let anyone shut the family out.

State is in memory, so a restart forgives everyone. That is an acceptable
trade for a household app; a restart is not something an attacker can force.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("login_guard")

# Per-key
MAX_FAILURES = 5
WINDOW_S = 300.0
LOCKOUT_S = 900.0
# Global
GLOBAL_THRESHOLD = 20
GLOBAL_WINDOW_S = 300.0
GLOBAL_DELAY_S = 2.0
# Applied to every failed attempt, so even a first guess costs real time.
FAILURE_DELAY_S = 0.5


class LoginGuard:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    # --- queries ---------------------------------------------------------

    def locked_out(self, key: str, now: float | None = None) -> float:
        """Seconds remaining on this key's lockout; 0.0 if it may try."""
        now = time.time() if now is None else now
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            return max(0.0, until - now)

    def under_attack(self, now: float | None = None) -> bool:
        """Are failures across all keys above the global threshold?"""
        now = time.time() if now is None else now
        with self._lock:
            self._trim(self._global, now, GLOBAL_WINDOW_S)
            return len(self._global) >= GLOBAL_THRESHOLD

    def delay_for_failure(self, now: float | None = None) -> float:
        return FAILURE_DELAY_S + (GLOBAL_DELAY_S if self.under_attack(now) else 0.0)

    # --- updates ---------------------------------------------------------

    def record_failure(self, key: str, detail: str = "", now: float | None = None) -> float:
        """Record a bad password. Returns the lockout remaining afterwards."""
        now = time.time() if now is None else now
        with self._lock:
            bucket = self._failures.setdefault(key, deque())
            bucket.append(now)
            self._trim(bucket, now, WINDOW_S)
            self._global.append(now)
            self._trim(self._global, now, GLOBAL_WINDOW_S)
            count = len(bucket)
            if count >= MAX_FAILURES:
                self._locked_until[key] = now + LOCKOUT_S
                bucket.clear()
                log.warning("Login LOCKED OUT %s for %.0f min after %d failures %s",
                            key, LOCKOUT_S / 60, count, detail)
                return LOCKOUT_S
            log.warning("Failed login %d/%d from %s %s", count, MAX_FAILURES, key, detail)
            return 0.0

    def record_success(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()
            self._global.clear()

    @staticmethod
    def _trim(bucket: deque[float], now: float, window: float) -> None:
        while bucket and now - bucket[0] > window:
            bucket.popleft()


guard = LoginGuard()


def client_key(headers, remote_addr: str | None) -> str:
    """Best available identity for the caller.

    Requests reaching us through Tailscale Serve/Funnel all arrive from
    127.0.0.1, so remote_addr alone would lump the whole internet together
    with the family. Prefer a forwarded address or the tailnet identity.

    X-Forwarded-For is client-controllable, so a determined attacker can
    rotate it to dodge the per-key limit — that is what the global tier is
    for.
    """
    xff = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff
    ts_user = headers.get("Tailscale-User-Login")
    if ts_user:
        return f"tailnet:{ts_user}"
    return remote_addr or "unknown"


def describe(headers) -> str:
    """Context worth having in the log when something is grinding away."""
    bits = []
    for h in ("User-Agent", "X-Forwarded-For", "Tailscale-User-Login"):
        v = headers.get(h)
        if v:
            bits.append(f"{h}={v[:80]}")
    return "(" + "; ".join(bits) + ")" if bits else ""
