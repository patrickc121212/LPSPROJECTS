"""The external watchdog: the only thing that notices the app is gone.

Every test here exists because the app sat dead for five days on 2026-10-14
while the in-app health monitor, being inside the dead app, reported nothing.
"""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import watchdog


# --- Liveness probe --------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 — http.server's interface
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


@pytest.fixture
def served():
    """A real HTTP server on a free port, so the probe is tested end to end."""
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def test_serving_true_when_app_answers(served):
    assert watchdog.serving(port=served) is True


def test_serving_false_when_nothing_listens():
    with socket.socket() as s:          # grab a port, then release it
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert watchdog.serving(port=port, timeout=1.0) is False


def test_serving_false_when_port_is_held_but_nothing_serves():
    """The failure mode a plain port check would call healthy.

    A listening socket that never answers is exactly what a wedged app looks
    like, so the probe asks for a page instead of trusting the listen state.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        assert watchdog.serving(port=sock.getsockname()[1], timeout=1.0) is False
    finally:
        sock.close()


def test_serving_false_on_error_status(served, monkeypatch):
    monkeypatch.setattr(_Handler, "status", 500)
    assert watchdog.serving(port=served) is False


# --- Decisions -------------------------------------------------------------

def test_healthy_app_is_left_alone(monkeypatch):
    """No restart, no push, no noise: silence is the normal outcome."""
    calls = []
    monkeypatch.setattr(watchdog, "serving", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "start_task", lambda *a, **k: calls.append("start"))
    monkeypatch.setattr(watchdog, "notify", lambda *a, **k: calls.append("notify"))
    monkeypatch.setattr(watchdog, "log", lambda *a, **k: None)
    assert watchdog.main() == 0
    assert calls == []


def test_dead_app_is_restarted_and_reported(monkeypatch):
    states = iter([False, True])
    pushed = []
    monkeypatch.setattr(watchdog, "serving",
                        lambda *a, **k: next(states, True))
    monkeypatch.setattr(watchdog, "start_task", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "notify",
                        lambda title, message, priority="high": pushed.append(title))
    monkeypatch.setattr(watchdog, "log", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert watchdog.main() == 0
    assert len(pushed) == 1 and "restarted" in pushed[0]


def test_unrecoverable_app_pushes_a_loud_alert(monkeypatch):
    """The five-day case: it will not come back, so a human must be told."""
    pushed = []
    monkeypatch.setattr(watchdog, "serving", lambda *a, **k: False)
    monkeypatch.setattr(watchdog, "start_task", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "notify",
                        lambda title, message, priority="high":
                            pushed.append((title, priority)))
    monkeypatch.setattr(watchdog, "log", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert watchdog.main() == 1
    assert len(pushed) == 1
    title, priority = pushed[0]
    assert "down" in title and priority == "high"


def test_restart_is_attempted_even_if_notification_is_broken(monkeypatch):
    """Pushing is how we tell you; restarting is the actual job."""
    started = []
    monkeypatch.setattr(watchdog, "serving", lambda *a, **k: False)
    monkeypatch.setattr(watchdog, "start_task",
                        lambda *a, **k: started.append(True) or True)
    monkeypatch.setattr(watchdog, "log", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    def boom(*_a, **_k):
        raise RuntimeError("ntfy unreachable")

    monkeypatch.setattr(watchdog, "notify", boom)
    with pytest.raises(RuntimeError):
        watchdog.main()
    assert started == [True], "the app must be restarted before we try to push"


def test_notify_swallows_push_failures(monkeypatch, tmp_path):
    """notify() itself must never propagate: an outage breaks pushing."""
    monkeypatch.setattr(watchdog, "LOG", tmp_path / "watchdog.log")
    import notify as notifier

    def boom(**_k):
        raise OSError("network is unreachable")

    monkeypatch.setattr(notifier, "send", boom)
    watchdog.notify("t", "m")           # must not raise
    assert "could not push" in (tmp_path / "watchdog.log").read_text()


def test_start_task_failure_is_survived(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "LOG", tmp_path / "watchdog.log")

    def boom(*_a, **_k):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(watchdog.subprocess, "run", boom)
    assert watchdog.start_task("whatever") is False


# --- Logging ---------------------------------------------------------------

def test_log_creates_the_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "LOG", tmp_path / "data" / "watchdog.log")
    watchdog.log("hello")
    assert "hello" in (tmp_path / "data" / "watchdog.log").read_text()
