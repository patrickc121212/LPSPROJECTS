# Dad's Tesla Vehicle Tracker

A Flask + Tailscale web app that tracks three Teslas live on a Leaflet map,
sends family messages between drivers, and auto-opens the right garage
door via Google Assistant Routines (Shelly linked through the Shelly
Cloud skill — the web tier never talks to Shelly directly).

Built per [Plan.md](Plan.md). Vehicles:

| Key  | Driver | Vehicle          | Owns    |
|------|--------|------------------|---------|
| mom  | Mom    | Model Y "Rosie"        | Garage 1 |
| dad  | Dad    | Cybertruck "AeroTitan" | Garage 2 (middle) |
| lp   | LP     | Model 3                | Garage 3 |

## Features

1. **Live map** — Leaflet + OpenStreetMap, all 3 vehicles updated via SSE,
   fed by Fleet Telemetry push (sub-second from the car).
2. **Per-vehicle messaging** — compose to one driver; in-app inbox is the
   source of truth, Twilio SMS is a fallback push if the recipient hasn't
   checked in for `SMS_FALLBACK_AFTER_S` seconds.
3. **Manual door controls** — per-garage Open/Close buttons. Each click
   fires a Google Assistant Routine (e.g. "Open Garage 1"); the existing
   Shelly Cloud skill handles the relay.
4. **Geofence auto-open** — the Tesla poller runs every ~30s; the
   geofence worker computes haversine distance to each door's center
   and fires the door's open routine on the moment a permitted vehicle
   **enters** the circle (edge-triggered — a car parked at home never
   re-fires). `GEOFENCE_DEBOUNCE_S` is a second guard against GPS jitter
   at the fence line. Permissions: **owner-per-door + explicit allowlist**.
5. **Allowlist editor** — `/doors` page lets you grant or revoke a
   non-owner's auto-open access per door.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
# edit .env (set APP_PASSWORD + FLASK_SECRET; leave simulator on for now)
python app.py
# open http://localhost:5000  (sign in as family / changeme)
```

In simulator mode (default when `TESLA_ACCESS_TOKEN` is empty) each
vehicle drives a scripted round trip from its owner's garage — park,
~600 m out, park, back — so you can watch the geofence auto-open fire on
every return without real cars or real Shellys. Set
`TESLA_POLL_INTERVAL_S=2` in `.env` for a faster demo loop.

## Running it 24/7 (Windows host)

`run_tracker.cmd` supervises the app (restarts it 10 s after any exit) and
logs to `data/app.log`. It's registered as the Scheduled Task
**"Dads Vehicle Tracker"** — runs hidden at logon (+30 s so Docker is up),
no time limit. Manage it in Task Scheduler or:

```powershell
schtasks /Run  /TN "Dads Vehicle Tracker"    # start now
schtasks /End  /TN "Dads Vehicle Tracker"    # stop (kill python.exe too if it lingers)
Get-Content datapp.log -Tail 50 -Wait      # follow the log
```

Requirements for it to stay reachable: PC set to never sleep on AC
(`powercfg /change standby-timeout-ac 0`), Docker Desktop set to start at
sign-in, and the user logged in (the task is an at-logon task).

## Tests

```bash
pytest            # 80 tests, ~15 s, no network
```

The suite runs against a temp SQLite file with the background workers
disabled. Tesla, Twilio and the Google webhook are faked.

## Wiring up real services

### Tesla Fleet API — onboarding (done once, via `tesla_setup.py`)

Tesla's Fleet API is pay-per-use (500 `vehicle_data` polls / $1, 150k
telemetry signals / $1, $10/month credit). Polling three cars every 30 s
would be ~$500/month, so production uses **Fleet Telemetry** (the cars push
to our server) and polling is only a fallback.

```bash
python tesla_setup.py check       # public key hosted? partner registered? tokens?
python tesla_setup.py register    # one-time: register TESLA_DOMAIN with Tesla
python tesla_setup.py login       # prints a sign-in URL for the account that owns the cars
python tesla_setup.py exchange "<redirected url>"   # -> data/tesla_tokens.json
python tesla_setup.py vehicles    # VINs -> .env
python tesla_setup.py pair        # virtual-key link to open on each owner's phone
python tesla_setup.py status      # key paired? firmware / telemetry version
```

Needs `TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET` (developer.tesla.com app),
the app's *Allowed Origin* domain hosting
`/.well-known/appspecific/com.tesla.3p.public-key.pem`, and the matching
P-256 private key at `TESLA_PRIVATE_KEY`. Tokens refresh automatically
(refresh tokens are single-use and rotate; the file is rewritten each time).

### Fleet Telemetry (production vehicle source)

See **[telemetry/README.md](telemetry/README.md)** — Docker Compose with
Tesla's `fleet-telemetry` receiver, Mosquitto, and a Let's Encrypt cert via
Cloudflare DNS-01. Then:

```bash
python tesla_setup.py telemetry          # sign + push the signal config to the cars
python tesla_setup.py telemetry-status   # synced?
```

and set `VEHICLE_SOURCE=telemetry` in `.env`. `telemetry_worker.py` folds
the per-field MQTT messages into `vehicle_state` and pushes SSE.

The config is signed with your virtual key using Tesla's Schnorr-P256 JWT
scheme (`tesla_jws.py`, verified against Tesla's own test vectors), so no
`tesla-http-proxy` is required.

### Polling fallback (`VEHICLE_SOURCE=poll`)

Uses the same token file. On a 429 the poller backs off ×4 per hit (capped
at 16× the interval); other errors ×2. Keep `TESLA_POLL_INTERVAL_S` ≥ 300 —
every call is billed and polling keeps the cars awake.

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
3. Point Twilio's inbound webhook at `https://your-host/sms`. Once
   `TWILIO_AUTH_TOKEN` is set, every inbound request must carry a valid
   `X-Twilio-Signature` or it gets a 403.

Fallback SMS never marks a message read in the app — it's a nudge. Each
message is pushed at most once (`inbox.sms_sent_at`).

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
├── tesla_poller.py     Simulator + Fleet API polling fallback
├── telemetry_worker.py Fleet Telemetry (MQTT) consumer — production source
├── tesla_setup.py      One-time onboarding CLI (register / login / pair / telemetry)
├── tesla_jws.py        Tesla SS256 (Schnorr-P256) JWT signer for telemetry configs
├── telemetry/          docker-compose: fleet-telemetry + mosquitto + certbot, runbook
├── geofence_worker.py  Haversine + Google Routine trigger
├── sms.py              Twilio send + inbound + fallback sweeper
├── templates/          Jinja2 templates
├── static/             app.js, style.css
├── data/               SQLite file (gitignored)
├── tests/              pytest suite (conftest + 4 modules)
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
