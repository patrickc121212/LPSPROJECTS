"""
Tiny in-process pub/sub for SSE. Each subscriber gets its own queue; the
publisher fans an event out to every queue. If a subscriber falls behind
(a slow browser), the queue hits MAXSIZE and we drop the event for that
subscriber only — keeps the live stream live without unbounded memory.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

MAXSIZE = 256


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next_id = 1

    def subscribe(self) -> tuple[int, queue.Queue]:
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            q: queue.Queue = queue.Queue(maxsize=MAXSIZE)
            self._subs[sid] = q
            return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data, "ts": time.time()})
        with self._lock:
            for q in self._subs.values():
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    # Drop this event for the slow subscriber only. Every
                    # publisher sends full snapshots, so the next one that
                    # fits brings them back up to date.
                    pass

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# Module-level singleton — importable from app, geofence, poller, sms.
bus = EventBus()
