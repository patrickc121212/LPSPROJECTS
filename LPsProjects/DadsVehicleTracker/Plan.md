    # Dad's Tesla Vehicle Tracker — Plan (draft)

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
    5. Per-driver login + per-door allowlist configuration screen.

    ## Architecture (high level)
    - Flask app on home server, bound to Tailscale interface.
    - Background worker: Tesla Fleet API poller (every ~30s) → updates vehicle state in SQLite.
    - Geofence worker: computes haversine; on enter/exit, calls Google Smart Home Action or Google Assistant Routine webhook.
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
    - Tesla Fleet API rate limits — keep polling conservative; back off on 429.
    EOF
    echo "saved"
    
    