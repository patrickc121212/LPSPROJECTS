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
import time
from datetime import datetime
from functools import wraps
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import charging
import config
import trips
import door_control
import geofence_worker
import login_guard
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
        door_control.startup_check()
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
            key = login_guard.client_key(request.headers, request.remote_addr)
            remaining = login_guard.guard.locked_out(key)
            if remaining > 0:
                log.warning("Rejected login from locked-out %s (%.0fs left)", key, remaining)
                resp = make_response(render_template(
                    "login.html", lockout=int(remaining / 60) + 1), 429)
                resp.headers["Retry-After"] = str(int(remaining))
                return resp

            u = request.form.get("username", "")
            p = request.form.get("password", "")
            ok_u = hmac.compare_digest(u, config.SHARED_LOGIN_USERNAME)
            ok_p = hmac.compare_digest(p, config.SHARED_LOGIN_PASSWORD)
            if ok_u and ok_p:
                login_guard.guard.record_success(key)
                log.info("Login OK from %s", key)
                session["user"] = u
                nxt = request.args.get("next", "")
                # Only follow relative paths; never an absolute URL.
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("map_view")
                return redirect(nxt)
            # Cost every wrong guess real time, and more when many are
            # arriving at once, so grinding is slow rather than free.
            delay = login_guard.guard.delay_for_failure()
            login_guard.guard.record_failure(
                key, login_guard.describe(request.headers))
            time.sleep(delay)
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
            toggle_doors=[d.key for d in config.GARAGE_DOORS if door_control.is_toggle(d)],
        )

    @app.route("/charging")
    @login_required
    def charging_view():
        who = request.args.get("as", "")
        who = who if who in config.VEHICLES_BY_KEY else ""
        sessions = models.list_charge_sessions(limit=200, vehicle_key=who or None)
        return render_template(
            "charging.html",
            vehicles=config.VEHICLES,
            me=who,
            sessions=sessions,
            summary=charging.summary(sessions),
            months=models.charge_totals_by_month(),
            rate=config.ELECTRICITY_RATE_PER_KWH,
            currency=config.CURRENCY_SYMBOL,
        )

    @app.route("/trips")
    @login_required
    def trips_view():
        who = request.args.get("as", "")
        who = who if who in config.VEHICLES_BY_KEY else ""
        rows = models.list_trips(limit=200, vehicle_key=who or None)
        for t in rows:
            t["from_name"] = trips.place_name(t.get("start_lat"), t.get("start_lon"))
            t["to_name"] = trips.place_name(t.get("end_lat"), t.get("end_lon"))
        return render_template(
            "trips.html",
            vehicles=config.VEHICLES,
            me=who,
            trips=rows,
            summary=trips.summary(rows),
            months=models.trip_totals_by_month(),
        )

    # --- JSON API ---------------------------------------------------------

    @app.route("/api/vehicles")
    @login_required
    def api_vehicles() -> Any:
        return jsonify(models.all_vehicle_states())

    @app.route("/api/trips")
    @login_required
    def api_trips() -> Any:
        who = request.args.get("as", "")
        who = who if who in config.VEHICLES_BY_KEY else None
        rows = models.list_trips(limit=200, vehicle_key=who)
        return jsonify({"summary": trips.summary(rows),
                        "by_month": models.trip_totals_by_month(),
                        "trips": rows})

    @app.route("/api/trips/<int:trip_id>/path")
    @login_required
    def api_trip_path(trip_id: int) -> Any:
        trip = models.get_trip(trip_id)
        if trip is None:
            abort(404)
        path = models.trip_path(trip_id)
        if not path:
            # Points recorded before trip tagging, or after a prune, are not
            # attached to the trip: fall back to the time window.
            path = models.positions_between(
                trip["vehicle_key"], trip["started_at"],
                trip.get("ended_at") or (trip["started_at"] + 86400))
        return jsonify({"trip": trip, "path": path})

    @app.route("/api/charging")
    @login_required
    def api_charging() -> Any:
        who = request.args.get("as", "")
        who = who if who in config.VEHICLES_BY_KEY else None
        sessions = models.list_charge_sessions(limit=200, vehicle_key=who)
        return jsonify({
            "rate_per_kwh": config.ELECTRICITY_RATE_PER_KWH,
            "summary": charging.summary(sessions),
            "by_month": models.charge_totals_by_month(),
            "sessions": sessions,
        })

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
        result = door_control.actuate(door, action)
        if result["ok"]:
            models.upsert_door_state(door_key, action == "open")
            bus.publish("doors", models.all_door_states())
        return jsonify({
            "ok": result["ok"],
            "via": result["via"],
            "routine": door.routine_open if action == "open" else door.routine_close,
            "detail": result["detail"],
            "toggle": door_control.is_toggle(door),
        })

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
