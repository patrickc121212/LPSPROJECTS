"""tesla_jws against the test vectors from teslamotors/vehicle-command
(internal/schnorr/*_test.go). If these pass, the signer matches Tesla's."""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import ec

import tesla_jws as j


def _key_from_scalar(scalar: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())


# testKey(): 32 zero bytes with scalar[0] = 3  (i.e. 3 << 248, NOT the integer 3)
TEST_KEY = _key_from_scalar(bytes([3]) + bytes(31))

GOOD_SIG = bytes([
    0x7c, 0xfd, 0xbe, 0xb5, 0xba, 0xa7, 0x30, 0x54, 0x04, 0x01, 0x55, 0x0b,
    0xde, 0xfa, 0x20, 0x97, 0x64, 0x53, 0xe8, 0x53, 0x9a, 0xe4, 0xb2, 0xf2,
    0x6c, 0xe3, 0x31, 0x25, 0x80, 0x1a, 0x08, 0xf9, 0x0e, 0xd2, 0x0c, 0x3d,
    0x84, 0x64, 0x97, 0xff, 0x82, 0xcc, 0x97, 0x72, 0xe3, 0xdb, 0x47, 0x03,
    0x98, 0x2f, 0x47, 0xbd, 0x0b, 0x0b, 0x89, 0xdf, 0xb9, 0xa4, 0x9c, 0xd2,
    0xe5, 0x24, 0x05, 0x46, 0x02, 0xb1, 0xe0, 0x5f, 0xbf, 0x95, 0xf5, 0x68,
    0x6f, 0xae, 0xa7, 0xa5, 0x80, 0x9e, 0xb9, 0x2f, 0x5e, 0xcc, 0x22, 0xea,
    0xe7, 0x4c, 0xec, 0xcc, 0x5e, 0x2a, 0x65, 0xdd, 0x67, 0xff, 0x20, 0xfc,
])


def test_rfc6979_vector_a_2_5():
    """RFC 6979 appendix A.2.5, P-256 / SHA-256, message 'sample'."""
    scalar = bytes.fromhex("c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721")
    expected = bytes.fromhex("a6e3c57dd01abe90086538398355dd4c3b17aa873382b0f24d6129493d8aad60")
    assert j.deterministic_nonce(scalar, hashlib.sha256(b"sample").digest()) == expected


def test_rejection_sampling_vector():
    """First candidate exceeds N; the resampled value must match Go's."""
    digest = bytes.fromhex("0080c36864c5f2f460e3767983c65677b65cef901bcedcb223f9b365c68f52f6")
    expected = bytes.fromhex("264fc6592fbea24fd0954e0b86b886e87431617 58ddad2f7e9fed75a0019e005".replace(" ", ""))
    got = j.deterministic_nonce(j._scalar_bytes(TEST_KEY), digest)
    assert got == expected
    assert int.from_bytes(got, "big") < j.N


def test_sign_matches_go_vector():
    assert j.sign(TEST_KEY, b"hello world") == GOOD_SIG


def test_verify_good_signature():
    assert j.verify(j.public_bytes(TEST_KEY), b"hello world", GOOD_SIG) is True


def test_verify_rejects_tampering():
    pub = j.public_bytes(TEST_KEY)
    assert j.verify(pub, b"iello world", GOOD_SIG) is False
    bad = bytearray(GOOD_SIG); bad[-1] ^= 1
    assert j.verify(pub, b"hello world", bytes(bad)) is False
    bad = bytearray(GOOD_SIG); bad[0] ^= 1  # nonce off-curve
    assert j.verify(pub, b"hello world", bytes(bad)) is False
    other = _key_from_scalar(bytes([4]) + bytes(31))
    assert j.verify(j.public_bytes(other), b"hello world", GOOD_SIG) is False
    assert j.verify(pub, b"hello world", GOOD_SIG[:-1]) is False


def test_fleet_jwt_shape_and_signature():
    tok = j.sign_for_fleet(TEST_KEY, "TelemetryClient", {"hostname": "t.example.com", "port": 443})
    h, p, s = tok.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    header = json.loads(base64.urlsafe_b64decode(pad(h)))
    payload = json.loads(base64.urlsafe_b64decode(pad(p)))
    assert header == {"alg": "Tesla.SS256", "typ": "JWT"}
    assert payload["aud"] == "com.tesla.fleet.TelemetryClient"
    assert base64.b64decode(payload["iss"]) == j.public_bytes(TEST_KEY)
    assert payload["hostname"] == "t.example.com" and payload["port"] == 443
    assert j.verify(j.public_bytes(TEST_KEY), f"{h}.{p}".encode(), base64.urlsafe_b64decode(pad(s)))


def test_random_key_roundtrip():
    k = ec.generate_private_key(ec.SECP256R1())
    msg = b"x" * 1000
    assert j.verify(j.public_bytes(k), msg, j.sign(k, msg))
