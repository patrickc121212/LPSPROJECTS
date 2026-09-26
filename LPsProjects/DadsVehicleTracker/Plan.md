# Dad's Tesla Vehicle Tracker — Plan

## Decisions locked
- Stack: Python + Flask
- Hosting: Tailscale + LAN host (home server, e.g. Pi/NUC). Reachable from in-car browsers via MagicDNS.
- Map updates: LIVE (push). Use SSE or WebSocket; do not rely on page refresh.
- Auth: Shared family login — everyone sees everyone. Per-garage-door preferences live in config/DB.
- ~~Shelly integration: website triggers a Google Assistant Routine; no direct Shelly API call from the web tier.~~ **Changed 2026-09-26: the web tier now pulses the Shelly directly over the LAN.** One hop instead of four (~50 ms vs 1–3 s), works during an internet outage, and removes the IFTTT / routine-name / Shelly-Cloud-skill dependencies. Google Home voice control still works independently through the Shelly Cloud skill — we simply do not route our own triggers through it. Per-door: a door with no `GARAGE{N}_SHELLY_HOST` falls back to the routine webhook, then to dry-run.
- Geofence auto-open: hard-coded per-door lat/long + radius. Owner-per-door model: each garage door (Shelly) has one designated owner. Owner + allowlist override: owner's vehicle always opens; other drivers can be explicitly allowed or denied per door.
- Messaging: BOTH in-app inbox per driver AND SMS via Twilio. In-app is source of truth; SMS push if recipient hasn't checked in for N minutes.

## Vehicles
- Mom's Model Y ("Rosie") — owner of Garage 1
- Dad's Cybertruck ("AeroTitan") — owner of Garage 2 (middle bay)
- LP's Model 3 — owner of Garage 3

(Door numbers follow the physical bays so Google Home's "Open Garage N" moves door N. Confirmed on site 2026-09-12.)

## Core features
1. Live map (Leaflet + OpenStreetMap tiles) showing 3 vehicles, refreshed via SSE.
2. Per-vehicle selector → compose message → sends to in-app inbox (and SMS via Twilio).
3. Per-garage-door manual Open/Close button.
4. Per-garage-door auto-open: backend polls Tesla Fleet API; if owner's vehicle enters the door's geofence, trigger the corresponding Google Home Routine.
5. Shared family login + per-door allowlist configuration screen (per-driver login is v2; see Out of scope).

## Architecture (high level)
- Flask app on home server, bound to Tailscale interface.
- Vehicle source: **Fleet Telemetry** — cars push Location/Speed/Battery over mTLS to a `fleet-telemetry` receiver on the home server → MQTT → `telemetry_worker` → SQLite. (Polling `vehicle_data` every 30 s would cost ~$500/month at Tesla's 2025+ pricing; telemetry is ~$0 under the $10 credit. Poller kept as a slow fallback.)
- Geofence worker: computes haversine; fires the door's open routine on the outside→inside transition only (edge-triggered, plus a debounce against GPS jitter at the fence line). A car parked at home never re-fires.
- SSE endpoint streams vehicle position + door state changes to the browser.
- Shelly status mirrored from Google Home Graph (query only); we do NOT talk to Shelly directly.
- Outbound SMS via Twilio API; inbound messages handled via Twilio webhook → /sms endpoint → store in inbox + push SSE event.
- Secret storage: env vars or `python-dotenv`; never commit.

## Out of scope (v1)
- Custom Google Smart Home Action (using existing Shelly skill instead).
- Per-driver login (using shared login for v1).
- Polygon geofences (using circles only).
- Voice control from inside the car (in-car browser only).

## Open questions / risks
- Tailscale on the Tesla in-car browser: Teslas run a stripped Chromium; we may need to confirm MagicDNS resolves and the in-car browser allows the Tailscale cert. Fallback: expose via Cloudflare Tunnel from the same LAN host.
- Shelly Cloud skill → Google Home round-trip latency (~1–3 s). Acceptable for manual; auto-open should debounce.
- Tesla Fleet API rate limits — keep polling conservative; back off on 429. *(Implemented: poller backs off ×4 per 429, ×2 per other error, capped at 16× the poll interval.)*
- **Tesla Fleet API pricing** (new since plan was drafted): 500 data polls / $1, 50 wakes / $1, 150k telemetry signals / $1, $10/mo credit. Drove the decision to stream rather than poll.
- Telemetry receiver needs a public 443 with a real cert and must terminate mTLS itself — `telemetry.dowdsgarage.com` must be a DNS-only (grey-cloud) Cloudflare record; home IP changes need DDNS.

## Status (as of 2026-09-12, end of day)

### Working, live, on real cars
- **Fleet Telemetry is deployed and streaming.** Receiver (`tesla/fleet-telemetry` v0.9.4) + Mosquitto run in Docker Desktop on the Windows PC (`192.168.1.40`, Wi-Fi). Public `telemetry.dowdsgarage.com` (Cloudflare, **DNS-only/grey cloud**) → router forwards TCP 443 → PC. Let's Encrypt cert via Cloudflare DNS-01, valid to **2026-12-11**; renewal is `docker compose --profile cert run --rm certbot-renew` (not yet scheduled — see Next steps).
- Signed telemetry config pushed to all 3 VINs (`updated_vehicles: 3`). **AeroTitan (Cybertruck) connected within a minute and streams** Location / Speed / Gear / Battery / charge state. Model 3 and Rosie showed `synced=False` (asleep) — they adopt the config on next wake, no action needed. Check: `python tesla_setup.py telemetry-status`.
- Tracker app runs in `VEHICLE_SOURCE=telemetry` mode and shows the truck's real position. Geofence centres for all three doors = the truck's parked fix (one point; 75 m radius covers the three bays).
- **Door ownership follows the physical bays**: Garage 1 = Mom/Rosie, Garage 2 = Dad/AeroTitan (middle), Garage 3 = LP/Model 3. Overridable via `GARAGE{N}_OWNER`.
- **Remote access via Tailscale.** PC is node `tracker` (100.81.24.40). Tailscale Serve + **Funnel on**: the app is public at **https://tracker.taild11993.ts.net** (Let's Encrypt cert, Tailscale edge → 127.0.0.1:5000). HTTPS certs enabled in the tailnet admin; Funnel enabled via `nodeAttrs` in the ACL file. Patrick's S24 Ultra is on the tailnet. `APP_PASSWORD` changed from the default (16 chars).
- Tesla onboarding complete: partner `dowdsgarage.com` registered (na, pay-as-you-go), OAuth tokens in `data/tesla_tokens.json` (auto-refresh), virtual key paired on all three cars (fw 2026.26.6, telemetry 1.3.0).
- 81 tests pass (`pytest`, ~15 s, no network).

### Still dry-run / not wired
- **Garage 2 can now physically move** (Shelly 1 Gen4 at 192.168.1.107, fw 2.0.1, `auto_off 0.5 s`). Garages 1 and 3 are still dry-run until their relays are installed. Old status below for reference:
- ~~**Garage doors don't actually move yet**~~ — `GOOGLE_ROUTINE_WEBHOOK_URL` is empty, so the geofence/door buttons log `[dry-run] would fire routine: Open Garage N`. **Blocked on hardware: the Shelly relays are not installed yet** (plan assumed they were). Order of work once they are: Shelly → Shelly app → link Shelly Cloud skill in Google Home → create routines named exactly "Open Garage 1/2/3" and "Close Garage 1/2/3" → IFTTT/Apps Script webhook → `GOOGLE_ROUTINE_WEBHOOK_URL`.
- **No SMS/push** — `TWILIO_*` and `PHONE_*` are empty. Decision pending: replace Twilio with **ntfy** push (free; see Open decisions).
- ~~Nothing survives a reboot~~ **DONE 2026-09-12**: app runs under Scheduled Task "Dads Vehicle Tracker" (at logon +30 s, windowless `supervisor.pyw` via pythonw, log `data/app.log`; crash-tested: back in 12 s). **Incident 17:06–21:31**: the first launcher was a `cmd` window and someone at the PC closed it (exit 0xC000013A); site was down 4.5 h. Fixed by going windowless. PC set to never sleep/hibernate on AC. Tailscale is an auto-start service; containers are `unless-stopped`. **2026-09-26**: re-registered with an **S4U principal + at-startup trigger**, so the app now starts ~1 min after boot with nobody signed in. Ran unattended for two weeks; supervisor restarted 6× (reboots) and recovered every time. **Remaining caveat:** Docker Desktop (and therefore the telemetry receiver + MQTT) still only starts at *logon* — after a reboot with no sign-in the site is up but shows stale positions until someone signs in.
- **Windows Firewall rule** for port 5000 restricted to Tailscale (100.64.0.0/10) was attempted but needs a UAC click; currently relying on whatever Python's existing firewall allowance is. Phone-over-tailnet access not yet confirmed (laptop test was inconclusive — laptop connectivity).
- ~~In-car browser test~~ **DONE 2026-09-12: tested in two cars, "everything is working great"** — the Tesla browser handles the Funnel URL, cert, login and Leaflet map. Plan's biggest open risk is closed.
- **Geofence validated on real data**: Rosie returned home twice (10:05, 14:26); each fired exactly one `Open Garage 1` (dry-run), no re-fires while parked.
- Public URL is already being scanned by bots (`GET /.env` → 404 within 30 min of Funnel on). Flask login is the only gate today.

### Open decisions (Patrick)
1. **Push channel**: ntfy (free, phone app, recommended) vs Twilio SMS (~$1.15/mo + per-text; US numbers need 10DLC or toll-free). Leaning ntfy; Twilio stays optional.
2. **Custom domain**: (A) `tracker.dowdsgarage.com` via Cloudflare Tunnel, leaving the root Pages site (which hosts the Tesla public key) untouched — recommended; or (B) bare `dowdsgarage.com`, which requires Flask to also serve `/.well-known/appspecific/com.tesla.3p.public-key.pem`. Either way, put **Cloudflare Access** (free ≤50 users) in front. Needs a tunnel token from one.dash.cloudflare.com → Networks → Tunnels.
3. **Long-term host** (now the highest-value remaining infra task): the Windows PC is a stopgap. A Pi 5 / mini-PC runs the Docker stack + app as system services — no logon dependency, survives power cuts unattended, and takes the tracker off the daily-driver PC. Runbook in `telemetry/README.md` is host-agnostic; the move is mostly copy `.env` + `data/` + `telemetry/`, re-point the router's 443 forward, and re-run `docker compose up -d`. The Tesla config needs no change (same hostname/cert).

## Next steps (suggested order)
1. ~~Car browser test~~ done.
2. ~~Survive reboots~~ **done 2026-09-26**: S4U + at-startup trigger registered; weekly cert renewal task registered and dry-run twice (`telemetry/renew_cert.py`, Mondays 04:00, logs to `telemetry/renew.log`, restarts the receiver only on an actual renewal). Docker Desktop autostarts at logon via its Run key (proven: containers returned after the 2026-09-22 reboot). **Windows auto-login: considered and rejected 2026-09-26.** The account is passwordless (Windows Hello; `DevicePasswordLessBuildVersion=2`), so auto-login would require creating an MSA password, re-authing every other device on that account, and storing it for a no-prompt sign-in on an internet-facing PC. Payoff is small: one reboot in 30 days (2026-09-22 07:54 → app back 07:56 via the startup trigger; Docker returned when someone signed in that morning). Residual gap: between a reboot and the next sign-in the site is up but positions are stale and **geofence auto-open will not fire** — which will matter once the Shellys are in. Proper fix is decision 3 (move to a Pi/mini-PC, where Docker is a system service and no logon exists).
3. **Firewall rule** (needs someone at the PC to click Yes on UAC): allow TCP 5000 from 100.64.0.0/10 only.
4. **Install Shelly relays** (one per door; see hardware notes below), then **Google Home webhook** → real door control: IFTTT "Webhooks → Google Assistant (V2) trigger routine" or Apps Script; set `GOOGLE_ROUTINE_WEBHOOK_URL` (+ token); confirm a manual Open from `/doors` moves the Shelly; then let the geofence fire for real. Consider a manual-close timeout.
5. **Push notifications**: implement ntfy sender alongside Twilio in `sms.py`; per-driver random topics in `.env`; family installs the ntfy app.
6. **Custom domain + Access** per decision 2.
7. **DDNS** for `telemetry.dowdsgarage.com` — home IP is `68.184.61.239` today; if the ISP rotates it, the cars lose the receiver. Cloudflare token already has DNS edit rights; a `cloudflare-ddns` container in `telemetry/docker-compose.yml` would cover it.
8. Invite Mom and LP to the tailnet (or rely on the Funnel URL + Access).

## Door control — how it works now (2026-09-26)
- **Auto-close on departure — ENABLED 2026-09-26 at Patrick's explicit request, risk accepted.** Once the door's owner has been outside the fence for `AUTO_CLOSE_DELAY_S` (180 s) and the door is considered open, we pulse it shut, once per departure. **Without a position sensor this acts on a belief.** The common wrong case is the driver closing the door with HomeLink or the wall button on the way out: our belief still says open, the pulse toggles, and the garage is left OPEN at an empty house. A wired reed switch removes this entirely — `door_control.is_open()` already prefers the sensor and the code needs no change beyond setting `GARAGE{N}_SENSOR_INPUT`.
- `AUTO_CLOSE_DELAY_S` must stay below `DOOR_OPEN_TTL_S` or the belief expires before the close fires and auto-close silently never runs. `door_control.config_warnings()` logs this and the sensorless case at every startup.
- **Reaction time** (fixed 2026-09-26): the geofence worker used to sleep 30 s between sweeps, a leftover from the polling era, so auto-open lagged an arrival by ~21 s. It is now event-driven — every position flush calls `geofence_worker.request_tick()` and the loop wakes in microseconds; `GEOFENCE_INTERVAL_S` (30 s) is only a heartbeat for expiring stale state. Typical arrival → pulse is now ~4 s, dominated by the cars' 5 s Location interval.
- **Fence radius 150 m** on all three doors (`GARAGE{N}_RADIUS_M`), raised from 75 m on 2026-09-26: ~13 s of approach at 25 mph, roughly what a door needs to finish opening. Watch for false triggers if a through road passes within 150 m; drop it back if so.
- Remaining lever if it still feels slow: drop the cars' Location `interval_seconds` from 5 to 2 in `tesla_setup.telemetry_fields()` and re-push (`python tesla_setup.py telemetry`). Costs roughly $2/month against the $10 credit.
- `door_control.actuate(door, action)` picks, per door: local Shelly pulse -> Google Routine webhook -> dry-run.
- `shelly.pulse()` sends `Switch.Set?id=0&on=true&toggle_after=0.5`. `toggle_after` is passed explicitly so the pulse length never depends on the device's own `auto_off` config — if that were ever cleared, a plain `on=true` would latch the relay, i.e. hold the opener's button down indefinitely.
- **A pulse toggles the door**; open and close are the same action. There is no sensor, so `door_state` is only ever a memory of our last pulse. The UI says so on `/doors`.
- Two guards make auto-open safe enough to enable without a sensor:
  1. **Never auto-fire at a door we believe is open** — pulsing an open door would shut it on the arriving car.
  2. **Clear that belief when the door's owner leaves the geofence** — the door has closed behind them, so a quick trip out and back still auto-opens. Without this, the belief would block re-opens for `DOOR_OPEN_TTL_S` (default 600 s).
- Residual risk that only a sensor removes: if a door is left open manually and the owner's car then arrives from outside, we believe it closed and the pulse will *close* it. **Fitting a reed switch to the Shelly's SW input is the fix** — `shelly.get_status()` already surfaces `input_state` ready for it.
- **Open security item**: the Shelly has auth disabled (`auth_en: false`), so anything on the LAN can open Garage 2 with one HTTP request. Deferred by choice 2026-09-26.

## Hardware notes — Shelly for garage doors
- A garage opener's wall button is a dry-contact **momentary** input; the relay must *pulse* (~0.5 s), not latch. Shelly 1 / Plus 1 / 1 Mini Gen3 wired across the opener's button terminals, with the Shelly set to **auto-off after 0.5 s** ("button" mode). 12 V / 24 V / 110 V input variants exist — match the power source.
- A pulse **toggles** the door: the same pulse opens or closes. So "Open Garage N" and "Close Garage N" routines both just pulse; without a sensor the app can't know the door's true state — it only *assumes* open after firing. To know for real, add a reed/contact sensor (Shelly Plus Add-on + door sensor, or a Shelly BLU Door/Window) and feed it back as door state. Until then the geofence should **never** fire when it believes the door is already open, and manual buttons should be treated as "toggle".
- Three doors → three Shellys (or one Shelly Pro 3 / 2PM with multiple outputs in a single box near the openers).

## Where things live
- Project: `LPSPROJECTS/LPsProjects/DadsVehicleTracker` (`main`, pushed through `cd4abb5`).
- Secrets (all gitignored): `.env` (Tesla client id/secret, VINs, APP_PASSWORD, geofence coords), `data/tesla_tokens.json`, `data/tesla_private_key.pem`, `telemetry/cloudflare.ini`, `telemetry/certs/`, `telemetry/.env`.
- Tesla developer app: client id `65f81dac-…`, Allowed Origin `dowdsgarage.com`, redirect `https://dowdsgarage.com/callback` (a 404 page — fine). Public key hosted on Cloudflare Pages.
- Cloudflare zone `dowdsgarage.com` id `398ef2d3…`; API token (Edit zone DNS) in `telemetry/cloudflare.ini`.
- Tailscale: tailnet `taild11993.ts.net`, account patrickc121212@gmail.com.
- Ops CLI: `python tesla_setup.py {check|status|telemetry-status|telemetry|telemetry-delete|refresh}`.
