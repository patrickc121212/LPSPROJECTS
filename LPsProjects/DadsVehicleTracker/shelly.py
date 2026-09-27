"""
Local Shelly Gen2+/Gen4 RPC client.

We talk to the relay directly over the LAN instead of going
app -> IFTTT -> Google Assistant Routine -> Shelly Cloud -> device. One hop
instead of four: ~50 ms instead of 1-3 s, and it keeps working when the
internet is down. Google Home voice control still works independently via
the Shelly Cloud skill — we simply aren't routing our own triggers through it.

Wiring model: the relay is across the opener's wall-button terminals, so a
short PULSE is a button press. A press TOGGLES the door — there is no
separate open and close command. Without a reed switch on the Shelly's SW
input we cannot know the door's real position; see door_control for how that
assumption is handled.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("shelly")

DEFAULT_TIMEOUT_S = 4.0
# How long the relay stays closed = how long the opener's button is "pressed".
DEFAULT_PULSE_S = 0.5


# --- Finding the device again after it moves ---------------------------------
# A DHCP lease is not an identity. A power cut reboots the router, every
# device gets a new address, and door control breaks with nothing else
# looking wrong. The MAC is the identity, so we keep one and use it to find
# the relay again: try the configured address, then the mDNS name the device
# advertises, then sweep the subnet. Whatever answers with the right MAC is
# remembered for next time.

_located: dict[str, tuple[str, float]] = {}   # mac -> (host, found_at)
_locate_lock = threading.Lock()
LOCATE_CACHE_S = 300.0


def identify(host: str, timeout: float = 2.0) -> str | None:
    """The MAC of whatever answers at `host`, or None."""
    ok, payload = _rpc(host, "Shelly.GetDeviceInfo", None, timeout)
    if ok and isinstance(payload, dict):
        return str(payload.get("mac") or "").upper() or None
    return None


def mdns_name(mac: str, model_prefix: str = "shelly1g4") -> str:
    """The name the device advertises, which survives a new lease."""
    return f"{model_prefix}-{mac.lower()}.local"


def _subnet_hosts() -> list[str]:
    """Addresses on this machine's own /24, for a last-resort sweep."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            mine = sock.getsockname()[0]
    except OSError:
        return []
    base = mine.rsplit(".", 1)[0]
    return [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != mine]


def _sweep(mac: str, workers: int = 64) -> str | None:
    from concurrent.futures import ThreadPoolExecutor
    hosts = _subnet_hosts()
    if not hosts:
        return None
    log.warning("Sweeping the subnet for Shelly %s — this takes a few seconds", mac)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, found in zip(hosts, pool.map(lambda h: identify(h, 1.5), hosts)):
            if found == mac:
                return host
    return None


def locate(configured_host: str, mac: str = "", now: float | None = None) -> str:
    """Where the relay actually is.

    Falls back to `configured_host` when there is no MAC to search by, so
    behaviour is unchanged for anyone who has not set one.
    """
    if not mac:
        return configured_host
    now = time.time() if now is None else now
    with _locate_lock:
        hit = _located.get(mac)
    if hit and now - hit[1] < LOCATE_CACHE_S:
        return hit[0]

    for candidate, how in ((configured_host, "configured address"),
                           (mdns_name(mac), "mDNS name")):
        if not candidate:
            continue
        if identify(candidate) == mac:
            if candidate != configured_host:
                log.warning("Shelly %s answered on its %s (%s), not %s",
                            mac, how, candidate, configured_host)
            with _locate_lock:
                _located[mac] = (candidate, now)
            return candidate

    found = _sweep(mac)
    if found:
        log.warning("Shelly %s has moved to %s (was %s) — update "
                    "GARAGE*_SHELLY_HOST, or give it a DHCP reservation",
                    mac, found, configured_host)
        with _locate_lock:
            _located[mac] = (found, now)
        return found

    log.error("Shelly %s not found anywhere on this network", mac)
    return configured_host


def _rpc(host: str, method: str, params: dict[str, Any] | None = None,
         timeout: float = DEFAULT_TIMEOUT_S) -> tuple[bool, Any]:
    """Call a Gen2+ RPC method over HTTP GET. Returns (ok, payload-or-error)."""
    url = f"http://{host}/rpc/{method}"
    if params:
        # Shelly expects JSON literals in the query string (true, not True).
        qs = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, bool) else v for k, v in params.items()}
        )
        url = f"{url}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return True, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        # Include the reason line as well as the body: Shelly puts the
        # error in the body, but a bare 4xx with no body would otherwise
        # log as "HTTP 400:" and tell us nothing.
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            body = ""
        return False, f"HTTP {exc.code}: {exc.reason or ''} {body}".strip()
    except Exception as exc:  # noqa: BLE001 — network/JSON, caller decides
        return False, str(exc)


def pulse(host: str, channel: int = 0, pulse_s: float = DEFAULT_PULSE_S,
          timeout: float = DEFAULT_TIMEOUT_S) -> tuple[bool, str]:
    """Press the opener's button once. Returns (ok, detail).

    We pass `toggle_after` explicitly rather than relying on the device's
    own auto_off config: if that config were ever cleared (firmware reset,
    someone in the Shelly app), a plain `on=true` would LATCH the relay —
    the equivalent of holding the wall button down forever.
    """
    ok, payload = _rpc(host, "Switch.Set",
                       {"id": channel, "on": True, "toggle_after": pulse_s}, timeout)
    if not ok and "toggle_after" in str(payload):
        # Older firmware without the parameter: fall back to the device's
        # configured auto_off (verified via check_pulse_config below).
        log.warning("%s: toggle_after unsupported, falling back to auto_off", host)
        ok, payload = _rpc(host, "Switch.Set", {"id": channel, "on": True}, timeout)
    if ok:
        log.info("Shelly %s ch%d: pulsed %.1fs", host, channel, pulse_s)
        return True, f"pulsed {pulse_s}s"
    log.warning("Shelly %s ch%d: pulse FAILED: %s", host, channel, payload)
    return False, str(payload)


def get_status(host: str, channel: int = 0,
               timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any] | None:
    """Relay + input snapshot, or None if the device is unreachable.

    `input_state` is None until a reed/contact sensor is wired to the SW
    terminal and configured as a switch; once it is, it becomes the real
    door position and we can stop guessing.
    """
    ok, sw = _rpc(host, "Switch.GetStatus", {"id": channel}, timeout)
    if not ok:
        return None
    ok_in, inp = _rpc(host, "Input.GetStatus", {"id": channel}, timeout)
    return {
        "host": host,
        "output": sw.get("output"),
        "input_state": inp.get("state") if ok_in else None,
        "temperature_c": (sw.get("temperature") or {}).get("tC"),
        "switch_on_count": (sw.get("counts") or {}).get("switch_on"),
    }


def get_input_state(host: str, input_id: int = 0,
                    timeout: float = DEFAULT_TIMEOUT_S) -> bool | None:
    """Raw state of one input, or None if unreachable / not reporting.

    NOTE an unconnected `switch`-type input reads False, which is
    indistinguishable from a genuine open contact. Only call this for doors
    whose sensor is actually wired (GARAGE{N}_SENSOR_INPUT set).
    """
    ok, payload = _rpc(host, "Input.GetStatus", {"id": input_id}, timeout)
    if not ok or not isinstance(payload, dict):
        return None
    st = payload.get("state")
    return None if st is None else bool(st)


def check_pulse_config(host: str, channel: int = 0,
                       timeout: float = DEFAULT_TIMEOUT_S) -> tuple[bool, str]:
    """Startup sanity check: would a pulse behave like a button press?

    Warns if auto_off is off or its delay is long — that is the fallback
    path's only protection against latching the opener button on.
    """
    ok, cfg = _rpc(host, "Switch.GetConfig", {"id": channel}, timeout)
    if not ok:
        return False, f"unreachable: {cfg}"
    auto_off, delay = cfg.get("auto_off"), cfg.get("auto_off_delay")
    if not auto_off:
        return False, "auto_off is DISABLED — a fallback pulse would latch the relay on"
    if delay and delay > 2.0:
        return False, f"auto_off_delay is {delay}s — longer than a button press"
    return True, f"auto_off {delay}s"
