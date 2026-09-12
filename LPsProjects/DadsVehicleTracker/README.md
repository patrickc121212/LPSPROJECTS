# Dad's Tesla Vehicle Tracker

A Flask + Tailscale web app that tracks three Teslas live on a Leaflet map,
sends family messages between drivers, and auto-opens the right garage
door via Google Assistant Routines (Shelly linked through the Shelly
Cloud skill — the web tier never talks to Shelly directly).

Built per [Plan.md](Plan.md). Vehicles:

| Key  | Driver | Vehicle          | Owns    |
|------|--------|------------------|---------|
| dad  | Dad    | Cyberbeast       | Garage 1|
| lp   | LP     | Model 3          | Garage 2|
| mom  | Mom    | Model Y          | Garage 3|

## Features

1. **Live map** — Leaflet + OpenStreetMap, all 3 vehicles updated via SSE.
2. **Per-vehicle messaging** — compose to one driver; in-app inbox is the
   source of truth, Twilio SMS is a fallback push if the recipient hasn't
   checked in for `SMS_FALLBACK_AFTER_S` seconds.
3. **Manual door controls** — per-garage Open/Close buttons. Each click
   fires a Google Assistant Routine (e.g. "Open Garage 1"); the existing
   Shelly Cloud skill handles the relay.
4. **Geofence auto-open** — the Tesla poller runs every ~30s; the
   geofence worker computes haversine distance to each door's center
   and fires the door's open routine when a permitted vehicle enters.
   Permissions: **owner-per-door + explicit allowlist**. Debounced to
   avoid re-firing while a car sits in the zone.
5. **Allowlist editor** — `/doors` page lets you grant or revoke a
   non-owner's auto-open access per door.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env (set APP_PASSWORD; leave simulator on for now)
python app.py
# open http://localhost:5000  (sign in as family / changeme)
```

In simulator mode (default when `TESLA_ACCESS_TOKEN` is empty) each
vehicle orbits its owner's garage so you can see the geofence auto-open
firing end-to-end without real cars or real Shellys.

## Wiring up real services

### Tesla Fleet API
1. Create a Tesla developer account at https://developer.tesla.com.
2. Register a partner application and get a `client_id` + `client_secret`.
3. Pair each VIN via the Fleet API OAuth flow. Put the resulting bearer
   token in `TESLA_ACCESS_TOKEN` and the VINs in `TESLA_VIN_DAD` /
   `TESLA_VIN_LP` / `TESLA_VIN_MOM`.
4. Set `TRACKER_SIMULATE=0`. The poller switches to the real Fleet API
   client automatically.

### Google Assistant Routines (for Shelly)
We do NOT call the Shelly API directly. The flow is:

```
web tier → POST /routine-webhook → Google Assistant Routine → Shelly Cloud skill → Shelly device
```

Simplest path — IFTTT:
1. IFTTT → "Receive a web request" trigger, event name e.g. `open_garage_1`.
2. Action: "Google Assistant → Trigger routine by name" → "Open Garage 1".
3. Webhook URL: `https://maker.ifttt.com/trigger/{event}/with/key/{your_key}`.
4. Set `GOOGLE_ROUTINE_WEBHOOK_URL` to that URL, and make sure the
   routine names in `.env` match what you set up in Google Home.

Or use Google Apps Script as a webhook receiver that calls the Smart Home API directly.

### Twilio
1. Buy a Twilio number with SMS capability.
2. Drop `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
   into `.env`, plus each driver's phone in `PHONE_DAD` / `PHONE_LP` /
   `PHONE_MOM`.
3. Point Twilio's inbound webhook at `https://your-host/sms`.

### Tailscale + in-car browser
Run this on your home server (Pi/NUC), bind to the Tailscale interface
(`tailscale0`), and the in-car browser can hit it via MagicDNS:
`http://tracker.tailnet-name.ts.net:5000`. If the in-car Chromium
rejects the cert, expose via Cloudflare Tunnel as a fallback.

## Architecture

```
        ┌──────────────────┐
        │  Tesla Fleet API │ (real) or simulator
        └────────┬─────────┘
                 │ 30s poll
                 ▼
┌──────┐    ┌─────────┐    ┌──────────────┐
│ Poller│──▶│  SQLite │◀───│Geofence worker│──▶ Google Routine webhook
└──────┘    │ (state) │    └────────────────┘
            └────┬────┘
                 │ SSE: vehicles / doors / inbox
                 ▼
            ┌─────────┐
            │ Browser │ (Leaflet, in-car or any LAN device)
            └─────────┘
```

Background threads (`tesla_poller`, `geofence_worker`, `sms`) are
started from `create_app()` and run for the lifetime of the process.

## Layout

```
DadsVehicleTracker/
├── app.py              Flask routes + SSE
├── config.py           Vehicles, doors, geofences, owner model
├── models.py           SQLite schema + helpers
├── eventbus.py         In-process pub/sub for SSE
├── tesla_poller.py     Tesla Fleet API client + simulator
├── geofence_worker.py  Haversine + Google Routine trigger
├── sms.py              Twilio send + inbound + fallback sweeper
├── templates/          Jinja2 templates
├── static/             app.js, style.css
├── data/               SQLite file (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```

## Out of scope (v1)

- Custom Google Smart Home Action (using the existing Shelly skill).
- Per-driver login (shared family login for now).
- Polygon geofences (circles only).
- Voice control from inside the car (in-car browser only).

See [Plan.md](Plan.md) for the full plan and open questions.
