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
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("shelly")

DEFAULT_TIMEOUT_S = 4.0
# How long the relay stays closed = how long the opener's button is "pressed".
DEFAULT_PULSE_S = 0.5


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
