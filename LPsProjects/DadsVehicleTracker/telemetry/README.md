# Fleet Telemetry receiver — setup runbook

The cars push their position to a server **you** run; Tesla charges per
signal (150,000 / $1) instead of per poll (500 / $1). Three cars at the
configured rates cost well under the $10/month credit. This folder is that
server. It runs on the home server next to the Flask app (currently: the Windows
PC, under Docker Desktop).

```
Tesla car ──mTLS :443──▶ fleet-telemetry ──MQTT──▶ mosquitto ◀── telemetry_worker.py (Flask)
                         (tesla/fleet-telemetry)   (:1883, LAN only)
```

Already done (from the dev machine, via `tesla_setup.py`): partner
registered, OAuth tokens in `data/tesla_tokens.json`, virtual key paired on
all three cars, VINs in `.env`.

## 0. Prerequisites on the home server

- Docker + Docker Compose plugin
- This repo checked out, `.env` copied over **including** `data/tesla_tokens.json`
  and `data/tesla_private_key.pem` (both gitignored — copy them by hand, e.g. `scp`)
- Python venv as in the main README (`pip install -r requirements.txt`)

## 1. DNS + router (one-time)

1. Cloudflare → DNS → add **A record** `telemetry` → your home public IP,
   **Proxy status: DNS only (grey cloud)**. Cloudflare's proxy would
   terminate TLS and break the car's mTLS handshake, so it must be grey.
   If your ISP changes your IP, run a DDNS updater for this record
   (e.g. `oznu/cloudflare-ddns` container) — not covered here.
2. Router → port forwarding → **TCP 443 → home server IP : 443**.
   Only 443. Do **not** forward 1883 (MQTT) or 5000 (Flask).

## 2. Cloudflare API token (for the TLS cert)

The server needs a publicly trusted cert for `telemetry.dowdsgarage.com`.
We use Let's Encrypt with the DNS-01 challenge, so port 80 stays closed.

1. https://dash.cloudflare.com/profile/api-tokens → Create Token →
   template **"Edit zone DNS"** → Zone Resources: *Include → Specific zone →
   dowdsgarage.com* → Create. Copy the token.
2. On the home server:
   ```bash
   cd telemetry
   cp cloudflare.ini.example cloudflare.ini && chmod 600 cloudflare.ini
   # paste the token into cloudflare.ini
   cp .env.example .env
   # set LETSENCRYPT_EMAIL in .env
   ```

## 3. Get the certificate

```bash
docker compose --profile cert run --rm certbot
ls certs/live/telemetry.dowdsgarage.com/     # fullchain.pem  privkey.pem  chain.pem
```

The receiver runs as uid 65532 while certbot writes as root; the deploy hook
in `hooks/` fixes ownership so the receiver can read the key. It runs
automatically on issue and renew. If you ever see
`permission denied ... fullchain.pem` in the receiver log, run it by hand:

```bash
docker run --rm -v "$PWD/certs:/etc/letsencrypt" -v "$PWD/hooks:/h:ro" alpine sh /h/10-fleet-telemetry-perms.sh
```

Build the CA bundle the cars will be told to trust — the chain that
*issued* the server cert. Certbot's `chain.pem` already holds the
intermediate(s) and root:

```bash
cp certs/live/telemetry.dowdsgarage.com/chain.pem certs/ca.pem
```

Renewal: Let's Encrypt certs last 90 days. Add to the server's crontab:

```
0 4 * * 1  cd /path/to/DadsVehicleTracker/telemetry && docker compose --profile cert run --rm certbot-renew && docker compose restart fleet-telemetry
```

After a renewal the *leaf* changes but the chain normally doesn't, so the
config on the cars stays valid. If Let's Encrypt rotates its intermediate,
rebuild `certs/ca.pem` and re-run step 5.

## 4. Start the receiver

```bash
docker compose up -d
docker compose logs -f fleet-telemetry     # expect: "server started" on :443
```

Verify from outside the LAN (phone on cellular, or any VPS):

```bash
openssl s_client -connect telemetry.dowdsgarage.com:443 -servername telemetry.dowdsgarage.com </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

You should see your Let's Encrypt cert. (The server will then close the
connection because you presented no client cert — that's mTLS working.)

Tesla also ships a checker: `tools/check_server_cert.sh` in
github.com/teslamotors/fleet-telemetry.

## 5. Tell the cars where to stream

From any machine with the repo, `.env`, tokens and private key
(`TELEMETRY_CA_FILE` defaults to `telemetry/certs/ca.pem`):

```bash
python tesla_setup.py telemetry           # signs config with the virtual key, pushes to all 3 VINs
python tesla_setup.py telemetry-status    # synced=True once each car has adopted it (may take a drive/wake)
```

Signals configured (edit `telemetry_fields()` in `tesla_setup.py`):

| Field | Interval | Notes |
|---|---|---|
| Location | 5 s | only when moved ≥ 10 m → parked = silent |
| VehicleSpeed, Gear | 10 s | on change |
| BatteryLevel, DetailedChargeState | 60 s | on change |
| VehicleName | 1 h | |

The car sends a field only when the interval has elapsed **and** the value
changed, so idle cars cost nothing.

## 6. Switch the Flask app to telemetry

In the app's `.env`:

```
VEHICLE_SOURCE=telemetry
MQTT_HOST=127.0.0.1        # or the docker host's LAN IP if Flask runs elsewhere
```

Restart the app; watch for `Telemetry worker: 3 VINs mapped` then
`MQTT connected`. Then watch the map — the first signals arrive the next
time a car wakes or moves.

Debugging what arrives:

```bash
docker exec mosquitto mosquitto_sub -t 'telemetry/#' -v
```

## 7. Rollback / stop

```bash
python tesla_setup.py telemetry-delete    # cars stop streaming
docker compose down
```

Set `VEHICLE_SOURCE=poll` (with `TESLA_POLL_INTERVAL_S=300`) for the
billed polling fallback, or `sim` for the simulator.

## Security notes

- Port 443 on the home server is now internet-facing. `fleet-telemetry`
  only completes the handshake for clients presenting a **Tesla-issued
  vehicle certificate**; everyone else is dropped at TLS. Keep the image
  updated (`docker compose pull && docker compose up -d`).
- Mosquitto is anonymous on purpose (LAN/Tailscale). Never forward 1883.
- `cloudflare.ini`, `certs/`, and `.env` are gitignored. The private key in
  `../data/` is what lets anyone push configs to the cars — treat it like
  the car key it is.
