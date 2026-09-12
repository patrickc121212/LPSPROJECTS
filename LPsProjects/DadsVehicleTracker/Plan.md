# Dad's Tesla Vehicle Tracker — Plan

## Decisions locked
- Stack: Python + Flask
- Hosting: Tailscale + LAN host (home server, e.g. Pi/NUC). Reachable from in-car browsers via MagicDNS.
- Map updates: LIVE (push). Use SSE or WebSocket; do not rely on page refresh.
- Auth: Shared family login — everyone sees everyone. Per-garage-door preferences live in config/DB.
- Shelly integration: Shelly devices already linked to Google Home via the Shelly Cloud skill. Website triggers a Google Assistant Routine ("Open Dad's Garage", etc.) — no direct Shelly API call from the web tier.
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
- **Garage doors don't actually move yet** — `GOOGLE_ROUTINE_WEBHOOK_URL` is empty, so the geofence/door buttons log `[dry-run] would fire routine: Open Garage N`.
- **No SMS/push** — `TWILIO_*` and `PHONE_*` are empty. Decision pending: replace Twilio with **ntfy** push (free; see Open decisions).
- ~~Nothing survives a reboot~~ **DONE 2026-09-12**: app runs under Scheduled Task "Dads Vehicle Tracker" (at logon +30 s, hidden, self-supervising launcher `run_tracker.cmd`, log `data/app.log`; crash-tested: back in 3 s). PC set to never sleep/hibernate on AC. Tailscale is an auto-start service; containers are `unless-stopped`. **Remaining caveat:** it is an at-logon task, so after a reboot someone must sign in to Windows (or enable auto-login) — and verify Docker Desktop's "Start when you sign in" is ticked.
- **Windows Firewall rule** for port 5000 restricted to Tailscale (100.64.0.0/10) was attempted but needs a UAC click; currently relying on whatever Python's existing firewall allowance is. Phone-over-tailnet access not yet confirmed (laptop test was inconclusive — laptop connectivity).
- ~~In-car browser test~~ **DONE 2026-09-12: tested in two cars, "everything is working great"** — the Tesla browser handles the Funnel URL, cert, login and Leaflet map. Plan's biggest open risk is closed.
- **Geofence validated on real data**: Rosie returned home twice (10:05, 14:26); each fired exactly one `Open Garage 1` (dry-run), no re-fires while parked.
- Public URL is already being scanned by bots (`GET /.env` → 404 within 30 min of Funnel on). Flask login is the only gate today.

### Open decisions (Patrick)
1. **Push channel**: ntfy (free, phone app, recommended) vs Twilio SMS (~$1.15/mo + per-text; US numbers need 10DLC or toll-free). Leaning ntfy; Twilio stays optional.
2. **Custom domain**: (A) `tracker.dowdsgarage.com` via Cloudflare Tunnel, leaving the root Pages site (which hosts the Tesla public key) untouched — recommended; or (B) bare `dowdsgarage.com`, which requires Flask to also serve `/.well-known/appspecific/com.tesla.3p.public-key.pem`. Either way, put **Cloudflare Access** (free ≤50 users) in front. Needs a tunnel token from one.dash.cloudflare.com → Networks → Tunnels.
3. **Long-term host**: the Windows PC is a stopgap; a Pi 5 / mini-PC would run the Docker stack + app 24/7. Runbook in `telemetry/README.md` is host-agnostic.

## Next steps (suggested order)
1. ~~Car browser test~~ done.
2. ~~Survive reboots~~ done, except: (a) confirm Docker Desktop → Settings → General → "Start Docker Desktop when you sign in" is ticked; (b) decide on Windows auto-login or accept signing in after reboots; (c) **schedule weekly `certbot-renew` + `docker compose restart fleet-telemetry`** (cert expires 2026-12-11) — not yet done.
3. **Firewall rule** (needs someone at the PC to click Yes on UAC): allow TCP 5000 from 100.64.0.0/10 only.
4. **Google Home webhook** → real door control: IFTTT "Webhooks → Google Assistant (V2) trigger routine" or Apps Script; set `GOOGLE_ROUTINE_WEBHOOK_URL` (+ token); confirm a manual Open from `/doors` moves the Shelly; then let the geofence fire for real. Consider a manual-close timeout.
5. **Push notifications**: implement ntfy sender alongside Twilio in `sms.py`; per-driver random topics in `.env`; family installs the ntfy app.
6. **Custom domain + Access** per decision 2.
7. **DDNS** for `telemetry.dowdsgarage.com` — home IP is `68.184.61.239` today; if the ISP rotates it, the cars lose the receiver. Cloudflare token already has DNS edit rights; a `cloudflare-ddns` container in `telemetry/docker-compose.yml` would cover it.
8. Invite Mom and LP to the tailnet (or rely on the Funnel URL + Access).

## Where things live
- Project: `LPSPROJECTS/LPsProjects/DadsVehicleTracker` (`main`, pushed through `cd4abb5`).
- Secrets (all gitignored): `.env` (Tesla client id/secret, VINs, APP_PASSWORD, geofence coords), `data/tesla_tokens.json`, `data/tesla_private_key.pem`, `telemetry/cloudflare.ini`, `telemetry/certs/`, `telemetry/.env`.
- Tesla developer app: client id `65f81dac-…`, Allowed Origin `dowdsgarage.com`, redirect `https://dowdsgarage.com/callback` (a 404 page — fine). Public key hosted on Cloudflare Pages.
- Cloudflare zone `dowdsgarage.com` id `398ef2d3…`; API token (Edit zone DNS) in `telemetry/cloudflare.ini`.
- Tailscale: tailnet `taild11993.ts.net`, account patrickc121212@gmail.com.
- Ops CLI: `python tesla_setup.py {check|status|telemetry-status|telemetry|telemetry-delete|refresh}`.
