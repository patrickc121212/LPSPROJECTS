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

import json
import logging
import time
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

import os as _os

import config
import geofence_worker
import models
import sms
import tesla_poller
from eventbus import bus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.secret_key = _os.getenv("FLASK_SECRET", "dev-only-change-me")
    app.config["JSON_SORT_KEYS"] = False

    models.init_db()
    tesla_poller.start_background()
    geofence_worker.start_background()
    sms.start_background()

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
            if u == config.SHARED_LOGIN_USERNAME and p == config.SHARED_LOGIN_PASSWORD:
                session["user"] = u
                return redirect(request.args.get("next") or url_for("map_view"))
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
            doors=config.GARAGE_DOORS,
        )

    @app.route("/inbox")
    @login_required
    def inbox_view():
        me = request.args.get("as", config.VEHICLES[0].key)
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
                last_ping = time.time()
                while True:
                    try:
                        payload = q.get(timeout=15.0)
                        yield f"data: {payload}\n\n"
                    except Exception:
                        # keep-alive comment; prevents proxies from dropping us
                        yield ": ping\n\n"
                        last_ping = time.time()
            finally:
                bus.unsubscribe(sid)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    # --- Twilio inbound webhook (no login required, signature-checked in prod) ----

    @app.route("/sms", methods=["POST"])
    def sms_inbound():
        result = sms.handle_inbound(
            from_number=request.form.get("From", ""),
            body=request.form.get("Body", ""),
        )
        # Twilio expects TwiML.
        xml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(xml, mimetype="text/xml"), 200 if result.get("status") == "stored" else 200

    return app


app = create_app()


if __name__ == "__main__":
    # Bind to all interfaces so Tailscale (and the in-car browser over
    # MagicDNS) can reach us; in production put a reverse proxy in front.
    app.run(host="0.0.0.0", port=int(_os.getenv("PORT", "5000")), threaded=True)
