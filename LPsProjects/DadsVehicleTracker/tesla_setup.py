"""
One-time Tesla Fleet API onboarding for Dad's Tesla Tracker.

    python tesla_setup.py check       # is the public key hosted? is the partner registered?
    python tesla_setup.py register    # register dowdsgarage.com as a partner (per region)
    python tesla_setup.py login       # print the Tesla sign-in URL (remembers the OAuth state)
    python tesla_setup.py exchange <redirected-url>   # finish login -> data/tesla_tokens.json
    python tesla_setup.py vehicles    # list VINs on the account -> paste into .env
    python tesla_setup.py pair        # print the virtual-key pairing link for the phone
    python tesla_setup.py refresh     # force a token refresh (sanity check)
    python tesla_setup.py status      # key paired? firmware + telemetry version per car
    python tesla_setup.py telemetry   # sign + push the Fleet Telemetry config to the cars
    python tesla_setup.py telemetry-status   # has each car adopted the config?
    python tesla_setup.py telemetry-delete   # stop streaming (removes our config)

Reads from .env:
    TESLA_CLIENT_ID, TESLA_CLIENT_SECRET   developer.tesla.com app credentials
    TESLA_DOMAIN                           domain hosting the public key (Allowed Origin)
    TESLA_REDIRECT_URI                     must match an Allowed Redirect URI in the app
    TESLA_REGION                           na | eu | cn
    TESLA_PRIVATE_KEY                      P-256 key matching the hosted public key
    TELEMETRY_HOST / TELEMETRY_PORT        where the cars stream to (public, mTLS)
    TELEMETRY_CA_FILE                      PEM chain that issued the server's TLS cert

Tokens are written to data/tesla_tokens.json (gitignored). The poller reads
the same file and keeps it fresh; refresh tokens are single-use and rotate.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("TESLA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TESLA_CLIENT_SECRET", "")
DOMAIN = os.getenv("TESLA_DOMAIN", "dowdsgarage.com")
REDIRECT_URI = os.getenv("TESLA_REDIRECT_URI", f"https://{DOMAIN}/callback")
REGION = os.getenv("TESLA_REGION", "na")
TOKENS_PATH = Path(os.getenv("TESLA_TOKENS_PATH", "data/tesla_tokens.json"))
PRIVATE_KEY_PATH = os.getenv("TESLA_PRIVATE_KEY", "data/tesla_private_key.pem")
TELEMETRY_HOST = os.getenv("TELEMETRY_HOST", f"telemetry.{DOMAIN}")
TELEMETRY_PORT = int(os.getenv("TELEMETRY_PORT", "443"))
TELEMETRY_CA_FILE = os.getenv("TELEMETRY_CA_FILE", "telemetry/certs/ca.pem")

AUTH_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"

# What the tracker needs: identity, a refresh token, vehicle state, and
# location (a separate scope since 2024). No command scopes — we never
# drive the cars from here.
SCOPES = ["openid", "offline_access", "user_data", "vehicle_device_data", "vehicle_location"]


def _require_creds() -> None:
    missing = [k for k, v in (("TESLA_CLIENT_ID", CLIENT_ID), ("TESLA_CLIENT_SECRET", CLIENT_SECRET)) if not v]
    if missing:
        sys.exit(f"Set {', '.join(missing)} in .env first.")


def load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def save_tokens(tokens: dict) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except OSError:
        pass  # Windows; rely on the folder ACL


# --- Partner (application-level) ---------------------------------------------

async def partner_token(session: aiohttp.ClientSession) -> str:
    from tesla_fleet_api.const import SERVERS
    async with session.post(AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": " ".join(SCOPES),
        "audience": SERVERS[REGION],
    }) as resp:
        data = await resp.json()
        if not resp.ok:
            sys.exit(f"Partner token failed ({resp.status}): {data}")
        return data["access_token"]


async def cmd_check() -> None:
    async with aiohttp.ClientSession() as session:
        # 1. Is the public key reachable where Tesla will look for it?
        url = f"https://{DOMAIN}{PUBLIC_KEY_PATH}"
        async with session.get(url) as resp:
            body = await resp.text()
            ok = resp.status == 200 and "BEGIN PUBLIC KEY" in body
            print(f"[{'ok' if ok else 'FAIL'}] public key at {url} (HTTP {resp.status})")
            if ok:
                print("      " + body.strip().splitlines()[1][:40] + "...")

        if not (CLIENT_ID and CLIENT_SECRET):
            print("[skip] partner registration check — set TESLA_CLIENT_ID/SECRET in .env")
            return

        # 2. Does Tesla already know this domain for our app?
        from tesla_fleet_api import TeslaFleetApi
        token = await partner_token(session)
        api = TeslaFleetApi(session, access_token=token, region=REGION)
        try:
            info = await api.partner.public_key(domain=DOMAIN)
            print(f"[ok] partner registered in region '{REGION}':")
            print("     " + json.dumps(info.get("response", info))[:300])
        except BaseException as exc:  # noqa: BLE001 — SDK errors don't all derive from Exception
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            print(f"[no] partner not registered in '{REGION}' yet ({exc}). Run: python tesla_setup.py register")

    toks = load_tokens()
    if toks:
        left = toks.get("expires", 0) - time.time()
        print(f"[ok] tokens on disk; access token {'valid' if left > 0 else 'EXPIRED'} ({left/3600:.1f} h left), refresh token present: {bool(toks.get('refresh_token'))}")
    else:
        print("[no] no user tokens yet. Run: python tesla_setup.py login")


async def cmd_register() -> None:
    _require_creds()
    from tesla_fleet_api import TeslaFleetApi
    async with aiohttp.ClientSession() as session:
        token = await partner_token(session)
        api = TeslaFleetApi(session, access_token=token, region=REGION)
        result = await api.partner.register(DOMAIN)
        print(json.dumps(result, indent=2))
        print(f"\nRegistered '{DOMAIN}' in region '{REGION}'.")


# --- User (account-level) OAuth ----------------------------------------------

def _oauth(session: aiohttp.ClientSession, toks: dict | None = None):
    from tesla_fleet_api import TeslaFleetOAuth
    toks = toks or {}
    return TeslaFleetOAuth(
        session,
        region=REGION,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        access_token=toks.get("access_token"),
        refresh_token=toks.get("refresh_token"),
        expires=int(toks.get("expires", 0)),
    )


STATE_PATH = TOKENS_PATH.with_name(".oauth_state")


async def cmd_login() -> None:
    """Step 1: print the sign-in URL. Step 2 is `exchange` (or paste inline
    when run in a real terminal)."""
    _require_creds()
    from tesla_fleet_api.const import Scope
    state = secrets.token_urlsafe(16)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(state, encoding="utf-8")
    async with aiohttp.ClientSession() as session:
        oauth = _oauth(session)
        url = oauth.get_login_url([Scope(s) for s in SCOPES], state=state)
    print("\n1. Open this URL and sign in with the Tesla account that owns the cars:\n")
    print("   " + url + "\n")
    print(f"2. You'll land on {REDIRECT_URI}?code=... (a 404 page is fine).")
    print("3. Copy the FULL address from the browser bar, then run:\n")
    print('   python tesla_setup.py exchange "<that url>"\n')
    if sys.stdin.isatty():
        try:
            pasted = input("...or paste it here now (Enter to skip): ").strip()
        except EOFError:
            return
        if pasted:
            await _exchange(pasted)


async def _exchange(pasted: str) -> None:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    code = (qs.get("code") or [""])[0]
    got_state = (qs.get("state") or [""])[0]
    if not code:
        sys.exit("No ?code= in that URL.")
    want = STATE_PATH.read_text(encoding="utf-8").strip() if STATE_PATH.exists() else ""
    if got_state != want:
        sys.exit("state mismatch — run `login` again and use the URL from that run.")
    async with aiohttp.ClientSession() as session:
        oauth = _oauth(session)
        await oauth.get_refresh_token(code)
        if not oauth._access_token:
            sys.exit("Code exchange failed. Codes are single-use and short-lived; make sure "
                     f"TESLA_REDIRECT_URI ({REDIRECT_URI}) matches the app EXACTLY, then run login again.")
        if not oauth.refresh_token:
            sys.exit("Tesla returned no refresh token; check that offline_access is an allowed scope in the app.")
        save_tokens({
            "access_token": oauth._access_token,
            "refresh_token": oauth.refresh_token,
            "expires": oauth.expires,
            "region": REGION,
            "obtained_at": time.time(),
        })
    STATE_PATH.unlink(missing_ok=True)
    print(f"Saved tokens to {TOKENS_PATH}. Access token valid ~{(oauth.expires - time.time())/3600:.0f} h; refresh token rotates automatically.")


async def cmd_exchange() -> None:
    _require_creds()
    if len(sys.argv) < 3:
        sys.exit('usage: python tesla_setup.py exchange "<redirected url>"')
    await _exchange(sys.argv[2])


async def _user_api(session: aiohttp.ClientSession):
    toks = load_tokens()
    if not toks.get("refresh_token"):
        sys.exit("No tokens. Run: python tesla_setup.py login")
    oauth = _oauth(session, toks)
    if oauth.expires < time.time() + 60:
        await oauth.refresh_access_token()
        toks.update(access_token=oauth._access_token, refresh_token=oauth.refresh_token, expires=oauth.expires)
        save_tokens(toks)
    return oauth


async def cmd_vehicles() -> None:
    _require_creds()
    async with aiohttp.ClientSession() as session:
        api = await _user_api(session)
        resp = await api.products()
        # products() mixes vehicles and energy sites; vehicles have a VIN.
        cars = [p for p in resp.get("response", []) if p.get("vin")]
        if not cars:
            print("No vehicles on this account.")
            return
        print(f"\n{'VIN':<20} {'Name':<24} {'State':<10} Model")
        for c in cars:
            vin = c.get("vin", "")
            print(f"{vin:<20} {str(c.get('display_name') or ''):<24} {str(c.get('state') or ''):<10} {vin[3] if len(vin) > 3 else ''}")
        print("\nPaste into .env (match each VIN to the right driver):")
        for key in ("DAD", "LP", "MOM"):
            print(f"TESLA_VIN_{key}=")


async def cmd_refresh() -> None:
    _require_creds()
    async with aiohttp.ClientSession() as session:
        toks = load_tokens()
        oauth = _oauth(session, toks)
        await oauth.refresh_access_token()
        toks.update(access_token=oauth._access_token, refresh_token=oauth.refresh_token, expires=oauth.expires)
        save_tokens(toks)
        print(f"Refreshed. New access token valid ~{(oauth.expires - time.time())/3600:.0f} h.")


def _vins() -> dict[str, str]:
    vins = {k: os.getenv(f"TESLA_VIN_{k}", "") for k in ("DAD", "LP", "MOM")}
    missing = [k for k, v in vins.items() if not v]
    if missing:
        sys.exit(f"Missing TESLA_VIN_{'/'.join(missing)} in .env — run: python tesla_setup.py vehicles")
    return vins


async def cmd_status() -> None:
    """fleet_status: is our virtual key on each car, and can it stream?"""
    _require_creds()
    vins = _vins()
    async with aiohttp.ClientSession() as session:
        api = await _user_api(session)
        res = await api.vehicles.createFleet(next(iter(vins.values()))).fleet_status(list(vins.values()))
        r = res.get("response", res)
        paired = set(r.get("key_paired_vins", []))
        info = r.get("vehicle_info") or {}
        for k, vin in vins.items():
            i = info.get(vin, {})
            print(f"{k:<4} {vin}  key_paired={'YES' if vin in paired else 'NO '}  "
                  f"fw={i.get('firmware_version')}  telemetry={i.get('fleet_telemetry_version')}")
        if r.get("unpaired_vins"):
            print(f"\nUnpaired: {r['unpaired_vins']} — open https://tesla.com/_ak/{DOMAIN} on the owner's phone.")


def telemetry_fields() -> dict:
    """Signals the tracker needs. The car sends a field only when BOTH the
    interval has elapsed AND the value changed (minimum_delta for Location,
    in metres), so a parked car costs nothing. Pricing is 150k signals/$1;
    three cars driving ~2 h/day at these rates is well under $2/month."""
    loc_s = int(os.getenv("TELEMETRY_LOCATION_S", "5"))
    return {
        "Location":            {"interval_seconds": loc_s, "minimum_delta": 10, "delivery_policy": "latest"},
        "VehicleSpeed":        {"interval_seconds": 10},
        "Gear":                {"interval_seconds": 10},
        "BatteryLevel":        {"interval_seconds": 60},
        "DetailedChargeState": {"interval_seconds": 60},
        "VehicleName":         {"interval_seconds": 3600},
    }


def build_telemetry_config() -> dict:
    ca_path = Path(TELEMETRY_CA_FILE)
    if not ca_path.exists():
        sys.exit(f"{ca_path} not found. It must hold the full PEM chain that issued the telemetry server's "
                 "TLS cert (for Let's Encrypt: the intermediate + ISRG Root X1). See telemetry/README.md.")
    ca = ca_path.read_text(encoding="utf-8").strip()
    if "BEGIN CERTIFICATE" not in ca:
        sys.exit(f"{ca_path} does not look like a PEM certificate chain.")
    return {
        "hostname": TELEMETRY_HOST,
        "port": TELEMETRY_PORT,
        "ca": ca,
        "fields": telemetry_fields(),
        "alert_types": ["service"],
    }


async def cmd_telemetry() -> None:
    """Sign the telemetry config with our virtual key and push it to all
    three cars via fleet_telemetry_config_jws (what tesla-http-proxy does)."""
    _require_creds()
    import tesla_jws
    vins = _vins()
    key = tesla_jws.load_private_key(PRIVATE_KEY_PATH)
    config = build_telemetry_config()
    token = tesla_jws.sign_for_fleet(key, "TelemetryClient", config)
    print(f"Pushing telemetry config -> {TELEMETRY_HOST}:{TELEMETRY_PORT} "
          f"({len(config['fields'])} fields) to {len(vins)} cars...")
    async with aiohttp.ClientSession() as session:
        api = await _user_api(session)
        vf = api.vehicles.createFleet(next(iter(vins.values())))
        from tesla_fleet_api.const import Method
        res = await vf._request(Method.POST, "api/1/vehicles/fleet_telemetry_config_jws",
                                json={"vins": list(vins.values()), "token": token})
    r = res.get("response", res)
    by_vin = {v: k for k, v in vins.items()}
    for vin in r.get("updated_vehicles", []) if isinstance(r.get("updated_vehicles"), list) else []:
        print(f"  [ok] {by_vin.get(vin, vin)} {vin}")
    if isinstance(r.get("updated_vehicles"), int):
        print(f"  updated_vehicles: {r['updated_vehicles']}")
    skipped = r.get("skipped_vehicles") or {}
    for reason, lst in skipped.items():
        for vin in lst or []:
            print(f"  [skipped:{reason}] {by_vin.get(vin, vin)} {vin}")
    if not skipped and not r.get("updated_vehicles"):
        print(json.dumps(r, indent=2))
    print("\nCars adopt the config when next online; check with: python tesla_setup.py telemetry-status")


async def cmd_telemetry_status() -> None:
    _require_creds()
    vins = _vins()
    async with aiohttp.ClientSession() as session:
        api = await _user_api(session)
        for k, vin in vins.items():
            try:
                res = await api.vehicles.createFleet(vin).fleet_telemetry_config_get()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"{k:<4} {vin}  error: {exc}")
                continue
            r = res.get("response", res)
            cfg = r.get("config") or {}
            print(f"{k:<4} {vin}  synced={r.get('synced')}  host={cfg.get('hostname')}:{cfg.get('port')}  "
                  f"fields={len(cfg.get('fields') or {})}  limit_reached={r.get('limit_reached')}")


async def cmd_telemetry_delete() -> None:
    _require_creds()
    vins = _vins()
    async with aiohttp.ClientSession() as session:
        api = await _user_api(session)
        for k, vin in vins.items():
            res = await api.vehicles.createFleet(vin).fleet_telemetry_config_delete()
            print(f"{k:<4} {vin}  {json.dumps(res.get('response', res))}")


def cmd_pair() -> None:
    print(f"""
Virtual key pairing (needed for Fleet Telemetry; do this once per car):

  1. On a phone with the Tesla app signed in as the car's owner, open:

        https://tesla.com/_ak/{DOMAIN}

  2. The Tesla app opens and asks to add the "{DOMAIN}" key to the car.
     Approve. Repeat with each of the three cars selected in the app
     (or have each owner do it on their own phone).

  The key that gets installed is the public key hosted at
  https://{DOMAIN}{PUBLIC_KEY_PATH} — so the matching private key is
  what the telemetry server and any future signed commands will use.
""")


COMMANDS = {
    "check": cmd_check,
    "register": cmd_register,
    "login": cmd_login,
    "exchange": cmd_exchange,
    "vehicles": cmd_vehicles,
    "refresh": cmd_refresh,
    "pair": cmd_pair,
    "status": cmd_status,
    "telemetry": cmd_telemetry,
    "telemetry-status": cmd_telemetry_status,
    "telemetry-delete": cmd_telemetry_delete,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    fn = COMMANDS[sys.argv[1]]
    if asyncio.iscoroutinefunction(fn):
        asyncio.run(fn())
    else:
        fn()
