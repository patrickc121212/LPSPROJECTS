"""
Tesla "SS256" JWS signer — a Schnorr signature over NIST P-256, used to sign
Fleet Telemetry configs (and other fleet-wide messages) so that vehicles can
verify they came from the holder of the partner's virtual key.

This is a line-for-line port of teslamotors/vehicle-command
internal/schnorr + internal/authentication/jwt.go. It is verified against
that package's test vectors in tests/test_jws.py. Pure Python; not
constant-time — fine for signing one config every few months, don't use it
in a hot path.

Scheme (all scalars mod N, the P-256 group order):
    k  = RFC 6979 deterministic nonce from (private scalar, SHA-256(msg))
    R  = k * G                                    (public nonce, uncompressed)
    c  = SHA-256( LV(G) || LV(R) || LV(P) || LV(msg) )   LV = 4-byte BE length + bytes
    r  = k - a * c
    sig = R.x || R.y || r                          (96 bytes)

JWT: header {"alg":"Tesla.SS256","typ":"JWT"}; claims get
    iss = base64(P uncompressed)   aud = "com.tesla.fleet.<app>"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# --- NIST P-256 (secp256r1) domain parameters -------------------------------
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
G = (GX, GY)

Point = tuple[int, int] | None  # None is the point at infinity


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k: int, pt: Point) -> Point:
    acc: Point = None
    while k:
        if k & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        k >>= 1
    return acc


def _on_curve(pt: Point) -> bool:
    if pt is None:
        return False
    x, y = pt
    return (y * y - (x * x * x + A * x + B)) % P == 0


def _marshal(pt: Point) -> bytes:
    """Uncompressed SEC1 encoding, 65 bytes (what Go's elliptic.Marshal emits)."""
    assert pt is not None
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def _unmarshal(b: bytes) -> Point:
    if len(b) != 65 or b[0] != 0x04:
        return None
    pt = (int.from_bytes(b[1:33], "big"), int.from_bytes(b[33:], "big"))
    return pt if _on_curve(pt) else None


# --- RFC 6979 deterministic nonce (q = N, hash = SHA-256) -------------------

def deterministic_nonce(scalar: bytes, digest: bytes) -> bytes:
    """k for the given private scalar and 32-byte message digest. Matches
    vehicle-command's schnorr.DeterministicNonce, which is RFC 6979 §3.2
    with bits2octets(h1) = int2octets(int(h1) mod N)."""
    h1 = (int.from_bytes(digest, "big") % N).to_bytes(32, "big")
    k = b"\x00" * 32
    v = b"\x01" * 32
    k = hmac.new(k, v + b"\x00" + scalar + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + scalar + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 0 < cand < N:
            return v
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


# --- Schnorr sign / verify ---------------------------------------------------

def _lv(h, buf: bytes) -> None:
    h.update(len(buf).to_bytes(4, "big"))
    h.update(buf)


def _challenge(public_nonce: bytes, sender_public: bytes, message: bytes) -> int:
    h = hashlib.sha256()
    _lv(h, _marshal(G))
    _lv(h, public_nonce)
    _lv(h, sender_public)
    _lv(h, message)
    return int.from_bytes(h.digest(), "big")


def _scalar_bytes(priv: ec.EllipticCurvePrivateKey) -> bytes:
    return priv.private_numbers().private_value.to_bytes(32, "big")


def public_bytes(priv: ec.EllipticCurvePrivateKey) -> bytes:
    nums = priv.public_key().public_numbers()
    return _marshal((nums.x, nums.y))


def sign(priv: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    if not isinstance(priv.curve, ec.SECP256R1):
        raise ValueError("Tesla SS256 requires a P-256 key")
    scalar = _scalar_bytes(priv)
    a = int.from_bytes(scalar, "big")
    k_bytes = deterministic_nonce(scalar, hashlib.sha256(message).digest())
    k = int.from_bytes(k_bytes, "big")
    public_nonce = _marshal(_mul(k, G))
    c = _challenge(public_nonce, public_bytes(priv), message)
    r = (k - a * c) % N
    return public_nonce[1:] + r.to_bytes(32, "big")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    pub = _unmarshal(public_key)
    if pub is None:
        raise ValueError("invalid public key")
    if len(signature) != 96:
        return False
    nonce = _unmarshal(b"\x04" + signature[:64])
    if nonce is None:
        return False
    r = int.from_bytes(signature[64:], "big")
    c = _challenge(b"\x04" + signature[:64], public_key, message)
    # r*G + c*P == k*G  <=>  (k - a*c)*G + c*(a*G) == k*G
    return _add(_mul(r, G), _mul(c, pub)) == nonce


# --- JWT -----------------------------------------------------------------------

ALG = "Tesla.SS256"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def sign_for_fleet(priv: ec.EllipticCurvePrivateKey, app: str, claims: dict) -> str:
    """Equivalent of vehicle-command sign.SignMessageForFleet: a JWT any
    vehicle trusting this key will accept. Overwrites iss/aud like the Go
    code does."""
    payload = dict(claims)
    payload["iss"] = base64.b64encode(public_bytes(priv)).decode("ascii")
    payload["aud"] = f"com.tesla.fleet.{app}"
    header = {"alg": ALG, "typ": "JWT"}
    signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = sign(priv, signing_input.encode("ascii"))
    return signing_input + "." + _b64url(sig)


def load_private_key(path: str) -> ec.EllipticCurvePrivateKey:
    with open(path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError(f"{path} is not a P-256 EC private key")
    return key
