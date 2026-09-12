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
- Dad's Cyberbeast — owner of Garage 1
- LP's Model 3 — owner of Garage 2
- Mom's Model Y — owner of Garage 3

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

## Status (2026-09-12)
- All five core features implemented and covered by `tests/` (80 tests; `pytest`).
- Verified end-to-end in simulator mode: each car's scripted round trip fires its owner's door exactly once per return.
- **Tesla onboarding complete**: partner `dowdsgarage.com` registered (na), OAuth tokens obtained (auto-refresh), VINs mapped (Cybertruck→Dad, Model 3→LP, Model Y→Mom), virtual key paired on all three cars (fw 2026.26.6, telemetry 1.3.0). One live `vehicle_data` call confirmed the real-API path works.
- Fleet Telemetry receiver stack + signed-config pusher written and unit-tested; **not yet deployed** (needs home-server steps in `telemetry/README.md`).
- Google Routine webhook and Twilio paths run in dry-run mode until their env vars are set.

## Next steps
1. **Deploy the telemetry receiver** on the home server per `telemetry/README.md`: grey-cloud DNS record, router 443 forward, Cloudflare API token, `certbot`, `docker compose up`, then `python tesla_setup.py telemetry` and `VEHICLE_SOURCE=telemetry`.
2. Set real garage lat/lon + radius per door in `.env`; check the circles on the map.
3. IFTTT (or Apps Script) webhook → `GOOGLE_ROUTINE_WEBHOOK_URL`; confirm a manual Open from `/doors` moves the Shelly.
4. Twilio number + `/sms` webhook URL; send a test message and wait out `SMS_FALLBACK_AFTER_S`.
5. Confirm MagicDNS resolves in the Tesla in-car browser; else Cloudflare Tunnel.
