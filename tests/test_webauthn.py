"""webauthn.py: CBOR, P-256 / RSA verification with known vectors, registration and assertion checks."""
import hashlib
import random
import unittest
from unittest.mock import patch

import webauthn
import webauthn_testkit as kit

RP = "nas.tail1234.ts.net"
ORIGIN = "https://" + RP

# RFC 6979 appendix A.2.5 (P-256, SHA-256)
RFC_PRIV = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
RFC_X = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
RFC_Y = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
RFC_VECTORS = (
    (b"sample", 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
     0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
    (b"test", 0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
     0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
)
# Generated once with the OpenSSL CLI (`openssl dgst -sha256 -sign`), message b"hello passkey".
OPENSSL_EC = (0x47e4db79990ef9ae0bc74c8dfdcef094081e98fa171a67b94966e7d36c0f68f1,
              0x9f65c09ce2d1d719c4806469af4509ef4e16b31d50af2ab0ece9a010297d3c4a,
              bytes.fromhex("30450220529cd4f6de0b8ec6bbc26955a6b565ac91fc996f98ce023c10e99213868ed995022100"
                            "d67a54f1024cfb0b6053a784620a6e97e346246c42a5574cdbb12fb5b7793a5e"))
OPENSSL_RSA_N = int(
    "be2571eb5f629c6de3113a8622e5670a37e43d61a99a6bfb5b13f81bd9fbccb9868402643ee046ed89b7d5949b0091b75bb3460a1d29d43b"
    "7666bbe28c2d1c1ff20cc62bdead67d03a08ee8e0199252e6b1d653e171278fa9174c63d25a7d105b64ab7e9c6e93ccaf4c8ef7d1e33c04d"
    "716a32ff14a5dc5393ba3fe9d2f816bb333a5f39f58b475fcbb702bd1d0c244cbac9c2ffeb3978597b45716347012e92033998989aa4665c"
    "b43c7ad074475c92a7c948aa00e378e85f006a2ad4449a9640d5bcb6ed9aab8d36cc92cff3e7e80ebc0d39a5334e679131e593878fdcfe78"
    "5cec93835e71f3c5a11f88500ccc8b45620cfd429515970addb70d41b969e73f", 16)
OPENSSL_RSA_SIG = bytes.fromhex(
    "adad08040e9d83ebb628bcf0e27b00d1e1fcaa7bc31e6a0b1ff317f56f6552445aa2cf93add94817657ab83cf04bd3c45c6172673ba64dd"
    "d7b26471cbb2f0dc5a3aa3bb65720e28daca9fbb4d861ad77ae91f805c4d9088f166c6c2d5076fb13c760c7817c7c3a3feedfcfe80d1f39d"
    "102eb915cdb43fb1b24aef98ff7523c26b29132c2a944cb5bbee45025810eac932083f0e39d5cb8f2e537aea60d24bb98ef14baee76368bb"
    "85219e7c5f941731757627bff08dd4657b222332a7100ebe55207f6314129f4e9400d513c5a97bb8f2820569480189791e72dfeb5c0aac46"
    "9b23ddfe473a4b2c475cc64b513ce3d6934aee2982528743fbfe30ae1b0da0dd9")


class CborTests(unittest.TestCase):
    def test_roundtrip_of_supported_types(self):
        value = {"a": [1, -1, 24, 255, 65536, -1000, True, False, None, b"\x00\xff", "日本"], 7: {-3: b""}}
        self.assertEqual(webauthn.cbor_decode(kit.cbor(value)), value)

    def test_rfc8949_examples(self):
        self.assertEqual(webauthn.cbor_decode(bytes.fromhex("1903e8")), 1000)
        self.assertEqual(webauthn.cbor_decode(bytes.fromhex("3863")), -100)
        self.assertEqual(webauthn.cbor_decode(bytes.fromhex("a201020304")), {1: 2, 3: 4})
        self.assertEqual(webauthn.cbor_decode(bytes.fromhex("83010203")), [1, 2, 3])

    def test_rejects_unsupported_and_malformed(self):
        bad = {
            "indefinite array": "9f01ff", "indefinite bytes": "5fff", "tag": "c11a514b67b0", "float": "f93c00",
            "undefined": "f7", "reserved info": "1c", "truncated int": "19", "truncated bytes": "4401",
            "bytes length beyond data": "5b00000000ffffffff00", "array count beyond data": "9a0000ffff01",
            "map count beyond data": "ba0000ffff01", "map with bytes key": "a14101 02".replace(" ", ""),
            "duplicate key": "a201020103", "bad utf8": "62c328", "trailing": "0100", "empty": "",
        }
        for name, hexed in bad.items():
            with self.subTest(name):
                with self.assertRaises(webauthn.WebAuthnError):
                    webauthn.cbor_decode(bytes.fromhex(hexed))

    def test_depth_item_and_size_caps(self):
        deep = b"\x81" * (webauthn.CBOR_MAX_DEPTH + 2) + b"\x00"
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.cbor_decode(deep)
        shallow = b"\x81" * webauthn.CBOR_MAX_DEPTH + b"\x00"
        webauthn.cbor_decode(shallow)
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.cbor_decode(b"\x99\x04\x01" + b"\x00" * 1025)             # 1025 items
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.cbor_decode(b"\x00" * (webauthn.CBOR_MAX_BYTES + 1))

    def test_random_garbage_never_raises_anything_else(self):
        rng = random.Random(1153)
        valid = kit.cbor({"fmt": "none", "attStmt": {}, "authData": bytes(40)})
        for index in range(3000):
            if index % 2:
                data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 40)))
            else:       # mutate a valid document
                data = bytearray(valid)
                for _ in range(rng.randint(1, 4)):
                    data[rng.randrange(len(data))] = rng.getrandbits(8)
                data = bytes(data[:rng.randint(1, len(data))])
            try:
                webauthn.cbor_decode(data)
            except webauthn.WebAuthnError:
                pass


class Base64Tests(unittest.TestCase):
    def test_strict(self):
        self.assertEqual(webauthn.b64url_decode("aGVsbG8"), b"hello")
        self.assertEqual(webauthn.b64url_encode(b"\xff\xfe"), "__4")
        for bad in ("aGVsbG8=", "aGV sbG8", "a+b/", "a", "aGVsbG8\n", 5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(webauthn.WebAuthnError):
                    webauthn.b64url_decode(bad)
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.b64url_decode("AAAA" * 10, max_bytes=5)


class EcdsaTests(unittest.TestCase):
    def test_rfc6979_vectors_verify(self):
        for message, r, s in RFC_VECTORS:
            with self.subTest(message=message):
                self.assertTrue(webauthn.ecdsa_p256_verify(RFC_X, RFC_Y, message, r, s))

    def test_testkit_signer_reproduces_rfc6979(self):
        self.assertEqual(kit.public_key(RFC_PRIV), (RFC_X, RFC_Y))
        for message, r, s in RFC_VECTORS:
            sr, ss = kit.ecdsa_sign(RFC_PRIV, message)
            self.assertEqual(sr, r)
            self.assertIn(ss, (s, webauthn.N - s))     # the kit normalizes to low-s

    def test_openssl_vector(self):
        x, y, der = OPENSSL_EC
        r, s = webauthn.parse_der_signature(der)
        self.assertTrue(webauthn.ecdsa_p256_verify(x, y, b"hello passkey", r, s))
        self.assertFalse(webauthn.ecdsa_p256_verify(x, y, b"hello passkeY", r, s))

    def test_negative_cases(self):
        message, r, s = RFC_VECTORS[0]
        n = webauthn.N
        cases = {
            "tampered message": (RFC_X, RFC_Y, b"sample!", r, s),
            "r flipped": (RFC_X, RFC_Y, message, r ^ 1, s),
            "s flipped": (RFC_X, RFC_Y, message, r, s ^ 1),
            "s zero": (RFC_X, RFC_Y, message, r, 0),
            "r zero": (RFC_X, RFC_Y, message, 0, s),
            "r equals n": (RFC_X, RFC_Y, message, n, s),
            "r above n": (RFC_X, RFC_Y, message, r + n, s),
            "s equals n": (RFC_X, RFC_Y, message, r, n),
            "s above n": (RFC_X, RFC_Y, message, r, s + n),
            "negative r": (RFC_X, RFC_Y, message, -r, s),
            "off-curve key": (RFC_X, RFC_Y ^ 1, message, r, s),
            "wrong key": (kit.public_key(5)[0], kit.public_key(5)[1], message, r, s),
            "coordinate >= p": (RFC_X + webauthn.P, RFC_Y, message, r, s),
            "infinity-ish zero key": (0, 0, message, r, s),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assertFalse(webauthn.ecdsa_p256_verify(*args))

    def test_curve_constants_and_point_math(self):
        self.assertTrue(webauthn.on_curve(webauthn.GX, webauthn.GY))
        # n*G is the point at infinity, (n-1)*G = -G, 2G matches the independent affine implementation
        self.assertIsNone(kit.scalar_mult(webauthn.N))
        self.assertEqual(kit.scalar_mult(webauthn.N - 1), (webauthn.GX, webauthn.P - webauthn.GY))
        doubled = webauthn._to_affine(webauthn._double((webauthn.GX, webauthn.GY, 1)))
        self.assertEqual(doubled, kit.scalar_mult(2))
        for k1, k2 in ((3, 5), (webauthn.N - 1, 2), (123456789, 987654321)):
            fast = webauthn._multi_mul(k1, (webauthn.GX, webauthn.GY), k2, kit.scalar_mult(7))
            slow = kit.point_add(kit.scalar_mult(k1), kit.scalar_mult(k2 * 7 % webauthn.N))
            self.assertEqual(fast, slow)

    def test_random_signatures_verify_and_cross_key_fails(self):
        rng = random.Random(7)
        for _ in range(4):
            private = rng.randrange(1, webauthn.N)
            x, y = kit.public_key(private)
            message = bytes(rng.getrandbits(8) for _ in range(40))
            r, s = kit.ecdsa_sign(private, message)
            self.assertTrue(webauthn.ecdsa_p256_verify(x, y, message, r, s))
            self.assertFalse(webauthn.ecdsa_p256_verify(RFC_X, RFC_Y, message, r, s))

    def test_der_parser_is_strict(self):
        good = kit.der_signature(0x1234, 0x5678)
        self.assertEqual(webauthn.parse_der_signature(good), (0x1234, 0x5678))
        # r == 0 is well-formed DER; the range check in ecdsa_p256_verify rejects it
        self.assertEqual(webauthn.parse_der_signature(bytes.fromhex("3006020100020101")), (0, 1))
        bad = {"trailing byte": good + b"\x00", "truncated": good[:-1], "empty": b"", "wrong tag": b"\x31" + good[1:],
               "non-minimal zero": bytes.fromhex("30080202000102020102"), "negative": bytes.fromhex("30060201ff020101"),
               "indefinite": bytes.fromhex("30800201010201020000"), "long form short length": bytes.fromhex("30810602010102010"
                                                                                                           "2"),
               "zero length integer": bytes.fromhex("3004020002 00".replace(" ", "")), "not bytes": "30"}
        for name, item in bad.items():
            with self.subTest(name):
                with self.assertRaises(webauthn.WebAuthnError):
                    webauthn.parse_der_signature(item)


class RsaTests(unittest.TestCase):
    def test_openssl_2048_vector(self):
        self.assertTrue(webauthn.rsa_pkcs1_sha256_verify(OPENSSL_RSA_N, 65537, b"hello passkey", OPENSSL_RSA_SIG))
        self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(OPENSSL_RSA_N, 65537, b"hello passkeY", OPENSSL_RSA_SIG))
        flipped = bytearray(OPENSSL_RSA_SIG)
        flipped[-1] ^= 1
        self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(OPENSSL_RSA_N, 65537, b"hello passkey", bytes(flipped)))
        self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(OPENSSL_RSA_N, 65537, b"hello passkey", OPENSSL_RSA_SIG[1:]))

    def test_small_fixed_key_and_padding_attacks(self):
        with patch.object(webauthn, "MIN_RSA_BITS", 512):
            message = b"message"
            signature = kit.rsa_sign(message)
            self.assertTrue(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, kit.RSA_E, message, signature))
            self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, kit.RSA_E, b"other", signature))
            self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, kit.RSA_E, message, signature + b"\x00"))
            self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, 3, message, signature))
            # signature value >= n
            self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(
                kit.RSA_N, kit.RSA_E, message, (kit.RSA_N + 5).to_bytes(64, "big")))
            # wrong padding byte (not all 0xff) and trailing garbage after the digest are rejected
            size = 64
            tail = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
            for em in (b"\x00\x01" + b"\xfe" + b"\xff" * (size - len(tail) - 4) + b"\x00" + tail,
                       b"\x00\x02" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail,
                       b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x01" + tail):
                forged = pow(int.from_bytes(em, "big"), kit.RSA_D, kit.RSA_N).to_bytes(size, "big")
                self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, kit.RSA_E, message, forged))

    def test_weak_keys_rejected_by_default(self):
        signature = kit.rsa_sign(b"m")
        self.assertFalse(webauthn.rsa_pkcs1_sha256_verify(kit.RSA_N, kit.RSA_E, b"m", signature))
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.parse_cose_key(webauthn.cbor_decode(kit.cose_rsa()))


class CoseAndAuthDataTests(unittest.TestCase):
    def test_cose_ec2_validation(self):
        x, y = kit.public_key(11)
        good = {1: 2, 3: -7, -1: 1, -2: x.to_bytes(32, "big"), -3: y.to_bytes(32, "big")}
        self.assertEqual(webauthn.parse_cose_key(good)["alg"], -7)
        bad = [None, {**good, 3: -8}, {**good, -1: 2}, {**good, -2: b"\x01" * 31},
               {**good, -3: ((y ^ 1).to_bytes(32, "big"))}, {**good, 1: 4}, [], None, {**good, -2: "x"},
               {**good, -2: bytes(32), -3: bytes(32)}]
        for item in bad:
            with self.subTest(item=str(item)[:40]):
                with self.assertRaises(webauthn.WebAuthnError):
                    webauthn.parse_cose_key(item)

    def test_authenticator_data_parsing(self):
        auth = kit.Authenticator()
        data = auth.auth_data(RP, 0x45, attested=True, counter=9)
        parsed = webauthn.parse_authenticator_data(data, attested=True)
        self.assertEqual((parsed["sign_count"], parsed["credential_id"]), (9, auth.credential_id))
        for name, value, attested in (
                ("short", data[:36], True), ("AT missing when required", auth.auth_data(RP, 0x05), True),
                ("AT present when not allowed", data, False), ("trailing", data + b"\x00", True),
                ("BS without BE", auth.auth_data(RP, 0x15, attested=True), True),
                ("empty credential id", data[:37 + 16] + b"\x00\x00" + data[37 + 18:], True),
                ("credential id too long", data[:37 + 16] + (1024).to_bytes(2, "big") + data[37 + 18:], True),
                ("ED flag without extension", auth.auth_data(RP, 0x85, attested=True), True)):
            with self.subTest(name):
                with self.assertRaises(webauthn.WebAuthnError):
                    webauthn.parse_authenticator_data(value, attested)
        with_ext = auth.auth_data(RP, 0xC5, attested=True, extra=kit.cbor({"credProps": {"rk": True}}))
        self.assertTrue(webauthn.parse_authenticator_data(with_ext, attested=True)["credential_id"])
        plain = auth.auth_data(RP, 0x05, counter=3)
        self.assertEqual(webauthn.parse_authenticator_data(plain, attested=False)["sign_count"], 3)


class ChallengeTests(unittest.TestCase):
    def test_single_use_ttl_binding(self):
        now = [1000.0]
        c = webauthn.Challenges(ttl=120, limit=4, clock=lambda: now[0])
        value = c.issue("get", "", RP, ORIGIN)
        self.assertEqual(len(webauthn.b64url_decode(value)), 32)
        c.consume(value, "get", "", RP, ORIGIN)
        with self.assertRaises(webauthn.WebAuthnError):
            c.consume(value, "get", "", RP, ORIGIN)                      # single use
        value = c.issue("create", "sess", RP, ORIGIN)
        now[0] += 121
        with self.assertRaises(webauthn.WebAuthnError):
            c.consume(value, "create", "sess", RP, ORIGIN)               # expired
        for kind, bind, rp, origin in (("get", "sess", RP, ORIGIN), ("create", "other", RP, ORIGIN),
                                       ("create", "sess", "evil.ts.net", ORIGIN),
                                       ("create", "sess", RP, "https://evil.ts.net")):
            value = c.issue("create", "sess", RP, ORIGIN)
            with self.subTest(kind=kind, bind=bind, rp=rp):
                with self.assertRaises(webauthn.WebAuthnError):
                    c.consume(value, kind, bind, rp, origin)
                with self.assertRaises(webauthn.WebAuthnError):          # burned by the failed attempt
                    c.consume(value, "create", "sess", RP, ORIGIN)
        self.assertEqual(len({c.issue("get", "", RP, ORIGIN) for _ in range(10)}), 10)
        self.assertLessEqual(c.pending(), 4)
        with self.assertRaises(webauthn.WebAuthnError):
            c.consume(None, "get", "", RP, ORIGIN)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.auth = kit.Authenticator()
        self.challenges = webauthn.Challenges()

    def decode(self, response):
        return {k: webauthn.b64url_decode(v) for k, v in response.items()
                if k in ("clientDataJSON", "attestationObject", "authenticatorData", "signature", "id")}

    def register(self, auth=None, **kwargs):
        auth = auth or self.auth
        options = {"challenge": self.challenges.issue("create", "s1", RP, ORIGIN), "rp": {"id": RP}}
        fields = self.decode(auth.create(options, kwargs.pop("origin", ORIGIN), **kwargs))
        return webauthn.verify_registration(self.challenges, "s1", RP, ORIGIN, fields["clientDataJSON"],
                                            fields["attestationObject"], fields["id"])

    def test_registration_es256_and_rs256(self):
        result = self.register()
        self.assertEqual((result["alg"], result["credential_id"], result["public_key"]),
                         (-7, self.auth.credential_id, self.auth.cose))
        with patch.object(webauthn, "MIN_RSA_BITS", 512):
            rs = kit.Authenticator("rs256", credential_id=b"rsa-credential-0123456789abcdef")
            self.assertEqual(self.register(rs)["alg"], -257)

    def test_registration_rejections(self):
        def tamper_challenge(cd, att):
            return cd.replace(b'"challenge":"', b'"challenge":"A'), att

        def tamper_type(cd, att):
            return cd.replace(b"webauthn.create", b"webauthn.get"), att

        def cross(cd, att):
            return cd.replace(b'"crossOrigin":false', b'"crossOrigin":true'), att

        cases = {
            "wrong origin": dict(origin="https://evil.ts.net"),
            "wrong rp id hash": dict(rp_id="other.ts.net"),
            "no user presence": dict(flags=0x04),
            "no user verification": dict(flags=0x01),
            "challenge changed": dict(tamper=tamper_challenge),
            "type get": dict(tamper=tamper_type),
            "cross origin": dict(tamper=cross),
            "none with statement": dict(att_stmt={"sig": b"x"}),
            "garbage attestation": dict(tamper=lambda cd, att: (cd, att[:-3])),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                with self.assertRaises(webauthn.WebAuthnError):
                    self.register(**kwargs)

    def test_other_attestation_formats_are_ignored_not_trusted(self):
        result = self.register(fmt="packed", att_stmt={"alg": -7, "sig": b"not checked"})
        self.assertEqual(result["alg"], -7)

    def test_credential_id_must_match_authdata(self):
        options = {"challenge": self.challenges.issue("create", "s1", RP, ORIGIN), "rp": {"id": RP}}
        fields = self.decode(self.auth.create(options, ORIGIN))
        with self.assertRaises(webauthn.WebAuthnError):
            webauthn.verify_registration(self.challenges, "s1", RP, ORIGIN, fields["clientDataJSON"],
                                         fields["attestationObject"], b"another-id")

    def assertion(self, auth, credential, **kwargs):
        options = {"challenge": self.challenges.issue("get", "", RP, ORIGIN), "rpId": RP}
        fields = self.decode(auth.get(options, kwargs.pop("origin", ORIGIN), **kwargs))
        return webauthn.verify_assertion(self.challenges, "", RP, ORIGIN, credential, fields["clientDataJSON"],
                                         fields["authenticatorData"], fields["signature"])

    def test_assertion_es256_and_counter(self):
        credential = {"public_key": self.auth.cose, "sign_count": 0}
        self.assertEqual(self.assertion(self.auth, credential), 1)
        credential["sign_count"] = 1
        self.assertEqual(self.assertion(self.auth, credential), 2)
        credential["sign_count"] = 2
        with self.assertRaises(webauthn.SignCountError):                  # replayed / cloned counter
            self.assertion(self.auth, credential, counter=2)
        with self.assertRaises(webauthn.SignCountError):
            self.assertion(self.auth, credential, counter=1)
        credential["sign_count"] = 5
        self.assertEqual(self.assertion(self.auth, credential, counter=0), 5)   # counters not supported: allowed
        credential["sign_count"] = 0
        self.assertEqual(self.assertion(self.auth, credential, counter=7), 7)

    def test_assertion_rs256(self):
        with patch.object(webauthn, "MIN_RSA_BITS", 512):
            rs = kit.Authenticator("rs256")
            self.assertEqual(self.assertion(rs, {"public_key": rs.cose, "sign_count": 0}), 1)

    def test_assertion_rejections(self):
        credential = {"public_key": self.auth.cose, "sign_count": 0}
        other = kit.Authenticator(private=424242)
        cases = {
            "wrong origin": (self.auth, dict(origin="https://evil.ts.net")),
            "wrong rp": (self.auth, dict(rp_id="evil.ts.net")),
            "no uv": (self.auth, dict(flags=0x01)),
            "no up": (self.auth, dict(flags=0x04)),
            "registration type": (self.auth, dict(kind="webauthn.create")),
            "other key signs": (other, {}),
        }
        for name, (auth, kwargs) in cases.items():
            with self.subTest(name):
                with self.assertRaises(webauthn.WebAuthnError):
                    self.assertion(auth, credential, **kwargs)

    def test_assertion_tampering_and_replay(self):
        credential = {"public_key": self.auth.cose, "sign_count": 0}
        options = {"challenge": self.challenges.issue("get", "", RP, ORIGIN), "rpId": RP}
        fields = self.decode(self.auth.get(options, ORIGIN))

        def run(**changes):
            f = dict(fields, **changes)
            return webauthn.verify_assertion(self.challenges, "", RP, ORIGIN, credential, f["clientDataJSON"],
                                             f["authenticatorData"], f["signature"])
        flipped = bytearray(fields["authenticatorData"])
        flipped[36] ^= 1
        with self.assertRaises(webauthn.WebAuthnError):
            run(authenticatorData=bytes(flipped))
        # the failed attempt burned the challenge: even the untouched response is now refused
        with self.assertRaises(webauthn.WebAuthnError):
            run()


if __name__ == "__main__":
    unittest.main()
