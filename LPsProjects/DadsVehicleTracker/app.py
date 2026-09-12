"""
Dad's Tesla Vehicle Tracker — Flask app.

Routes:
  GET  /                  redirect to /map
  GET  /login             shared family login
  GET  /logout
  GET  /map               live Leaflet map + door controls
  GET  /inbox             per-driver messaging
  GET  /doors             garage door controls + allowlist editor
  GET  /api/vehicles      full vehicle snapshot (JSON)
  GET  /api/doors         full door snapshot (JSON)
  GET  /api/inbox/<key>   inbox for a driver (JSON)
  POST /api/message       {sender_key, recipient_key, body}
  POST /api/door          {door_key, action: open|close}
  POST /api/allowlist     {door_key, vehicle_keys: [...]}
  POST /api/inbox/read    {recipient_key}
  GET  /stream            SSE: events 'vehicles' | 'doors' | 'inbox'
  POST /sms               Twilio inbound webhook (form-encoded)
"""
from __future__ import annotations

import hmac
import logging
import os
import queue
from datetime import datetime
from functools import wraps
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import config
import geofence_worker
import models
import sms
import telemetry_worker
import tesla_poller
from eventbus import bus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")


def create_app(start_workers: bool = True) -> Flask:
    """Build the Flask app. `start_workers=False` skips the background
    threads (poller / geofence / SMS sweeper) — used by the test suite."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.secret_key = os.getenv("FLASK_SECRET", "dev-only-change-me")
    app.config["JSON_SORT_KEYS"] = False
    # The JSON API is cookie-authenticated; Lax blocks cross-site POSTs
    # from carrying the session cookie, which is our CSRF story for v1.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    models.init_db()
    if start_workers:
        log.info("Vehicle source: %s", config.VEHICLE_SOURCE)
        if config.VEHICLE_SOURCE == "telemetry":
            telemetry_worker.start_background()
        else:
            tesla_poller.start_background()
        geofence_worker.start_background()
        sms.start_background()

    @app.template_filter("localtime")
    def _localtime(ts: float | None) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")

    # --- Auth -------------------------------------------------------------

    def login_required(fn):
        @wraps(fn)
        def _w(*a, **kw):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return _w

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = request.form.get("username", "")
            p = request.form.get("password", "")
            ok_u = hmac.compare_digest(u, config.SHARED_LOGIN_USERNAME)
            ok_p = hmac.compare_digest(p, config.SHARED_LOGIN_PASSWORD)
            if ok_u and ok_p:
                session["user"] = u
                nxt = request.args.get("next", "")
                # Only follow relative paths; never an absolute URL.
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("map_view")
                return redirect(nxt)
            flash("Invalid credentials.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- Pages ------------------------------------------------------------

    @app.route("/")
    def root():
        return redirect(url_for("map_view"))

    @app.route("/map")
    @login_required
    def map_view():
        return render_template(
            "map.html",
            vehicles=config.VEHICLES,
            vehicles_public=[v.public() for v in config.VEHICLES],
            doors=config.GARAGE_DOORS,
        )

    @app.route("/inbox")
    @login_required
    def inbox_view():
        me = request.args.get("as", config.VEHICLES[0].key)
        if me not in config.VEHICLES_BY_KEY:
            abort(404)
        return render_template(
            "inbox.html",
            vehicles=config.VEHICLES,
            me=me,
            messages=models.list_inbox(me),
        )

    @app.route("/doors")
    @login_required
    def doors_view():
        return render_template(
            "doors.html",
            vehicles=config.VEHICLES,
            doors=config.GARAGE_DOORS,
            door_states=models.all_door_states(),
            allowlists=models.all_allowlists(),
        )

    # --- JSON API ---------------------------------------------------------

    @app.route("/api/vehicles")
    @login_required
    def api_vehicles() -> Any:
        return jsonify(models.all_vehicle_states())

    @app.route("/api/doors")
    @login_required
    def api_doors() -> Any:
        return jsonify(models.all_door_states())

    @app.route("/api/inbox/<recipient_key>")
    @login_required
    def api_inbox(recipient_key: str):
        if recipient_key not in config.VEHICLES_BY_KEY:
            abort(404)
        return jsonify(models.list_inbox(recipient_key))

    @app.route("/api/message", methods=["POST"])
    @login_required
    def api_message():
        data = request.get_json(force=True)
        sender = data.get("sender_key", "")
        recipient = data.get("recipient_key", "")
        body = (data.get("body") or "").strip()
        if sender not in config.VEHICLES_BY_KEY or recipient not in config.VEHICLES_BY_KEY:
            abort(400, "unknown driver")
        if not body:
            abort(400, "empty body")
        msg_id = models.add_message(sender, recipient, body)
        bus.publish("inbox", {"recipient_key": recipient})
        return jsonify({"id": msg_id, "ok": True})

    @app.route("/api/inbox/read", methods=["POST"])
    @login_required
    def api_inbox_read():
        data = request.get_json(force=True)
        recipient = data.get("recipient_key", "")
        if recipient not in config.VEHICLES_BY_KEY:
            abort(400)
        models.mark_read(recipient)
        return jsonify({"ok": True})

    @app.route("/api/door", methods=["POST"])
    @login_required
    def api_door():
        data = request.get_json(force=True)
        door_key = data.get("door_key", "")
        action = data.get("action", "")
        door = config.GARAGE_DOORS_BY_KEY.get(door_key)
        if door is None or action not in ("open", "close"):
            abort(400)
        routine = door.routine_open if action == "open" else door.routine_close
        geofence_worker._trigger_routine(routine)
        models.upsert_door_state(door_key, action == "open")
        bus.publish("doors", models.all_door_states())
        return jsonify({"ok": True, "routine": routine})

    @app.route("/api/allowlist", methods=["POST"])
    @login_required
    def api_allowlist():
        data = request.get_json(force=True)
        door_key = data.get("door_key", "")
        vehicle_keys = data.get("vehicle_keys", [])
        if door_key not in config.GARAGE_DOORS_BY_KEY:
            abort(400)
        vehicle_keys = [k for k in vehicle_keys if k in config.VEHICLES_BY_KEY]
        models.set_allowlist(door_key, vehicle_keys)
        return jsonify({"ok": True, "allowlist": vehicle_keys})

    # --- SSE --------------------------------------------------------------

    @app.route("/stream")
    @login_required
    def stream():
        def gen():
            sid, q = bus.subscribe()
            try:
                # Send a hello so the EventSource open event round-trips.
                yield "event: hello\ndata: {}\n\n"
                while True:
                    try:
                        payload = q.get(timeout=15.0)
                        yield f"data: {payload}\n\n"
                    except queue.Empty:
                        # keep-alive comment; prevents proxies from dropping us
                        yield ": ping\n\n"
            finally:
                bus.unsubscribe(sid)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # --- Twilio inbound webhook. No login; authenticated by X-Twilio-Signature
    # whenever TWILIO_AUTH_TOKEN is set (dev accepts unsigned). ----------------

    @app.route("/sms", methods=["POST"])
    def sms_inbound():
        if not sms.verify_signature(
            request.url,
            request.form.to_dict(),
            request.headers.get("X-Twilio-Signature", ""),
        ):
            abort(403)
        sms.handle_inbound(
            from_number=request.form.get("From", ""),
            body=request.form.get("Body", ""),
        )
        # Twilio expects TwiML; an empty <Response> means "no reply".
        xml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(xml, mimetype="text/xml")

    return app


if __name__ == "__main__":
    app = create_app()
    # Bind to all interfaces so Tailscale (and the in-car browser over
    # MagicDNS) can reach us; in production put a reverse proxy in front.
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "5000")), threaded=True)
