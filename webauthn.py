"""WebAuthn (passkey) relying-party verification, Python standard library only.

The QPKG ships a plain stdlib Python, so there is no `cryptography` package: this module holds the
minimum needed to register and verify platform passkeys (Face ID / Touch ID):

* a strict minimal CBOR decoder (definite lengths, no tags/floats, size and depth caps),
* authenticator data and COSE key parsing,
* ES256 (ECDSA P-256 + SHA-256, DER signature) and RS256 (RSASSA-PKCS1-v1_5 + SHA-256) VERIFICATION,
* single-use, short-lived, bound challenges.

Nothing here can sign or store a private key; only public keys ever reach the database. Attestation is
requested as "none": other attestation statements are ignored (never trusted, never verified), so a
credential's authenticity is not attested. That is acceptable for a single-admin Hub because the
registering user is already authenticated; the passkey is just a convenient second way to log in.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

ALG_ES256 = -7
ALG_RS256 = -257
CHALLENGE_BYTES = 32
CHALLENGE_TTL = 120.0
CHALLENGE_LIMIT = 256
CREDENTIAL_ID_MAX = 1023
MIN_RSA_BITS = 2048          # tests lower this to use a small fixed key
MAX_RSA_BITS = 4096
CBOR_MAX_BYTES = 16384
CBOR_MAX_DEPTH = 8
CBOR_MAX_ITEMS = 1024

FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80


class WebAuthnError(ValueError):
    """Verification failed; the message is safe to show (Japanese), never contains secrets."""


class SignCountError(WebAuthnError):
    """The signature counter did not increase: possibly a cloned authenticator."""


# ---- base64url -------------------------------------------------------------------------------
_B64URL = re.compile(r"[A-Za-z0-9_-]*\Z")


def b64url_encode(raw):
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def b64url_decode(text, max_bytes=65536):
    """Strict unpadded base64url (alphabet check first; Python's decoder silently skips junk)."""
    if not isinstance(text, str) or len(text) > (max_bytes * 4) // 3 + 4 or not _B64URL.match(text):
        raise WebAuthnError("base64url形式が不正です")
    if len(text) % 4 == 1:
        raise WebAuthnError("base64url形式が不正です")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise WebAuthnError("base64url形式が不正です")
    if len(raw) > max_bytes:
        raise WebAuthnError("データが大きすぎます")
    return raw


# ---- CBOR (RFC 8949 subset) ------------------------------------------------------------------
class _Cbor:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.items = 0

    def take(self, count):
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise WebAuthnError("CBORが途中で終わっています")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def argument(self, info):
        if info < 24:
            return info
        if info in (24, 25, 26, 27):
            return int.from_bytes(self.take(1 << (info - 24)), "big")
        # 28-30 reserved, 31 indefinite length: refused
        raise WebAuthnError("CBORの長さ形式に対応していません")

    def item(self, depth):
        if depth > CBOR_MAX_DEPTH:
            raise WebAuthnError("CBORの入れ子が深すぎます")
        self.items += 1
        if self.items > CBOR_MAX_ITEMS:
            raise WebAuthnError("CBORの要素が多すぎます")
        initial = self.take(1)[0]
        major, info = initial >> 5, initial & 31
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info == 22:
                return None
            raise WebAuthnError("CBORの単純値/浮動小数点には対応していません")
        value = self.argument(info)
        if major == 0:
            return value
        if major == 1:
            return -1 - value
        if major in (2, 3):
            if value > len(self.data) - self.pos:
                raise WebAuthnError("CBORが途中で終わっています")
            raw = self.take(value)
            if major == 2:
                return raw
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise WebAuthnError("CBORの文字列が不正です")
        if major == 4:
            if value > len(self.data) - self.pos:      # every element needs at least one byte
                raise WebAuthnError("CBORが途中で終わっています")
            return [self.item(depth + 1) for _ in range(value)]
        if major == 5:
            if value * 2 > len(self.data) - self.pos:
                raise WebAuthnError("CBORが途中で終わっています")
            result = {}
            for _ in range(value):
                key = self.item(depth + 1)
                if type(key) not in (int, str):
                    raise WebAuthnError("CBORのキーは整数か文字列だけです")
                if key in result:
                    raise WebAuthnError("CBORのキーが重複しています")
                result[key] = self.item(depth + 1)
            return result
        raise WebAuthnError("CBORのタグには対応していません")


def cbor_decode_prefix(data):
    """Decode one item from the start of `data`; returns (value, bytes_consumed)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > CBOR_MAX_BYTES:
        raise WebAuthnError("CBORが大きすぎます")
    reader = _Cbor(bytes(data))
    value = reader.item(0)
    return value, reader.pos


def cbor_decode(data):
    """Decode exactly one item; trailing bytes are an error."""
    value, used = cbor_decode_prefix(data)
    if used != len(data):
        raise WebAuthnError("CBORの後ろに余分なデータがあります")
    return value


# ---- P-256 (secp256r1) ECDSA verification ----------------------------------------------------
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
_INF = (0, 1, 0)             # Jacobian point at infinity (Z == 0)


def on_curve(x, y):
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x - 3 * x + B)) % P == 0


def _double(pt):
    x, y, z = pt
    if z == 0 or y == 0:
        return _INF
    delta, gamma = z * z % P, y * y % P
    beta = x * gamma % P
    alpha = 3 * (x - delta) * (x + delta) % P
    x3 = (alpha * alpha - 8 * beta) % P
    z3 = ((y + z) ** 2 - gamma - delta) % P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P
    return (x3, y3, z3)


def _add(p1, p2):
    if p1[2] == 0:
        return p2
    if p2[2] == 0:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p1) if s1 == s2 else _INF
    h, r = (u2 - u1) % P, (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    v = u1 * h2 % P
    x3 = (r * r - h3 - 2 * v) % P
    y3 = (r * (v - x3) - s1 * h3) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def _to_affine(pt):
    if pt[2] == 0:
        return None
    zi = pow(pt[2], -1, P)
    zi2 = zi * zi % P
    return (pt[0] * zi2 % P, pt[1] * zi2 * zi % P)


def _multi_mul(u1, point1, u2, point2):
    """u1*point1 + u2*point2 (Shamir's trick); points are affine (x, y)."""
    a = (point1[0], point1[1], 1)
    b = (point2[0], point2[1], 1)
    both = _add(a, b)
    acc = _INF
    for bit in range(max(u1.bit_length(), u2.bit_length()) - 1, -1, -1):
        acc = _double(acc)
        first, second = (u1 >> bit) & 1, (u2 >> bit) & 1
        if first and second:
            acc = _add(acc, both)
        elif first:
            acc = _add(acc, a)
        elif second:
            acc = _add(acc, b)
    return _to_affine(acc)


def ecdsa_p256_verify(x, y, message, r, s):
    """True when (r, s) is a valid ECDSA-SHA256 signature of `message` under the public key (x, y).

    Rejects off-curve keys and r or s outside [1, n-1].
    """
    if not on_curve(x, y):
        return False
    if not (0 < r < N and 0 < s < N):
        return False
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")   # 256-bit hash: no truncation
    w = pow(s, -1, N)
    point = _multi_mul(z * w % N, (GX, GY), r * w % N, (x, y))
    return point is not None and point[0] % N == r


def parse_der_signature(sig):
    """Strict DER ECDSA-Sig-Value: SEQUENCE { INTEGER r, INTEGER s } -> (r, s)."""
    def length(buf, pos):
        if pos >= len(buf):
            raise WebAuthnError("署名形式が不正です")
        first = buf[pos]
        if first < 0x80:
            return first, pos + 1
        count = first & 0x7f
        if count != 1 or pos + 1 >= len(buf) or buf[pos + 1] < 0x80:
            raise WebAuthnError("署名形式が不正です")
        return buf[pos + 1], pos + 2

    def integer(buf, pos):
        if pos >= len(buf) or buf[pos] != 0x02:
            raise WebAuthnError("署名形式が不正です")
        size, pos = length(buf, pos + 1)
        raw = buf[pos:pos + size]
        if size == 0 or len(raw) != size or raw[0] & 0x80:
            raise WebAuthnError("署名形式が不正です")
        if size > 1 and raw[0] == 0 and not raw[1] & 0x80:
            raise WebAuthnError("署名形式が不正です")           # non-minimal
        return int.from_bytes(raw, "big"), pos + size

    if not isinstance(sig, bytes) or len(sig) < 8 or len(sig) > 80 or sig[0] != 0x30:
        raise WebAuthnError("署名形式が不正です")
    total, pos = length(sig, 1)
    if pos + total != len(sig):
        raise WebAuthnError("署名形式が不正です")
    r, pos = integer(sig, pos)
    s, pos = integer(sig, pos)
    if pos != len(sig):
        raise WebAuthnError("署名形式が不正です")
    return r, s


# ---- RSASSA-PKCS1-v1_5 / SHA-256 -------------------------------------------------------------
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_pkcs1_sha256_verify(n, e, message, signature):
    if not (isinstance(n, int) and isinstance(e, int) and isinstance(signature, bytes)):
        return False
    if n < (1 << (MIN_RSA_BITS - 1)) or n.bit_length() > MAX_RSA_BITS or n % 2 == 0:
        return False
    if e < 3 or e % 2 == 0 or e.bit_length() > 32:
        return False
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= n:
        return False
    encoded = pow(value, e, n).to_bytes(size, "big")
    tail = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if size < len(tail) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    return hmac.compare_digest(encoded, expected)


# ---- COSE keys -------------------------------------------------------------------------------
def parse_cose_key(cose):
    """Validate a COSE_Key (dict) and return {"alg", ...}. Raises WebAuthnError."""
    if not isinstance(cose, dict):
        raise WebAuthnError("公開鍵の形式が不正です")
    kty, alg = cose.get(1), cose.get(3)
    if kty == 2 and alg == ALG_ES256:
        x, y = cose.get(-2), cose.get(-3)
        if cose.get(-1) != 1 or type(x) is not bytes or type(y) is not bytes or len(x) != 32 or len(y) != 32:
            raise WebAuthnError("公開鍵の形式が不正です")
        xi, yi = int.from_bytes(x, "big"), int.from_bytes(y, "big")
        if not on_curve(xi, yi):
            raise WebAuthnError("公開鍵が曲線上にありません")
        return {"alg": ALG_ES256, "x": xi, "y": yi}
    if kty == 3 and alg == ALG_RS256:
        n, e = cose.get(-1), cose.get(-2)
        if type(n) is not bytes or type(e) is not bytes or not n or not e or len(e) > 4 or n[0] == 0:
            raise WebAuthnError("公開鍵の形式が不正です")
        ni, ei = int.from_bytes(n, "big"), int.from_bytes(e, "big")
        if ni.bit_length() < MIN_RSA_BITS or ni.bit_length() > MAX_RSA_BITS or ni % 2 == 0 or ei < 3 or ei % 2 == 0:
            raise WebAuthnError("RSA公開鍵が弱いか不正です")
        return {"alg": ALG_RS256, "n": ni, "e": ei}
    raise WebAuthnError("対応していない公開鍵の種類です")


def verify_signature(cose_bytes, message, signature):
    """Verify `signature` over `message` with the stored COSE public key; False on any mismatch."""
    try:
        key = parse_cose_key(cbor_decode(cose_bytes))
        if key["alg"] == ALG_ES256:
            r, s = parse_der_signature(signature)
            return ecdsa_p256_verify(key["x"], key["y"], message, r, s)
        return rsa_pkcs1_sha256_verify(key["n"], key["e"], message, signature)
    except WebAuthnError:
        return False


# ---- authenticator data ----------------------------------------------------------------------
def parse_authenticator_data(data, attested):
    """Parse authData. With attested=True the AT block (credential id + COSE key) is required."""
    if not isinstance(data, bytes) or len(data) < 37 or len(data) > 4096:
        raise WebAuthnError("認証データが不正です")
    result = {"rp_id_hash": data[:32], "flags": data[32], "sign_count": int.from_bytes(data[33:37], "big")}
    flags = result["flags"]
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise WebAuthnError("認証データのフラグが不正です")
    pos = 37
    if flags & FLAG_AT:
        if not attested:
            raise WebAuthnError("認証データのフラグが不正です")
        if len(data) < pos + 18:
            raise WebAuthnError("認証データが不正です")
        result["aaguid"] = data[pos:pos + 16]
        size = int.from_bytes(data[pos + 16:pos + 18], "big")
        pos += 18
        if not 1 <= size <= CREDENTIAL_ID_MAX or len(data) < pos + size:
            raise WebAuthnError("資格情報IDが不正です")
        result["credential_id"] = data[pos:pos + size]
        pos += size
        key, used = cbor_decode_prefix(data[pos:])
        result["cose_key"] = data[pos:pos + used]
        result["cose"] = key
        pos += used
    elif attested:
        raise WebAuthnError("認証データに資格情報がありません")
    if flags & FLAG_ED:
        extensions, used = cbor_decode_prefix(data[pos:])
        if not isinstance(extensions, dict):
            raise WebAuthnError("認証データの拡張が不正です")
        pos += used
    if pos != len(data):
        raise WebAuthnError("認証データの後ろに余分なデータがあります")
    return result


# ---- challenges ------------------------------------------------------------------------------
class Challenges:
    """Single-use server challenges, kept in RAM only, bound to a kind, a session and an origin."""

    def __init__(self, ttl=CHALLENGE_TTL, limit=CHALLENGE_LIMIT, clock=time.time):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self._items = {}
        self._lock = threading.Lock()

    def issue(self, kind, bind, rp_id, origin):
        value = b64url_encode(secrets.token_bytes(CHALLENGE_BYTES))
        now = self.clock()
        with self._lock:
            for key in [k for k, v in self._items.items() if v[3] <= now]:
                del self._items[key]
            while len(self._items) >= self.limit:
                del self._items[min(self._items, key=lambda k: self._items[k][3])]
            self._items[value] = (kind, bind, (rp_id, origin), now + self.ttl)
        return value

    def consume(self, value, kind, bind, rp_id, origin):
        """Burn the challenge (even when the other checks fail) and verify what it was issued for."""
        with self._lock:
            entry = self._items.pop(value, None) if isinstance(value, str) else None
        if entry is None or entry[3] <= self.clock():
            raise WebAuthnError("チャレンジが無効か期限切れです。もう一度お試しください")
        if (entry[0] != kind or not hmac.compare_digest(entry[1].encode(), bind.encode()) or
                entry[2] != (rp_id, origin)):
            raise WebAuthnError("チャレンジが一致しません")

    def pending(self):
        with self._lock:
            return len(self._items)


def parse_client_data(raw, expected_type, origin):
    """Parse clientDataJSON; returns the challenge string. Checks type and origin exactly."""
    if not isinstance(raw, bytes) or not 2 <= len(raw) <= 4096:
        raise WebAuthnError("clientDataJSONが不正です")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebAuthnError("clientDataJSONが不正です")
    if not isinstance(data, dict):
        raise WebAuthnError("clientDataJSONが不正です")
    if data.get("type") != expected_type:
        raise WebAuthnError("操作の種類が一致しません")
    if data.get("origin") != origin:
        raise WebAuthnError("オリジンが一致しません")
    if data.get("crossOrigin") not in (None, False) or "topOrigin" in data:
        raise WebAuthnError("別オリジンのフレームからは使えません")
    challenge = data.get("challenge")
    if not isinstance(challenge, str):
        raise WebAuthnError("チャレンジがありません")
    return challenge


def rp_id_hash(rp_id):
    return hashlib.sha256(rp_id.encode("ascii")).digest()


def check_flags(parsed, rp_id):
    if not hmac.compare_digest(parsed["rp_id_hash"], rp_id_hash(rp_id)):
        raise WebAuthnError("RP IDが一致しません")
    if not parsed["flags"] & FLAG_UP or not parsed["flags"] & FLAG_UV:
        raise WebAuthnError("顔認証/指紋認証(ユーザー検証)が行われていません")


def verify_registration(challenges, bind, rp_id, origin, client_data_json, attestation_object, credential_id):
    """Check a navigator.credentials.create() result; returns the data to store.

    The attestation statement is ignored whatever its format (only fmt "none" must be empty).
    """
    challenge = parse_client_data(client_data_json, "webauthn.create", origin)
    challenges.consume(challenge, "create", bind, rp_id, origin)
    attestation = cbor_decode(attestation_object)
    if not isinstance(attestation, dict) or not isinstance(attestation.get("fmt"), str) or \
            not isinstance(attestation.get("attStmt"), dict) or type(attestation.get("authData")) is not bytes:
        raise WebAuthnError("attestationObjectが不正です")
    if attestation["fmt"] == "none" and attestation["attStmt"]:
        raise WebAuthnError("attestationObjectが不正です")
    parsed = parse_authenticator_data(attestation["authData"], attested=True)
    check_flags(parsed, rp_id)
    if not hmac.compare_digest(parsed["credential_id"], credential_id):
        raise WebAuthnError("資格情報IDが一致しません")
    key = parse_cose_key(parsed["cose"])
    return {"credential_id": parsed["credential_id"], "public_key": parsed["cose_key"], "alg": key["alg"],
            "sign_count": parsed["sign_count"], "backup_eligible": bool(parsed["flags"] & FLAG_BE)}


def verify_assertion(challenges, bind, rp_id, origin, credential, client_data_json, authenticator_data, signature):
    """Check a navigator.credentials.get() result against a stored credential.

    `credential` has "public_key" (COSE bytes) and "sign_count". Returns the new sign count.
    """
    challenge = parse_client_data(client_data_json, "webauthn.get", origin)
    challenges.consume(challenge, "get", bind, rp_id, origin)
    parsed = parse_authenticator_data(authenticator_data, attested=False)
    check_flags(parsed, rp_id)
    message = authenticator_data + hashlib.sha256(client_data_json).digest()
    if not verify_signature(credential["public_key"], message, signature):
        raise WebAuthnError("署名を確認できません")
    stored, new = credential["sign_count"], parsed["sign_count"]
    if stored and new and new <= stored:
        raise SignCountError("署名カウンタが増えていません(複製された認証器の可能性)")
    return max(stored, new)
