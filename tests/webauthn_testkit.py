"""Test-only WebAuthn authenticator simulation. NEVER imported by the application.

It holds the signing side that the Hub deliberately does not have: affine P-256 scalar multiplication
(independent of webauthn.py's Jacobian code), RFC 6979 deterministic ECDSA, a fixed small RSA key,
a CBOR encoder and builders for clientDataJSON / attestationObject / authenticatorData.
"""
import hashlib
import hmac
import json

import webauthn

P, N, B = webauthn.P, webauthn.N, webauthn.B
G = (webauthn.GX, webauthn.GY)

# ---- P-256, affine (slow but obviously correct) ----------------------------------------------


def point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        slope = (3 * x1 * x1 - 3) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def scalar_mult(k, point=G):
    result, addend = None, point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def public_key(private):
    return scalar_mult(private)


def rfc6979_k(private, digest):
    """RFC 6979 section 3.2 for P-256 / SHA-256 (qlen == hlen == 256)."""
    x = private.to_bytes(32, "big")
    h1 = (int.from_bytes(digest, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def ecdsa_sign(private, message):
    digest = hashlib.sha256(message).digest()
    z = int.from_bytes(digest, "big")
    k = rfc6979_k(private, digest)
    r = scalar_mult(k)[0] % N
    s = pow(k, -1, N) * (z + r * private) % N
    if s > N // 2:       # low-s, as most authenticators produce (the verifier accepts both)
        s = N - s
    return r, s


def der_int(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + bytes([len(raw)]) + raw


def der_signature(r, s):
    body = der_int(r) + der_int(s)
    return b"\x30" + bytes([len(body)]) + body


# ---- RSA (fixed 512-bit test key; webauthn.MIN_RSA_BITS is lowered by the tests that use it) --
RSA_N = 0xcf6fc015d22cfb022e94d120b9d19d58eeb005440f1265d839edeb51b7cae84a93deec5c79ac8097adabcb585199faea4bc101a8291adf52fa4ccc1c202d29ed
RSA_D = 0x22a158630c34802f2c11a21c218972084fc8c90844afe09ac98903b91fc2db26931a57613e517637d1ce4ee79112bece1282c136b6a8adf799714dded9af24ad
RSA_E = 65537


def rsa_sign(message, n=RSA_N, d=RSA_D):
    size = (n.bit_length() + 7) // 8
    tail = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(size, "big")


# ---- CBOR encoder ----------------------------------------------------------------------------
def _head(major, value):
    if value < 24:
        return bytes([major << 5 | value])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * size):
            return bytes([major << 5 | info]) + value.to_bytes(size, "big")
    raise ValueError("too big")


def cbor(value):
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return _head(3, len(raw)) + raw
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


# ---- authenticator ---------------------------------------------------------------------------
def cose_ec2(x, y):
    return cbor({1: 2, 3: -7, -1: 1, -2: x.to_bytes(32, "big"), -3: y.to_bytes(32, "big")})


def cose_rsa(n=RSA_N, e=RSA_E):
    return cbor({1: 3, 3: -257, -1: n.to_bytes((n.bit_length() + 7) // 8, "big"),
                 -2: e.to_bytes((e.bit_length() + 7) // 8, "big")})


def client_data(kind, challenge, origin, **extra):
    data = {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
    data.update(extra)
    return json.dumps(data, separators=(",", ":")).encode()


class Authenticator:
    """One platform credential; kind is "es256" or "rs256"."""

    def __init__(self, kind="es256", private=0x1c0ffee_c0de_f00d_5ec12e7, credential_id=b"cred-" + bytes(range(27)),
                 user_handle=b""):
        self.kind, self.private, self.credential_id, self.user_handle = kind, private, credential_id, user_handle
        self.counter = 0
        if kind == "es256":
            self.cose = cose_ec2(*public_key(private))
        else:
            self.cose = cose_rsa()

    def sign(self, message):
        if self.kind == "es256":
            return der_signature(*ecdsa_sign(self.private, message))
        return rsa_sign(message)

    def auth_data(self, rp_id, flags=0x05, attested=False, counter=None, extra=b""):
        data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags | (0x40 if attested else 0)])
        data += (self.counter if counter is None else counter).to_bytes(4, "big")
        if attested:
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + self.cose
        return data + extra

    def create(self, options, origin, rp_id=None, flags=0x05, fmt="none", att_stmt=None, tamper=None):
        """Registration response fields (base64url strings) for a begin response."""
        rp_id = rp_id or options["rp"]["id"]
        cd = client_data("webauthn.create", options["challenge"], origin)
        auth = self.auth_data(rp_id, flags, attested=True)
        att = cbor({"fmt": fmt, "attStmt": att_stmt if att_stmt is not None else {}, "authData": auth})
        if tamper:
            cd, att = tamper(cd, att)
        enc = webauthn.b64url_encode
        return {"id": enc(self.credential_id), "rawId": enc(self.credential_id), "clientDataJSON": enc(cd),
                "attestationObject": enc(att), "transports": ["internal"], "label": "テストiPhone"}

    def get(self, options, origin, rp_id=None, flags=0x05, counter=None, kind="webauthn.get", with_handle=True):
        rp_id = rp_id or options["rpId"]
        if counter is None:
            self.counter += 1
            counter = self.counter
        cd = client_data(kind, options["challenge"], origin)
        auth = self.auth_data(rp_id, flags, counter=counter)
        sig = self.sign(auth + hashlib.sha256(cd).digest())
        enc = webauthn.b64url_encode
        result = {"id": enc(self.credential_id), "rawId": enc(self.credential_id), "clientDataJSON": enc(cd),
                  "authenticatorData": enc(auth), "signature": enc(sig)}
        if with_handle:
            result["userHandle"] = enc(self.user_handle) if self.user_handle else None
        return result
