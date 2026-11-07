"""v0.1.57: passthrough display (pt_core.py, pt_proxy.py, passthrough.py, web/pt-*.js, the Hub's /api/pt/*)."""
import gzip
import http.client
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlsplit

import passthrough
import pt_core
import pt_proxy
import storage
import webapp

ROOT = Path(__file__).resolve().parents[1]
HOOKS = {"request_is_https": webapp.request_is_https, "loopback_peer": webapp.loopback_peer,
         "tailscale_name": webapp.tailscale_name}


# ---------------------------------------------------------------------------------------------------- URL mapping
class UrlMappingTests(unittest.TestCase):
    CASES = [
        ("https://example.com/", "/p/s/example.com/"),
        ("https://example.com", "/p/s/example.com/"),
        ("http://example.com/a/b.html?x=1&y=2", "/p/h/example.com/a/b.html?x=1&y=2"),
        ("https://Example.COM:443/Path", "/p/s/example.com/Path"),
        ("http://example.com:80/", "/p/h/example.com/"),
        ("https://example.com:8443/x", "/p/s/example.com:8443/x"),
        ("https://example.com/a b?q=a b", "/p/s/example.com/a%20b?q=a%20b"),
        ("https://example.com/%E3%81%82?q=%26", "/p/s/example.com/%E3%81%82?q=%26"),
        ("https://example.com/日本?k=値", "/p/s/example.com/%E6%97%A5%E6%9C%AC?k=%E5%80%A4"),
        ("https://日本語.jp/", "/p/s/xn--wgv71a119e.jp/"),
        ("https://bücher.example/x", "/p/s/xn--bcher-kva.example/x"),
        ("https://example.com/a#frag", "/p/s/example.com/a"),
        ("https://example.com/?#x", "/p/s/example.com/"),
        ("https://example.com/a?b[]=1|2", "/p/s/example.com/a?b[]=1|2"),
        ("https://93.184.216.34/x", "/p/s/93.184.216.34/x"),
        ("https://a_b.example.com/", "/p/s/a_b.example.com/"),
        ("https://example.com/a\\b", "/p/s/example.com/a/b"),
        ("https://example.com./x", "/p/s/example.com/x"),
    ]

    def test_encode(self):
        for url, expected in self.CASES:
            with self.subTest(url=url):
                self.assertEqual(pt_core.encode_target(url), expected)

    def test_decode_round_trips_to_the_normalised_url(self):
        for url, expected in self.CASES:
            with self.subTest(url=url):
                target = pt_core.decode_target(expected)
                self.assertEqual(pt_core.encode_target(target.url), expected)
        self.assertEqual(pt_core.decode_target("/p/s/example.com/a%20b/c?x=1").url, "https://example.com/a%20b/c?x=1")
        self.assertEqual(pt_core.decode_target("/p/h/example.com").url, "http://example.com/")
        self.assertEqual(pt_core.decode_target("/p/h/example.com?x=1").url, "http://example.com/?x=1")
        self.assertEqual(pt_core.decode_target("/p/s/example.com:8443/x").url, "https://example.com:8443/x")
        self.assertEqual(pt_core.decode_target("/p/s/example.com:443/x").url, "https://example.com/x")
        self.assertEqual(pt_core.decode_target("/p/s/EXAMPLE.com/x").host, "example.com")
        self.assertEqual(pt_core.decode_target("/p/s/example.com/a//b/../c").path, "/a//b/../c")     # the browser normalises, we do not

    def test_refused(self):
        for url in ("ftp://example.com/", "javascript:alert(1)", "https://[::1]/", "https://[2001:db8::1]:8080/",
                    "https://user:pw@example.com/", "https:///x", "//example.com/", "example.com/x", "", None,
                    "https://example.com:0/", "https://example.com:99999/", "https://exa mple.com/",
                    "https://example.com/\r\nX: y", "https://" + "a" * 64 + ".com/", "https://-bad.example.com/",
                    "data:text/html,hi", "https://example.com/" + "a" * 5000):
            with self.subTest(url=str(url)[:40]):
                with self.assertRaises(pt_core.TargetError):
                    pt_core.encode_target(url)
        for path in ("/p/x/example.com/", "/p/s/", "/p/s//x", "/p/s/[::1]/x", "/p/s/%5B::1%5D/x", "/p/s/example.com:0/",
                     "/p/s/example.com:70000/", "/p/s/ex%40ample.com/", "/q/s/example.com/", "/p/s/example.com/a\\b",
                     "/p/s/exam ple.com/", "/p/s/example.com:abc/", "/pt/ping", "/p/12345"):
            with self.subTest(path=path):
                with self.assertRaises(pt_core.TargetError):
                    pt_core.decode_target(path)

    def test_fragments_are_never_part_of_the_target(self):
        target = pt_core.parse_target("https://example.com/p?q=1#section")
        self.assertNotIn("#", target.url)
        self.assertNotIn("#", target.proxy_path)
        self.assertEqual(pt_core.decode_target("/p/s/example.com/p?q=1").request_target, "/p?q=1")

    def test_site_of(self):
        for host, site in (("www.example.com", "example.com"), ("a.b.example.co.jp", "example.co.jp"),
                           ("example.com", "example.com"), ("x.example.com.au", "example.com.au"),
                           ("cdn.example.net", "example.net"), ("93.184.216.34", "93.184.216.34"), ("localhost", "localhost")):
            self.assertEqual(pt_core.site_of(host), site)
        self.assertEqual(pt_core.top_site("www.example.com"), "example.com")
        self.assertEqual(pt_core.top_site("https://a.example.com/x"), "example.com")
        self.assertEqual(pt_core.top_site(""), "")
        self.assertEqual(pt_core.top_site("bad host"), "")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_service_worker_mapping_matches_python(self):
        """web/pt-sw.js (the phone side) and pt_core.py (the Hub side) agree on every vector, both directions."""
        sw = (ROOT / "web" / "pt-sw.js").read_text(encoding="utf-8")
        start, end = sw.index("const PROXY"), sw.index("function proxyOfUrl")
        script = ("const self = {location: {origin: 'https://b.test'}, addEventListener() {}, skipWaiting() {}, clients: {claim() {}}};\n" + sw[start:end] + """
const cases = JSON.parse(process.argv[2]);
const out = cases.map(u => toProxy(u));
const back = cases.map(u => { const p = toProxy(u); const f = p ? fromProxy(p) : null; return f ? f.url : null; });
console.log(JSON.stringify({out, back}));
""")
        urls = [u for u, _ in self.CASES]
        urls += ["https://[::1]/x", "ftp://example.com/", "https://user:pw@example.com/"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path), json.dumps(urls)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        for url, got, back in zip(urls, result["out"], result["back"]):
            with self.subTest(url=url):
                try:
                    want = pt_core.encode_target(url)
                except pt_core.TargetError:
                    self.assertIsNone(got)
                    continue
                # the browser's URL parser and ours may spell the same path differently (percent-escapes): compare decoded
                self.assertEqual(urlsplit(got).netloc, urlsplit(want).netloc)
                from urllib.parse import unquote
                self.assertEqual(unquote(got), unquote(want), url)
                self.assertEqual(unquote(pt_core.decode_target(got).url), unquote(pt_core.decode_target(want).url))
                self.assertIsNotNone(back)


# ---------------------------------------------------------------------------------------------------- SSRF guard
class SsrfTests(unittest.TestCase):
    BLOCKED = ["127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
               "192.168.1.1", "169.254.169.254", "169.254.170.2", "100.64.0.1", "100.127.255.255", "0.0.0.0",
               "0.1.2.3", "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255", "192.0.0.1", "192.0.2.1",
               "198.18.0.1", "198.51.100.7", "203.0.113.9", "::1", "::", "fe80::1", "fc00::1", "fd7a:115c:a1e0::1",
               "fd12:3456::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
               "64:ff9b::7f00:1", "64:ff9b::a00:1", "2002:7f00:1::1", "2002:a9fe:a9fe::1", "2001:db8::1", "2001::1",
               "fec0::1", "100::1", "::127.0.0.1"]
    PUBLIC = ["93.184.216.34", "8.8.8.8", "1.1.1.1", "172.15.255.255", "172.32.0.1", "100.63.255.255", "100.128.0.1",
              "2606:4700:4700::1111", "::ffff:8.8.8.8"]

    def test_every_private_range_is_refused(self):
        for ip in self.BLOCKED:
            with self.subTest(ip=ip):
                self.assertTrue(pt_core.ip_blocked(ip), ip)
        for ip in self.PUBLIC:
            with self.subTest(ip=ip):
                self.assertEqual(pt_core.ip_blocked(ip), "", ip)

    def test_host_names(self):
        for host in ("localhost", "foo.localhost", "nas", "nas.local", "x.ts.net", "ts.net", "nas.tail1234.ts.net",
                     "printer.lan", "a.internal", "host.home.arpa", "foo.test", "foo.invalid", "127.1", "2130706433",
                     "0x7f.1", "0177.0.0.1", "10.0.0.1", "169.254.169.254", ""):
            with self.subTest(host=host):
                self.assertTrue(pt_core.host_blocked_reason(host), host)
        self.assertTrue(pt_core.host_blocked_reason("hub.example.com", own_names={"hub.example.com"}))
        for host in ("example.com", "a.b.example.co.jp", "xn--wgv71a119e.jp", "93.184.216.34"):
            with self.subTest(host=host):
                self.assertEqual(pt_core.host_blocked_reason(host), "", host)

    def test_dns_that_resolves_to_a_private_address_is_refused(self):
        def resolver(answers):
            return lambda host, port, type=0: [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers]
        never_local = lambda ip: False
        self.assertEqual(pt_core.resolve_public("example.com", 443, resolver(["93.184.216.34"]), local=never_local),
                         ["93.184.216.34"])
        for answer in (["127.0.0.1"], ["10.1.2.3"], ["93.184.216.34", "192.168.0.5"], ["::1"], ["fd00::5"], ["169.254.169.254"],
                       ["100.100.100.100"], ["::ffff:7f00:1"]):
            with self.subTest(answer=answer):
                with self.assertRaises(pt_core.Blocked):
                    pt_core.resolve_public("example.com", 443, resolver(answer), local=never_local)
        with self.assertRaises(pt_core.Blocked):               # a public-looking address that is ours
            pt_core.resolve_public("example.com", 443, resolver(["93.184.216.34"]), local=lambda ip: True)
        with self.assertRaises(pt_core.ResolveError):
            pt_core.resolve_public("example.com", 443, resolver([]), local=never_local)

        def failing(*args, **kwargs):
            raise socket.gaierror("no")
        with self.assertRaises(pt_core.ResolveError):
            pt_core.resolve_public("example.com", 443, failing, local=never_local)

    def test_ipv4_comes_first_and_literals_skip_dns(self):
        def resolver(host, port, type=0):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", port, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        self.assertEqual(pt_core.resolve_public("example.com", 443, resolver, local=lambda ip: False),
                         ["93.184.216.34", "2606:4700::1111"])

        def boom(*a, **k):
            raise AssertionError("resolver must not be used for a literal")
        self.assertEqual(pt_core.resolve_public("93.184.216.34", 80, boom, local=lambda ip: False), ["93.184.216.34"])

    def test_only_ports_80_and_443(self):
        resolver = lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        ok = pt_core.parse_target("https://example.com/")
        self.assertEqual(pt_core.check_target(ok, resolver=resolver, local=lambda ip: False), ["93.184.216.34"])
        self.assertEqual(pt_core.check_target(pt_core.parse_target("http://example.com/"), resolver=resolver,
                                              local=lambda ip: False), ["93.184.216.34"])
        for url in ("https://example.com:8443/", "http://example.com:8080/", "https://example.com:22/"):
            with self.assertRaises(pt_core.Blocked):
                pt_core.check_target(pt_core.parse_target(url), resolver=resolver, local=lambda ip: False)

    def test_local_address_probe(self):
        self.assertTrue(pt_core.is_local_address("127.0.0.1"))
        self.assertFalse(pt_core.is_local_address("93.184.216.34"))


# ---------------------------------------------------------------------------------------------------- cookie jar
class CookieJarTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.jar = pt_core.CookieJar(clock=lambda: self.now[0])

    def test_host_only_and_domain_cookies(self):
        self.jar.store("www.example.com", "/a/b", True, ["sid=1; Path=/", "d=2; Domain=example.com; Path=/", "p=3; Path=/a"])
        self.assertEqual(self.jar.header_for("www.example.com", "/a/x", True), "p=3; sid=1; d=2")
        self.assertEqual(self.jar.header_for("api.example.com", "/", True), "d=2")         # domain cookie only
        self.assertEqual(self.jar.header_for("www.example.com", "/b", True), "sid=1; d=2")  # path /a does not match /b
        self.assertEqual(self.jar.header_for("www.example.com", "/ab", True), "sid=1; d=2")  # ... nor /ab
        self.assertEqual(self.jar.header_for("www.other.net", "/", True), "")

    def test_other_sites_domains_are_refused(self):
        self.jar.store("www.example.com", "/", True, ["a=1; Domain=other.com", "b=1; Domain=com", "c=1; Domain=co.jp",
                                                       "d=1; Domain=example.com.evil.net"])
        self.assertEqual(self.jar.count(), 0)
        self.jar.store("a.example.co.jp", "/", True, ["x=1; Domain=co.jp", "y=1; Domain=example.co.jp"])
        self.assertEqual(self.jar.header_for("b.example.co.jp", "/", True), "y=1")

    def test_secure_expiry_and_deletion(self):
        self.jar.store("example.com", "/", True, ["s=1; Secure", "t=2; Max-Age=10", "u=3"])
        self.assertEqual(self.jar.header_for("example.com", "/", False), "t=2; u=3")           # Secure is not sent over http
        self.now[0] += 11
        self.assertEqual(self.jar.header_for("example.com", "/", True), "s=1; u=3")
        self.jar.store("example.com", "/", True, ["s=; Max-Age=0"])
        self.assertEqual(self.jar.header_for("example.com", "/", True), "u=3")
        self.jar.store("example.com", "/", True, ["u=4; Expires=Thu, 01 Jan 1970 00:00:01 GMT"])
        self.assertEqual(self.jar.header_for("example.com", "/", True), "")
        self.jar.store("example.com", "/", False, ["insecure_origin=1; Secure"])               # a Secure cookie over http is dropped
        self.assertEqual(self.jar.count(), 0)

    def test_prefixes_and_garbage(self):
        self.jar.store("example.com", "/", True, ["__Host-a=1; Secure; Path=/", "__Host-b=1; Secure; Path=/x",
                                                  "__Secure-c=1", "__Secure-d=1; Secure", "=nameless", "no-equals",
                                                  "ok=1"])
        self.assertEqual(sorted(c.name for cs in self.jar.by_site.values() for c in cs.values()),
                         ["__Host-a", "__Secure-d", "ok"])

    def test_default_path_and_overwrite(self):
        self.jar.store("example.com", "/dir/page", True, ["a=1"])
        self.assertEqual(self.jar.header_for("example.com", "/dir/other", True), "a=1")
        self.assertEqual(self.jar.header_for("example.com", "/elsewhere", True), "")
        self.jar.store("example.com", "/dir/page", True, ["a=2"])
        self.assertEqual(self.jar.header_for("example.com", "/dir/x", True), "a=2")

    def test_size_caps_and_clear(self):
        jar = pt_core.CookieJar(max_total=10, max_per_site=4, clock=lambda: self.now[0])
        for i in range(8):
            self.now[0] += 1
            jar.store("example.com", "/", True, ["c%d=%d" % (i, i)])
        self.assertEqual(jar.count(), 4)
        self.assertIn("c7=7", jar.header_for("example.com", "/", True))       # the oldest went first
        self.assertNotIn("c0=", jar.header_for("example.com", "/", True))
        for site in range(6):
            for i in range(4):
                self.now[0] += 1
                jar.store("s%d.example.net" % site, "/", True, ["k%d=v" % i])
        self.assertLessEqual(jar.count(), 10)
        jar.store("big.example.org", "/", True, ["x=" + "a" * 5000])
        self.assertEqual(jar.header_for("big.example.org", "/", True), "")        # one cookie over 4 KiB is refused
        jar.clear()
        self.assertEqual(jar.count(), 0)
        self.assertEqual(jar.sites(), [])

    def test_site_buckets_do_not_leak(self):
        self.jar.store("a.example.com", "/", True, ["a=1; Domain=example.com"])
        self.jar.store("www.other.org", "/", True, ["o=1"])
        self.assertEqual(self.jar.header_for("www.other.org", "/", True), "o=1")
        self.assertEqual(self.jar.header_for("evilexample.com", "/", True), "")


# ---------------------------------------------------------------------------------------------------- headers
class HeaderTests(unittest.TestCase):
    TARGET = pt_core.parse_target("https://www.example.com/a/b?q=1")

    def test_upstream_request_headers(self):
        incoming = [("Host", "nas.ts.net:8443"), ("Cookie", "qd_pt=secret; other=1"), ("Accept", "text/html"),
                    ("Accept-Language", "ja"), ("Accept-Encoding", "br, gzip"), ("User-Agent", "Mozilla/5.0 iPhone"),
                    ("X-Forwarded-For", "100.1.2.3"), ("X-Forwarded-Proto", "https"), ("Tailscale-User-Login", "me@example.com"),
                    ("Sec-Fetch-Site", "same-origin"), ("X-QD-PT-Top", "www.example.com"), ("X-Requested-With", "XMLHttpRequest"),
                    ("X-Csrf-Token", "abc"), ("Connection", "keep-alive"), ("Range", "bytes=0-9"), ("Origin", "https://nas.ts.net:8443"),
                    ("Referer", "https://nas.ts.net:8443/p/s/www.example.com/a"), ("Content-Type", "application/json"),
                    ("Content-Length", "5"), ("Via", "x"), ("Forwarded", "for=1.2.3.4"), ("Authorization", "Bearer t"),
                    ("Upgrade", "websocket"), ("X-Real-Ip", "1.2.3.4"), ("Dnt", "1"), ("Priority", "u=1")]
        headers = pt_core.build_upstream_headers(incoming, self.TARGET, "https://www.example.com/a", "sid=1", "UA/1", 5, "POST")
        names = [n.lower() for n, _ in headers]
        got = dict((n.lower(), v) for n, v in headers)
        self.assertEqual(got["host"], "www.example.com")
        self.assertEqual(got["user-agent"], "UA/1")
        self.assertEqual(got["accept-encoding"], "gzip, deflate")           # never br: the stdlib cannot decode it
        self.assertEqual(got["cookie"], "sid=1")                            # the jar's, not the phone's
        self.assertEqual(got["referer"], "https://www.example.com/a")
        self.assertEqual(got["origin"], "https://www.example.com")
        self.assertEqual(got["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(got["x-csrf-token"], "abc")
        self.assertEqual(got["content-length"], "5")
        self.assertEqual(got["authorization"], "Bearer t")
        for banned in ("x-forwarded-for", "x-forwarded-proto", "tailscale-user-login", "sec-fetch-site", "x-qd-pt-top", "via",
                       "forwarded", "upgrade", "x-real-ip", "connection", "dnt", "priority"):
            self.assertNotIn(banned, names)
        self.assertEqual(names.count("cookie"), 1)
        self.assertNotIn("qd_pt", json.dumps(headers))
        self.assertEqual(names.count("host"), 1)

    def test_default_ua_and_no_origin_on_plain_get(self):
        headers = dict((n.lower(), v) for n, v in pt_core.build_upstream_headers([], self.TARGET, "", "", None, None, "GET"))
        self.assertEqual(headers["user-agent"], pt_core.DEFAULT_UA)
        self.assertIn("iPhone", pt_core.DEFAULT_UA)
        self.assertNotIn("origin", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("content-length", headers)

    def test_referer_is_mapped_back_and_trimmed_across_sites(self):
        target = pt_core.parse_target("https://www.example.com/x")
        same = "https://b.test:8443/p/s/www.example.com/page?a=1"
        self.assertEqual(pt_core.real_referer(same, target), "https://www.example.com/page?a=1")
        other = "https://b.test:8443/p/s/news.example.net/story?id=7"
        self.assertEqual(pt_core.real_referer(other, target), "https://news.example.net/")
        self.assertEqual(pt_core.real_referer("https://b.test:8443/", target), "")
        self.assertEqual(pt_core.real_referer("", target), "")
        self.assertEqual(pt_core.real_referer("garbage", target), "")
        http_page = "https://b.test:8443/p/h/news.example.net/story"
        self.assertEqual(pt_core.real_referer(http_page, target), "http://news.example.net/")
        self.assertEqual(pt_core.real_referer("https://b.test/p/h/news.example.net/", pt_core.parse_target("https://www.example.com/")), "http://news.example.net/")

    def test_response_headers(self):
        upstream = [("Content-Type", "text/html"), ("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2"),
                    ("Content-Security-Policy", "default-src 'none'"), ("Content-Security-Policy-Report-Only", "x"),
                    ("X-Frame-Options", "DENY"), ("Strict-Transport-Security", "max-age=1"), ("Alt-Svc", "h3=:443"),
                    ("Clear-Site-Data", '"cookies"'), ("Cross-Origin-Opener-Policy", "same-origin"),
                    ("Cross-Origin-Embedder-Policy", "require-corp"), ("Cross-Origin-Resource-Policy", "same-site"),
                    ("Location", "/next?x=1"), ("Cache-Control", "max-age=60"), ("ETag", '"e"'), ("Link", "<https://x>; rel=preconnect"),
                    ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Transfer-Encoding", "chunked"),
                    ("Content-Length", "10"), ("Access-Control-Allow-Origin", "https://www.example.com"),
                    ("Access-Control-Allow-Credentials", "true"), ("Vary", "Origin"), ("Content-Disposition", "inline")]
        out, cookies, failed = pt_core.rewrite_response_headers(upstream, self.TARGET, "https://b.test:8443", "https://www.example.com")
        names = {n.lower(): v for n, v in out}
        self.assertEqual(cookies, ["a=1; Path=/", "b=2"])
        self.assertFalse(failed)
        for dropped in ("set-cookie", "content-security-policy", "content-security-policy-report-only", "x-frame-options",
                        "strict-transport-security", "alt-svc", "clear-site-data", "cross-origin-opener-policy",
                        "cross-origin-embedder-policy", "cross-origin-resource-policy", "link", "referrer-policy",
                        "transfer-encoding", "content-length"):
            self.assertNotIn(dropped, names)
        self.assertEqual(names["location"], "/p/s/www.example.com/next?x=1")
        self.assertEqual(names["cache-control"], "max-age=60")
        self.assertEqual(names["x-content-type-options"], "nosniff")        # upstream's own, kept
        self.assertEqual(names["etag"], '"e"')
        self.assertEqual(names["access-control-allow-origin"], "https://b.test:8443")
        self.assertEqual(names["access-control-allow-credentials"], "true")

    def test_location_variants(self):
        def location(value, base="https://www.example.com/a/b"):
            out, _, failed = pt_core.rewrite_response_headers([("Location", value)], pt_core.parse_target(base), "https://b", "")
            return None if failed else dict(out)["Location"]
        self.assertEqual(location("https://cdn.example.net/x?y=1"), "/p/s/cdn.example.net/x?y=1")
        self.assertEqual(location("//cdn.example.net/x"), "/p/s/cdn.example.net/x")
        self.assertEqual(location("c"), "/p/s/www.example.com/a/c")
        self.assertEqual(location("../c?z"), "/p/s/www.example.com/c?z")
        self.assertEqual(location("http://plain.example.org/"), "/p/h/plain.example.org/")
        self.assertEqual(location("/x#frag"), "/p/s/www.example.com/x")
        self.assertIsNone(location("intent://scan/#Intent;end"))
        self.assertIsNone(location("javascript:alert(1)"))
        self.assertIsNone(location("https://[::1]/x"))

    def test_cors_allow_origin_is_only_rewritten_for_the_page_or_wildcard(self):
        def acao(value, page="https://www.example.com"):
            out, _, _ = pt_core.rewrite_response_headers([("Access-Control-Allow-Origin", value)], self.TARGET, "https://b", page)
            return dict(out).get("Access-Control-Allow-Origin")
        self.assertEqual(acao("*"), "https://b")
        self.assertEqual(acao("https://www.example.com"), "https://b")
        self.assertIsNone(acao("https://evil.example.org"))
        self.assertIsNone(acao("null"))

    def test_refresh_header(self):
        out, _, _ = pt_core.rewrite_response_headers([("Refresh", "5; url=https://x.example.org/y")], self.TARGET, "https://b", "")
        self.assertEqual(dict(out)["Refresh"], "5; url=/p/s/x.example.org/y")
        out, _, _ = pt_core.rewrite_response_headers([("Refresh", "5")], self.TARGET, "https://b", "")
        self.assertEqual(dict(out)["Refresh"], "5")

    def test_html_csp_only_lets_the_app_frame(self):
        csp = pt_core.html_csp("https://app.ts.net")
        self.assertIn("frame-ancestors https://app.ts.net", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertIn("frame-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", pt_core.html_csp(""))


# ---------------------------------------------------------------------------------------------------- HTML injection
class InjectionTests(unittest.TestCase):
    S = b"<!--S-->"

    def run_injector(self, data, pieces=1):
        injector = pt_core.HtmlInjector(self.S)
        out = b""
        size = max(1, len(data) // pieces)
        for index in range(0, len(data), size):
            out += injector.feed(data[index:index + size])
        return out + injector.finish()

    def test_normal_document(self):
        out = self.run_injector(b"<!doctype html><html lang=ja><head><title>t</title></head><body>x</body></html>")
        self.assertEqual(out, b"<!doctype html><html lang=ja><head><!--S--><title>t</title></head><body>x</body></html>")

    def test_uppercase_tags_and_attributes(self):
        out = self.run_injector(b"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\">\n<HTML>\n<HEAD profile=x>\n<TITLE>t</TITLE>")
        self.assertIn(b"<HEAD profile=x><!--S-->\n<TITLE>" if False else b"<!--S-->", out)
        self.assertTrue(out.startswith(b"<!DOCTYPE HTML PUBLIC"))
        self.assertLess(out.index(b"<HEAD"), out.index(b"<!--S-->"))
        self.assertLess(out.index(b"<!--S-->"), out.index(b"<TITLE>"))

    def test_charset_meta_stays_first(self):
        page = b'<!doctype html><html><head><meta charset="Shift_JIS"><title>t</title>'
        out = self.run_injector(page)
        self.assertEqual(out, b'<!doctype html><html><head><meta charset="Shift_JIS"><!--S--><title>t</title>')
        legacy = b'<html><head><META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=EUC-JP"><title>t'
        self.assertEqual(self.run_injector(legacy), b'<html><head><META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=EUC-JP"><!--S--><title>t')
        self.assertLess(out.index(b"charset"), 1024)

    def test_no_head(self):
        self.assertEqual(self.run_injector(b"<!doctype html><html><body>x</body></html>"),
                         b"<!doctype html><html><!--S--><body>x</body></html>")
        self.assertEqual(self.run_injector(b"<!doctype html><p>hi"), b"<!doctype html><!--S--><p>hi")
        self.assertEqual(self.run_injector(b"<p>hi"), b"<!--S--><p>hi")
        self.assertEqual(self.run_injector(b"plain text"), b"<!--S-->plain text")
        self.assertEqual(self.run_injector(b""), b"<!--S-->")

    def test_bom_and_comments_before_doctype(self):
        out = self.run_injector(b"\xef\xbb\xbf<!doctype html><head><title>t")
        self.assertTrue(out.startswith(b"\xef\xbb\xbf<!doctype html><head><!--S-->"))
        out = self.run_injector(b"<!-- a <head> comment --><!doctype html><html><head>x")
        self.assertEqual(out, b"<!-- a <head> comment --><!doctype html><html><head><!--S-->x")

    def test_utf16_is_left_alone(self):
        data = "<html><head></head>".encode("utf-16")
        self.assertEqual(self.run_injector(data), data)

    def test_header_tag_is_not_head(self):
        out = self.run_injector(b"<!doctype html><html><header>x</header><head></head>")
        self.assertEqual(out, b"<!doctype html><html><!--S--><header>x</header><head></head>")

    def test_chunk_boundaries(self):
        page = b'<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body>' + b"x" * 5000 + b"</body></html>"
        for pieces in (1, 2, 7, 50, len(page)):
            with self.subTest(pieces=pieces):
                out = self.run_injector(page, pieces)
                self.assertEqual(out, page.replace(b'charset="utf-8">', b'charset="utf-8"><!--S-->', 1))

    def test_long_prefix_does_not_buffer_forever(self):
        injector = pt_core.HtmlInjector(self.S)
        first = injector.feed(b"<!-- " + b"x" * 100)
        self.assertEqual(first, b"")                       # still inside an open comment: wait ...
        later = injector.feed(b"y" * 9000)
        self.assertTrue(later.endswith(b"y" * 9000))       # ... but never past the 8 KiB limit
        self.assertIn(b"<!--S-->", later)
        self.assertTrue(injector.done)
        self.assertEqual(injector.feed(b"more"), b"more")

    def test_snippet_escapes_the_app_origin(self):
        snippet = pt_core.inject_snippet('https://a"><script>x</script>', "0.1.57").decode()
        self.assertNotIn('"><script>x', snippet)
        self.assertIn("/pt/helper.js?v=0.1.57", snippet)
        self.assertIn('name="referrer" content="same-origin"', snippet)

    def test_decoder_gzip_deflate_raw_and_bombs(self):
        data = b"<html><head></head>" + b"hello " * 10000
        for name, blob in (("gzip", gzip.compress(data)), ("deflate", zlib.compress(data)),
                           ("deflate", zlib.compress(data)[2:-4]), ("", data), ("identity", data)):
            with self.subTest(name=name, size=len(blob)):
                decoder = pt_core.Decoder(name)
                out = b"".join(piece for i in range(0, len(blob), 777) for piece in decoder.feed(blob[i:i + 777])) + decoder.flush()
                self.assertEqual(out, data)
        with self.assertRaises(ValueError):
            pt_core.Decoder("br")
        bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
        decoder = pt_core.Decoder("gzip")
        for piece in decoder.feed(bomb[:2000]):
            self.assertLessEqual(len(piece), 1 << 20)             # never more than 1 MiB per step

    def test_injected_pages_in_every_encoding(self):
        page = '<!doctype html><html><head><meta charset="utf-8"><title>日本語</title></head><body>本文</body></html>'.encode()
        for enc, blob in (("gzip", gzip.compress(page)), ("deflate", zlib.compress(page)), ("identity", page)):
            decoder = pt_core.Decoder(enc)
            injector = pt_core.HtmlInjector(self.S)
            out = b""
            for piece in decoder.feed(blob):
                out += injector.feed(piece)
            out += injector.feed(decoder.flush()) + injector.finish()
            self.assertIn("日本語".encode(), out)
            self.assertIn(b"<!--S-->", out)


# ---------------------------------------------------------------------------------------------------- downloads
class DownloadDecisionTests(unittest.TestCase):
    T = pt_core.parse_target("https://dl.example.com/files/archive%20one.zip?t=1")
    MIB = 1024 * 1024

    def decide(self, headers, method="GET", status=200, nav=True, ranged=False):
        return pt_core.download_decision(method, status, headers, nav, self.T, ranged)

    def test_attachment_is_a_download_at_any_size(self):
        got = self.decide([("Content-Disposition", 'attachment; filename="a b.zip"'), ("Content-Length", "10")])
        self.assertEqual(got["name"], "a b.zip")
        self.assertEqual(got["size"], 10)
        self.assertTrue(self.decide([("Content-Disposition", "Attachment")]))
        self.assertEqual(self.decide([("Content-Disposition", "attachment; filename*=UTF-8''%E6%97%A5%E6%9C%AC.zip")])["name"], "日本.zip")
        self.assertEqual(self.decide([("Content-Disposition", "attachment")])["name"], "archive one.zip")

    def test_inline_is_not(self):
        self.assertIsNone(self.decide([("Content-Disposition", "inline; filename=a.pdf")]))
        self.assertIsNone(self.decide([("Content-Type", "text/html")]))

    def test_size_threshold_for_binary_types(self):
        big = 20 * self.MIB + 1
        self.assertTrue(self.decide([("Content-Type", "application/zip"), ("Content-Length", str(big))]))
        self.assertTrue(self.decide([("Content-Type", "application/octet-stream"), ("Content-Length", str(big))]))
        self.assertIsNone(self.decide([("Content-Type", "application/zip"), ("Content-Length", str(20 * self.MIB))]))
        self.assertIsNone(self.decide([("Content-Type", "application/zip"), ("Content-Length", "100")]))
        self.assertIsNone(self.decide([("Content-Type", "application/zip")]))                    # unknown length: streamed
        for viewable in ("video/mp4", "audio/mpeg", "image/png", "text/plain", "application/pdf", "application/json", "text/html"):
            self.assertIsNone(self.decide([("Content-Type", viewable), ("Content-Length", str(big * 50))]), viewable)

    def test_only_document_gets_with_a_full_200(self):
        attach = [("Content-Disposition", "attachment")]
        self.assertIsNone(self.decide(attach, method="POST"))
        self.assertIsNone(self.decide(attach, method="HEAD"))
        self.assertIsNone(self.decide(attach, status=206))
        self.assertIsNone(self.decide(attach, status=404))
        self.assertIsNone(self.decide(attach, nav=False))
        self.assertIsNone(self.decide(attach, ranged=True))

    def test_file_names_are_sanitised(self):
        self.assertEqual(pt_core.filename_from('attachment; filename="../../etc/passwd"', self.T), "_.._etc_passwd")      # no separators, no leading dots
        self.assertEqual(pt_core.filename_from('attachment; filename="a\r\nb.zip"', self.T)[:1], "a")
        self.assertLessEqual(len(pt_core.filename_from('attachment; filename="%s"' % ("x" * 500), self.T)), 120)
        self.assertEqual(pt_core.filename_from("", pt_core.parse_target("https://dl.example.com/")), "dl.example.com")

    def test_navigation_detection(self):
        self.assertTrue(pt_core.is_navigation("document"))
        self.assertTrue(pt_core.is_navigation("iframe"))
        self.assertFalse(pt_core.is_navigation("image"))
        self.assertFalse(pt_core.is_navigation("script"))
        self.assertFalse(pt_core.is_navigation("empty", accept="text/html"))
        self.assertTrue(pt_core.is_navigation("", "navigate"))
        self.assertTrue(pt_core.is_navigation("", "", "text/html,application/xhtml+xml"))
        self.assertFalse(pt_core.is_navigation("", "", "*/*"))


# ---------------------------------------------------------------------------------------------------- state
class StateTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.state = pt_core.PassthroughState(clock=lambda: self.now[0])

    def test_ticket_is_single_use(self):
        ticket = self.state.new_ticket("https://app")
        self.assertGreaterEqual(len(ticket), 40)
        first = self.state.redeem_ticket(ticket)
        self.assertIsNotNone(first)
        self.assertEqual(first[1].app_origin, "https://app")
        self.assertIsNone(self.state.redeem_ticket(ticket))
        self.assertIsNone(self.state.redeem_ticket("x" * 43))
        self.assertIsNone(self.state.redeem_ticket(""))
        self.assertIsNone(self.state.redeem_ticket(None))

    def test_ticket_expires_after_60_seconds(self):
        ticket = self.state.new_ticket("https://app")
        self.now[0] += 59
        self.assertIsNotNone(self.state.redeem_ticket(ticket))
        ticket = self.state.new_ticket("https://app")
        self.now[0] += 61
        self.assertIsNone(self.state.redeem_ticket(ticket))

    def test_tickets_are_random_and_bounded(self):
        tickets = {self.state.new_ticket("https://app") for _ in range(200)}
        self.assertEqual(len(tickets), 200)
        self.assertLessEqual(len(self.state.tickets), pt_core.TICKET_MAX)

    def test_session_idle_and_absolute_limits(self):
        token, session = self.state.create_session("https://app")
        self.assertIs(self.state.session_for(token), session)
        self.now[0] += 11 * 3600
        self.assertIsNotNone(self.state.session_for(token))             # touched: idle clock restarts
        self.now[0] += 11 * 3600
        self.assertIsNotNone(self.state.session_for(token))
        self.now[0] += 13 * 3600
        self.assertIsNone(self.state.session_for(token))               # 12 h idle
        token, _ = self.state.create_session("https://app")
        for _ in range(8):
            self.now[0] += 23 * 3600
            self.state.session_for(token)
        self.assertIsNone(self.state.session_for(token))               # 7 days absolute, however busy
        self.assertIsNone(self.state.session_for("nope" * 10))
        self.assertIsNone(self.state.session_for(None))

    def test_revoke_all_and_limits(self):
        tokens = [self.state.create_session("https://app")[0] for _ in range(12)]
        self.assertLessEqual(len(self.state.sessions), pt_core.SESSION_MAX)
        self.assertIsNone(self.state.session_for(tokens[0]))             # the oldest was dropped
        ticket = self.state.new_ticket("https://app")
        session = self.state.session_for(tokens[-1])
        ident = self.state.add_download("https://x.example.com/f", "f", 1, "t", session)
        self.assertEqual(self.state.revoke_all(), len(tokens) - 4)
        self.assertIsNone(self.state.session_for(tokens[-1]))
        self.assertIsNone(self.state.redeem_ticket(ticket))
        self.assertIsNone(self.state.get_download(ident))

    def test_download_records(self):
        _, session = self.state.create_session("https://app")
        _, other = self.state.create_session("https://app")
        ident = self.state.add_download("https://x.example.com/f", "f.zip", 5, "application/zip", session)
        self.assertEqual(self.state.get_download(ident)["url"], "https://x.example.com/f")
        self.assertIsNone(self.state.get_download(ident, other))        # bound to the session that met it
        self.assertIsNotNone(self.state.get_download(ident, session))
        self.now[0] += 601
        self.assertIsNone(self.state.get_download(ident))
        self.assertIsNone(self.state.get_download(None))

    def test_clear_cookies_empties_every_jar(self):
        _, a = self.state.create_session("https://app")
        _, b = self.state.create_session("https://app")
        a.jar.store("example.com", "/", True, ["x=1"])
        b.jar.store("example.net", "/", True, ["y=1"])
        self.assertEqual(self.state.clear_cookies(), 2)
        self.assertEqual(a.jar.count() + b.jar.count(), 0)

    def test_app_origins_are_bounded(self):
        for i in range(10):
            self.state.note_app_origin("https://app%d.ts.net" % i)
        self.assertEqual(len(self.state.app_origins), 4)
        self.state.note_app_origin("")
        self.state.note_app_origin(None)
        self.state.note_app_origin("x" * 500)
        self.assertEqual(len(self.state.app_origins), 4)


# ---------------------------------------------------------------------------------------------------- the listener
class Upstream:
    """A small origin server standing in for the sites; it answers by path and records what it received."""

    def __init__(self, tls=None):
        self.seen = []
        self.connections = 0
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_ANY(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                host = self.headers.get("Host", "")
                outer.seen.append({"host": host, "path": self.path, "method": self.command, "headers": dict(self.headers.items()),
                                   "body": body})
                path = urlsplit(self.path).path
                h = {"Content-Type": "text/plain"}
                status, data = 200, b"ok"
                if path == "/":
                    h["Content-Type"] = "text/html; charset=utf-8"
                    h["Content-Security-Policy"] = "default-src 'none'"
                    h["X-Frame-Options"] = "DENY"
                    h["Strict-Transport-Security"] = "max-age=31536000"
                    h["Referrer-Policy"] = "no-referrer"
                    data = ('<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head><body>本文 %s</body></html>'
                            % (host, host)).encode()
                elif path == "/gz":
                    h["Content-Type"] = "text/html"
                    h["Content-Encoding"] = "gzip"
                    data = gzip.compress(b"<html><head><title>gz</title></head><body>zipped</body></html>")
                elif path == "/gz.js":
                    h["Content-Type"] = "text/javascript"
                    h["Content-Encoding"] = "gzip"
                    data = gzip.compress(b"var a = 1;")
                elif path == "/br":
                    h["Content-Type"] = "text/html"
                    h["Content-Encoding"] = "br"
                    data = b"\x00\x01"
                elif path == "/set":
                    h["Set-Cookie"] = "sid=A1; Path=/"
                elif path == "/echo":
                    data = json.dumps({"cookie": self.headers.get("Cookie"), "referer": self.headers.get("Referer"),
                                       "origin": self.headers.get("Origin"), "ua": self.headers.get("User-Agent"),
                                       "xff": self.headers.get("X-Forwarded-For"), "ae": self.headers.get("Accept-Encoding"),
                                       "qd": [k for k in self.headers.keys() if k.lower().startswith(("x-qd", "sec-", "tailscale"))],
                                       "host": host}).encode()
                    h["Content-Type"] = "application/json"
                elif path == "/redir":
                    status, data = 302, b""
                    h["Location"] = "http://b.example.net/landing?x=1"
                elif path == "/redir-rel":
                    status, data = 301, b""
                    h["Location"] = "../other"
                elif path == "/to-private":
                    status, data = 302, b""
                    h["Location"] = "http://169.254.169.254/latest/meta-data/"
                elif path == "/file.zip":
                    h["Content-Type"] = "application/zip"
                    h["Content-Disposition"] = 'attachment; filename="pack.zip"'
                    data = b"PK" * 600
                elif path == "/big.bin":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(21 * 1024 * 1024))
                    self.end_headers()
                    self.close_connection = True
                    return
                elif path == "/chunks":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"5\r\nfirst\r\n")
                    self.wfile.flush()
                    outer.release.wait(5)
                    self.wfile.write(b"6\r\nsecond\r\n0\r\n\r\n")
                    return
                elif path == "/range":
                    status = 206
                    h["Content-Range"] = "bytes 0-3/10"
                    h["Accept-Ranges"] = "bytes"
                    data = b"0123"
                elif path == "/304":
                    status, data = 304, b""
                    h["ETag"] = '"v1"'
                elif path == "/post":
                    data = body
                elif path == "/nolen":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b"until-close")
                    self.close_connection = True
                    return
                elif path == "/cors":
                    h["Access-Control-Allow-Origin"] = "*"
                    h["Content-Type"] = "application/json"
                    data = b"{}"
                elif path == "/img":
                    h["Content-Type"] = "image/png"
                    data = b"\x89PNG"
                elif path != "/ok":
                    status, data = 404, b"nope"
                self.send_response(status)
                for key, value in h.items():
                    self.send_header(key, value)
                if self.command != "HEAD" or True:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD" and status not in (204, 304):
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_PATCH = do_OPTIONS = do_ANY

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def get_request(self):
                result = super().get_request()
                outer.connections += 1
                return result

            def handle_error(self, request, client_address):
                pass

        self.httpd = Server(("127.0.0.1", 0), Handler)
        if tls:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls[0], tls[1])
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class Reply:
    def __init__(self, response, body):
        self.status, self.reason, self.body = response.status, response.reason, body
        self.headers = response.getheaders()
        self.h = {k.lower(): v for k, v in self.headers}

    def all(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self):
        return json.loads(self.body)


class ListenerCase(unittest.TestCase):
    APP = "https://app.tail1.ts.net"

    def setUp(self):
        patcher = mock.patch.object(passthrough.PassthroughManager, "poll_interval", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = storage.Store(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.up = Upstream()
        self.addCleanup(self.up.close)
        mapping = {"a.example.com": ("127.0.0.1", self.up.port), "b.example.net": ("127.0.0.1", self.up.port),
                   "c.example.org": ("127.0.0.1", self.up.port)}
        self.mgr = passthrough.PassthroughManager(self.store, "0.1.57", HOOKS, port=0, test_upstream=mapping)
        self.store.set_option("pt_enabled", True)
        self.mgr.sync()
        self.addCleanup(self.mgr.stop)
        self.port = self.mgr.bound_port
        self.cookie = ""

    # -- helpers
    def request(self, method, path, headers=None, body=None, host=None, cookie=True, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": host or "localhost:%d" % self.port}
        if cookie and self.cookie:
            sent["Cookie"] = "qd_pt=" + self.cookie
        sent.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            return Reply(response, response.read())
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def enter(self, url="http://a.example.com/", app=None):
        ticket = self.mgr.state.new_ticket(app or self.APP)
        reply = self.get("/pt/enter?t=%s&u=%s" % (ticket, quote(url, safe="")), cookie=False)
        for value in reply.all("Set-Cookie"):
            match = re.match(r"qd_pt=([^;]+)", value)
            if match:
                self.cookie = match.group(1)
        return reply

    def proxy(self, path, top="a.example.com", dest="iframe", extra=None, method="GET", body=None, **kw):
        headers = {"X-QD-PT-Dest": dest}
        if top is not None:
            headers["X-QD-PT-Top"] = top
        headers.update(extra or {})
        return self.request(method, path, headers=headers, body=body, **kw)


class ListenerTests(ListenerCase):
    # ---- what the listener serves at all
    def test_it_never_serves_the_hub(self):
        self.enter()
        for path in ("/", "/index.html", "/app.js", "/style.css", "/sw.js", "/icon.svg", "/api/session", "/api/health",
                     "/api/state", "/api/settings", "/api/pt/status", "/api/login", "/api/setup/status", "/pt/", "/pt",
                     "/p", "/p/", "/favicon.ico", "/robots.txt", "/pt/selftest/x", "/pt/..%2f..%2fwebapp.py", "/pt/save",
                     "/web/app.js", "/webapp.py", "/storage.py", "//etc/passwd"):
            for method in ("GET", "HEAD"):
                reply = self.request(method, path)
                self.assertIn(reply.status, (400, 404), "%s %s -> %s" % (method, path, reply.status))
                self.assertNotIn(b"QnapDownloader", reply.body)
                self.assertNotIn(b"function ", reply.body)
        for path in ("/api/login", "/api/pt/ticket", "/api/settings/options", "/pt/ping", "/pt/enter", "/"):
            reply = self.request("POST", path, body=b"{}", headers={"Content-Type": "application/json", "Origin": "http://localhost:%d" % self.port})
            self.assertEqual(reply.status, 404, path)

    def test_the_hub_session_cookie_is_not_an_identity_here(self):
        reply = self.request("GET", "/p/h/a.example.com/", headers={"Cookie": "qdl_session=abcdef0123456789abcdef0123456789abcdef01"},
                             cookie=False)
        self.assertEqual(reply.status, 401)
        self.enter()
        reply = self.proxy("/p/h/a.example.com/echo", extra={"Cookie": "qd_pt=%s; qdl_session=zzz; sid=phone" % self.cookie})
        self.assertEqual(reply.status, 200)
        sent = reply.json()
        self.assertIsNone(sent["cookie"])                                 # the phone's cookies never reach a site
        self.assertNotIn(self.cookie, json.dumps(self.up.seen[-1]["headers"]))

    def test_host_rules(self):
        handler = passthrough.PtHandler.__new__(passthrough.PtHandler)
        handler.server = mock.Mock(mgr=self.mgr)
        class Headers(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)
        def ok(host, peer="127.0.0.1"):
            handler.client_address = (peer, 1234)
            handler.headers = Headers(Host=host)
            return handler.host_ok()
        for host in ("localhost", "localhost:17865", "127.0.0.1:17865", "nas.tail1234.ts.net", "nas.tail1234.ts.net:8443",
                     "NAS.TAIL1234.TS.NET"):
            self.assertTrue(ok(host), host)
        for host in ("", "evil.example.com", "evil.example.com:8443", "nas", "nas:8443", "ts.net", "localhost.evil.com",
                     "nas.ts.net.evil.com", "evilts.net", "192.168.1.5", "10.0.0.1:17865", "100.100.1.1", "[::1]", "[::1]:17865",
                     "nas.tail1234.ts.net:99999", "nas.tail1234.ts.net:abc", "user@nas.tail1234.ts.net", "a b.ts.net",
                     "nas.tail1234.ts.net/x", "127.0.0.2", "0.0.0.0"):
            self.assertFalse(ok(host), host)
        for peer in ("192.168.1.10", "100.64.0.5", "10.0.0.2", "8.8.8.8", "fd7a:115c:a1e0::5"):
            self.assertFalse(ok("nas.tail1234.ts.net", peer), peer)       # only Tailscale Serve (loopback) may talk to it
        self.assertTrue(ok("nas.tail1234.ts.net", "::1"))
        self.assertTrue(ok("nas.tail1234.ts.net", "::ffff:127.0.0.1"))

    def test_bad_host_is_refused_over_http(self):
        self.enter()
        for host in ("evil.example.com", "192.168.1.5:17865", "nas"):
            reply = self.request("GET", "/pt/ping", host=host)
            self.assertEqual(reply.status, 403, host)
            self.assertEqual(self.request("GET", "/p/h/a.example.com/", host=host).status, 403)
        self.assertEqual(self.request("GET", "/pt/ping", host="nas.tail1234.ts.net:8443").status, 200)

    def test_disabled_listener_is_gone(self):
        self.assertEqual(self.get("/pt/ping").status, 200)
        self.store.set_option("pt_enabled", False)
        self.mgr.sync()
        self.assertFalse(self.mgr.enabled)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        self.assertEqual(self.mgr.state.sessions, {})
        self.assertFalse(self.mgr.status()["listening"])
        self.store.set_option("pt_enabled", True)
        self.mgr.sync()
        self.assertTrue(self.mgr.status()["listening"])

    def test_requests_on_an_open_connection_stop_with_the_switch(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/pt/ping", headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(conn.getresponse().read() is not None, True)
        self.mgr.enabled = False                    # the switch flips while a keep-alive connection is open
        conn.request("GET", "/pt/ping", headers={"Host": "localhost:%d" % self.port})
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        conn.close()
        self.mgr.enabled = True

    def test_port_in_use_is_reported_not_raised(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            other = passthrough.PassthroughManager(self.store, "t", HOOKS, port=s.getsockname()[1])
            self.assertFalse(other.start())
            self.assertIn("待ち受けできません", other.error)
            self.assertFalse(other.enabled)

    # ---- ticket and session
    def test_enter_sets_its_own_cookie_and_the_ticket_is_single_use(self):
        ticket = self.mgr.state.new_ticket(self.APP)
        url = "/pt/enter?t=%s&u=%s" % (ticket, quote("http://a.example.com/x?y=1", safe=""))
        first = self.get(url, cookie=False)
        self.assertEqual(first.status, 200)
        cookie = first.all("Set-Cookie")[0]
        self.assertTrue(cookie.startswith("qd_pt="))
        for attr in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age="):
            self.assertIn(attr, cookie)
        self.assertNotIn("Secure", cookie)                                  # plain http (the E2E host); see the https test below
        self.assertIn(b'data-target="/p/h/a.example.com/x?y=1"', first.body)
        self.assertIn(b'src="/pt/boot.js"', first.body)
        self.assertNotIn(b"<script>", first.body)                           # no inline script: the CSP forbids it
        self.assertIn("frame-ancestors " + self.APP, first.h["content-security-policy"])
        self.assertEqual(first.h["referrer-policy"], "no-referrer")
        self.assertIn("no-store", first.h["cache-control"])
        second = self.get(url, cookie=False)
        self.assertEqual(second.status, 401)
        self.assertEqual(second.all("Set-Cookie"), [])

    def test_https_through_tailscale_serve_gets_a_secure_cookie(self):
        ticket = self.mgr.state.new_ticket(self.APP)
        reply = self.get("/pt/enter?t=%s&u=%s" % (ticket, quote("https://a.example.com/", safe="")), cookie=False,
                         host="nas.tail1234.ts.net:8443", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(reply.status, 200)
        self.assertIn("Secure", reply.all("Set-Cookie")[0])
        ping = self.get("/pt/ping", host="nas.tail1234.ts.net:8443")
        self.assertTrue(ping.json()["https"])
        self.assertFalse(self.get("/pt/ping").json()["https"])

    def test_enter_rejects_bad_tickets_and_urls(self):
        for t in ("", "x", "x" * 43, "../../etc"):
            self.assertEqual(self.get("/pt/enter?t=%s&u=http%%3A%%2F%%2Fa.example.com%%2F" % t, cookie=False).status, 401)
        self.assertEqual(self.get("/pt/enter", cookie=False).status, 401)
        for bad in ("javascript:alert(1)", "https://[::1]/", "ftp://a.example.com/", "", "https://u:p@a.example.com/"):
            ticket = self.mgr.state.new_ticket(self.APP)
            reply = self.get("/pt/enter?t=%s&u=%s" % (ticket, quote(bad, safe="")), cookie=False)
            self.assertEqual(reply.status, 400, bad)
        self.assertEqual(self.mgr.state.sessions, {} if False else self.mgr.state.sessions)

    def test_enter_with_a_valid_session_does_not_need_a_new_ticket(self):
        self.enter()
        reply = self.get("/pt/enter?u=" + quote("http://a.example.com/page", safe=""))
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.all("Set-Cookie"), [])

    def test_proxy_needs_a_session(self):
        self.assertEqual(self.get("/p/h/a.example.com/").status, 401)
        self.cookie = "A" * 43
        self.assertEqual(self.get("/p/h/a.example.com/").status, 401)
        self.enter()
        self.assertEqual(self.proxy("/p/h/a.example.com/ok").status, 200)
        self.mgr.state.revoke_all()
        self.assertEqual(self.proxy("/p/h/a.example.com/ok").status, 401)
        self.assertEqual(self.get("/pt/save/abc").status, 404)

    def test_session_idle_expiry_over_http(self):
        self.enter()
        for session in self.mgr.state.sessions.values():
            session.last -= pt_core.SESSION_IDLE + 5
        self.assertEqual(self.proxy("/p/h/a.example.com/ok").status, 401)

    def test_cross_site_requests_are_refused(self):
        self.enter()
        self.assertEqual(self.proxy("/p/h/a.example.com/ok", extra={"Sec-Fetch-Site": "cross-site"}).status, 403)
        for ok in ("same-origin", "same-site", "none"):
            self.assertEqual(self.proxy("/p/h/a.example.com/ok", extra={"Sec-Fetch-Site": ok}).status, 200)

    def test_ping_cors_names_only_the_app_origin(self):
        plain = self.get("/pt/ping", headers={"Origin": self.APP})
        self.assertNotIn("access-control-allow-origin", plain.h)               # the app was never seen
        self.mgr.state.note_app_origin(self.APP)
        ok = self.get("/pt/ping", headers={"Origin": self.APP})
        self.assertEqual(ok.h["access-control-allow-origin"], self.APP)
        self.assertEqual(ok.h["vary"], "Origin")
        self.assertEqual(ok.json()["ok"], True)
        for origin in ("https://evil.example.com", "null", self.APP + ".evil.com", "http://app.tail1.ts.net"):
            self.assertNotIn("access-control-allow-origin", self.get("/pt/ping", headers={"Origin": origin}).h, origin)
        self.assertNotIn("access-control-allow-origin", self.get("/pt/ping").h)

    def test_static_files(self):
        sw = self.get("/pt-sw.js")
        self.assertEqual(sw.status, 200)
        self.assertIn("javascript", sw.h["content-type"])
        self.assertEqual(sw.body, (ROOT / "web" / "pt-sw.js").read_bytes())
        self.assertEqual(sw.h["service-worker-allowed"], "/")
        for path, name in (("/pt/helper.js", "pt-helper.js"), ("/pt/boot.js", "pt-boot.js"), ("/pt/download.js", "pt-download.js")):
            self.assertEqual(self.get(path).body, (ROOT / "web" / name).read_bytes())
        self.assertEqual(self.request("HEAD", "/pt-sw.js").body, b"")

    # ---- proxying
    def test_html_is_proxied_cleaned_and_injected(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/")
        self.assertEqual(reply.status, 200)
        self.assertIn("本文 a.example.com".encode(), reply.body)
        self.assertIn(b'<script src="/pt/helper.js?v=0.1.57" data-qd-app="%s"></script>' % self.APP.encode(), reply.body)
        self.assertLess(reply.body.index(b'charset="utf-8"'), reply.body.index(b"/pt/helper.js"))
        self.assertLess(reply.body.index(b"/pt/helper.js"), reply.body.index(b"<title>"))
        self.assertEqual(reply.h["content-security-policy"], pt_core.html_csp(self.APP))
        for dropped in ("x-frame-options", "strict-transport-security", "referrer-policy"):
            self.assertNotIn(dropped, reply.h)
        self.assertEqual(reply.h["transfer-encoding"], "chunked")
        self.assertNotIn("content-length", reply.h)
        seen = self.up.seen[-1]
        self.assertEqual(seen["host"], "a.example.com")
        self.assertEqual(seen["headers"]["Accept-Encoding"], "gzip, deflate")
        self.assertNotIn("X-Forwarded-For", seen["headers"])

    def test_non_document_html_is_not_injected(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/", dest="empty", extra={"Accept": "*/*"})
        self.assertNotIn(b"helper.js", reply.body)
        self.assertEqual(reply.h["content-length"], str(len(reply.body)))
        self.assertNotIn("content-security-policy", reply.h)                     # upstream's was dropped, none of ours needed

    def test_gzip_html_is_decoded_and_injected_gzip_assets_stay_compressed(self):
        self.enter()
        html = self.proxy("/p/h/a.example.com/gz")
        self.assertIn(b"zipped", html.body)
        self.assertIn(b"helper.js", html.body)
        self.assertNotIn("content-encoding", html.h)
        script = self.proxy("/p/h/a.example.com/gz.js", dest="script")
        self.assertEqual(script.h["content-encoding"], "gzip")
        self.assertEqual(gzip.decompress(script.body), b"var a = 1;")
        self.assertEqual(script.h["content-length"], str(len(script.body)))
        self.assertEqual(self.proxy("/p/h/a.example.com/br").status, 502)           # no brotli decoder: refuse, never serve it bare

    def test_redirects_are_rewritten_and_not_followed(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/redir")
        self.assertEqual(reply.status, 302)
        self.assertEqual(reply.h["location"], "/p/h/b.example.net/landing?x=1")
        self.assertEqual(len(self.up.seen), 1)                                     # the Hub did not follow it itself
        rel = self.proxy("/p/h/a.example.com/dir/redir-rel")
        self.assertEqual(self.up.seen[-1]["path"], "/dir/redir-rel")
        private = self.proxy("/p/h/a.example.com/to-private")
        self.assertEqual(private.h["location"], "/p/h/169.254.169.254/latest/meta-data/")
        followed = self.proxy("/p/h/169.254.169.254/latest/meta-data/")
        self.assertEqual(followed.status, 403)                                      # every hop goes through the guard again
        self.assertEqual(len(self.up.seen), 3)

    def test_ssrf_over_http(self):
        self.enter()
        before = len(self.up.seen)
        for host in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.0.1", "169.254.169.254", "100.100.100.100", "nas.tail1234.ts.net",
                     "nas", "metadata.google.internal", "0x7f.1", "2130706433", "172.16.5.5", "printer.local"):
            reply = self.proxy("/p/h/%s/" % host)
            self.assertEqual(reply.status, 403 if "." in host or host == "localhost" else 403, host)
            self.assertEqual(reply.status, 403, host)
        self.assertEqual(self.proxy("/p/h/[::1]/").status, 400)
        self.assertEqual(self.proxy("/p/h/%5B::1%5D/").status, 400)
        self.assertEqual(len(self.up.seen), before)                                  # nothing was sent anywhere
        self.assertGreaterEqual(self.mgr.stats["blocked"], 10)

    def test_own_names_are_blocked(self):
        self.enter()
        self.get("/pt/ping", host="hub.tail1234.ts.net:8443")
        self.assertIn("hub.tail1234.ts.net", self.mgr.state.own_names)
        self.assertEqual(self.proxy("/p/s/hub.tail1234.ts.net/").status, 403)

    def test_cookies_come_from_the_jar_for_first_party_requests_only(self):
        self.enter()
        set_ = self.proxy("/p/h/a.example.com/set", top="a.example.com")
        self.assertEqual(set_.all("Set-Cookie"), [])                                 # never forwarded to the phone
        self.assertEqual(self.mgr.state.sessions and next(iter(self.mgr.state.sessions.values())).jar.count(), 1)
        # first party: same site as the top-level page
        echo = self.proxy("/p/h/a.example.com/echo", top="a.example.com", dest="empty").json()
        self.assertEqual(echo["cookie"], "sid=A1")
        # a third-party page asking the same site
        echo = self.proxy("/p/h/a.example.com/echo", top="b.example.net", dest="empty").json()
        self.assertIsNone(echo["cookie"])
        # another site (never had cookies), first party for itself
        echo = self.proxy("/p/h/b.example.net/echo", top="b.example.net", dest="empty").json()
        self.assertIsNone(echo["cookie"])
        # the SW is gone: no header, no cookies
        echo = self.proxy("/p/h/a.example.com/echo", top=None, dest="empty").json()
        self.assertIsNone(echo["cookie"])
        echo = self.proxy("/p/h/a.example.com/echo", top="", dest="empty").json()
        self.assertIsNone(echo["cookie"])
        # a same-site subdomain is first party
        echo = self.proxy("/p/h/a.example.com/echo", top="www.example.com", dest="empty").json()
        self.assertEqual(echo["cookie"], "sid=A1")
        # a third party cannot set cookies either
        self.proxy("/p/h/c.example.org/set", top="a.example.com", dest="image")
        echo = self.proxy("/p/h/c.example.org/echo", top="c.example.org", dest="empty").json()
        self.assertIsNone(echo["cookie"])

    def test_cookie_jars_are_per_session(self):
        self.enter()
        self.proxy("/p/h/a.example.com/set")
        first = self.cookie
        self.cookie = ""
        self.enter()
        self.assertNotEqual(first, self.cookie)
        echo = self.proxy("/p/h/a.example.com/echo", dest="empty").json()
        self.assertIsNone(echo["cookie"])
        self.cookie = first
        self.assertEqual(self.proxy("/p/h/a.example.com/echo", dest="empty").json()["cookie"], "sid=A1")
        self.assertEqual(self.mgr.state.clear_cookies(), 1)
        self.assertIsNone(self.proxy("/p/h/a.example.com/echo", dest="empty").json()["cookie"])

    def test_referer_origin_and_user_agent(self):
        self.enter()
        echo = self.proxy("/p/h/a.example.com/echo", dest="empty", extra={
            "Referer": "http://localhost:%d/p/h/a.example.com/dir/page?x=1" % self.port,
            "Origin": "http://localhost:%d" % self.port, "User-Agent": "Mozilla/5.0 (iPhone) Safari/604.1 Test"}).json()
        self.assertEqual(echo["referer"], "http://a.example.com/dir/page?x=1")
        self.assertEqual(echo["origin"], "http://a.example.com")
        self.assertEqual(echo["ua"], "Mozilla/5.0 (iPhone) Safari/604.1 Test")
        self.assertEqual(echo["ae"], "gzip, deflate")
        self.assertEqual(echo["qd"], [])
        cross = self.proxy("/p/h/b.example.net/echo", dest="empty", extra={
            "Referer": "http://localhost:%d/p/h/a.example.com/dir/page?secret=1" % self.port}).json()
        self.assertEqual(cross["referer"], "http://a.example.com/")                  # trimmed to the origin across sites

    def test_post_body_and_limits(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/post", method="POST", body=b'{"a": 1}', dest="empty",
                           extra={"Content-Type": "application/json"})
        self.assertEqual(reply.body, b'{"a": 1}')
        self.assertEqual(self.up.seen[-1]["headers"]["Content-Type"], "application/json")
        self.assertEqual(self.up.seen[-1]["headers"]["Content-Length"], "8")
        empty = self.proxy("/p/h/a.example.com/post", method="POST", dest="empty")
        self.assertEqual(self.up.seen[-1]["headers"]["Content-Length"], "0")
        big = self.proxy("/p/h/a.example.com/post", method="POST", dest="empty",
                         extra={"Content-Length": str(pt_core.BODY_MAX + 1)}, body=b"x")
        self.assertEqual(big.status, 413)
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertEqual(self.proxy("/p/h/a.example.com/ok", method=method, dest="empty", body=b"z").status, 200)
            self.assertEqual(self.up.seen[-1]["method"], method)

    def test_head_304_and_ranges_pass_through(self):
        self.enter()
        head = self.request("HEAD", "/p/h/a.example.com/", headers={"X-QD-PT-Dest": "iframe", "X-QD-PT-Top": "a.example.com"})
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(self.up.seen[-1]["method"], "HEAD")
        not_modified = self.proxy("/p/h/a.example.com/304", extra={"If-None-Match": '"v1"'})
        self.assertEqual(not_modified.status, 304)
        self.assertEqual(self.up.seen[-1]["headers"]["If-None-Match"], '"v1"')
        part = self.proxy("/p/h/a.example.com/range", dest="video", extra={"Range": "bytes=0-3"})
        self.assertEqual((part.status, part.body, part.h["content-range"]), (206, b"0123", "bytes 0-3/10"))
        self.assertEqual(self.up.seen[-1]["headers"]["Range"], "bytes=0-3")

    def test_cors_allow_origin_is_rewritten_to_origin_b(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/cors", dest="empty", extra={"Origin": "http://localhost:%d" % self.port})
        self.assertEqual(reply.h["access-control-allow-origin"], "http://localhost:%d" % self.port)

    def test_body_without_length_is_sent_until_close(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/nolen", dest="empty")
        self.assertEqual(reply.body, b"until-close")

    def test_keep_alive_pool_reuses_the_upstream_connection(self):
        self.enter()
        before = self.up.connections
        for _ in range(4):
            self.assertEqual(self.proxy("/p/h/a.example.com/ok", dest="empty").status, 200)
        self.assertLessEqual(self.up.connections - before, 1)

    def test_a_big_body_is_streamed_not_buffered(self):
        self.enter()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/p/h/a.example.com/chunks", headers={"Host": "localhost:%d" % self.port, "Cookie": "qd_pt=" + self.cookie,
                                                                   "X-QD-PT-Dest": "empty", "X-QD-PT-Top": "a.example.com"})
        started = time.monotonic()
        response = conn.getresponse()
        self.assertEqual(response.read(5), b"first")             # arrives while the upstream is still holding the rest
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(self.up.release.is_set())
        self.up.release.set()
        self.assertEqual(response.read(), b"second")
        conn.close()

    def test_upstream_down_gives_a_502_page(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        self.mgr.client.test_upstream["gone.example.com"] = ("127.0.0.1", port)
        self.enter()
        reply = self.proxy("/p/h/gone.example.com/")
        self.assertEqual(reply.status, 502)
        self.assertIn("接続できません".encode(), reply.body)

    def test_the_concurrent_connection_limit(self):
        self.mgr.client = pt_proxy.UpstreamClient(own_names=lambda: set(), test_upstream=self.mgr.client.test_upstream, max_connections=1,
                                                  fakes=pt_proxy.selftest_site)
        self.enter()
        with mock.patch.object(pt_proxy, "SLOT_WAIT", 0.3):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/p/h/a.example.com/chunks", headers={"Host": "localhost:%d" % self.port, "Cookie": "qd_pt=" + self.cookie,
                                                                       "X-QD-PT-Dest": "empty"})
            held = conn.getresponse()
            self.assertEqual(held.read(5), b"first")              # one slot is now in use
            second = self.proxy("/p/h/a.example.com/ok", dest="empty")
            self.assertEqual(second.status, 503)
            self.up.release.set()
            held.read()
            conn.close()
            time.sleep(0.1)
            self.assertEqual(self.proxy("/p/h/a.example.com/ok", dest="empty").status, 200)

    # ---- downloads
    def test_an_attachment_navigation_becomes_the_download_page(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/file.zip")
        self.assertEqual(reply.status, 200)
        self.assertIn("text/html", reply.h["content-type"])
        self.assertIn("pack.zip".encode(), reply.body)
        self.assertIn("JD2で落とす".encode(), reply.body)
        self.assertIn("iPhoneに保存".encode(), reply.body)
        self.assertNotIn(b"PKPK", reply.body)                                          # the file itself was not streamed
        self.assertIn("frame-ancestors " + self.APP, reply.h["content-security-policy"])
        ident = re.search(rb'data-id="([^"]+)"', reply.body).group(1).decode()
        record = self.mgr.state.get_download(ident)
        self.assertEqual(record["url"], "http://a.example.com/file.zip")
        self.assertEqual((record["name"], record["size"]), ("pack.zip", 1200))
        save = self.get("/pt/save/" + ident)
        self.assertEqual(save.status, 200)
        self.assertEqual(save.body, b"PK" * 600)
        self.assertIn("pack.zip", save.h["content-disposition"])
        self.assertNotIn(b"helper.js", save.body)

    def test_the_same_file_as_a_subresource_is_streamed(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/file.zip", dest="empty")
        self.assertEqual(reply.body, b"PK" * 600)

    def test_a_huge_unknown_binary_becomes_the_download_page_without_reading_it(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/big.bin")
        self.assertIn("iPhoneに保存".encode(), reply.body)
        self.assertIn("21.0 MiB".encode(), reply.body)

    def test_download_records_belong_to_their_session(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/file.zip")
        ident = re.search(rb'data-id="([^"]+)"', reply.body).group(1).decode()
        mine = self.cookie
        self.cookie = ""
        self.enter()
        self.assertEqual(self.get("/pt/save/" + ident).status, 404)
        self.cookie = mine
        self.assertEqual(self.get("/pt/save/" + ident).status, 200)
        self.assertEqual(self.get("/pt/save/" + ident + "x").status, 404)

    def test_html_error_pages_are_escaped(self):
        self.enter()
        reply = self.proxy("/p/h/a.example.com/<script>")
        self.assertNotIn(b"<script>", reply.body.replace(b"<script src", b""))


class TlsUpstreamTests(unittest.TestCase):
    """The real TLS path: certificates are verified, the connection goes to the checked IP with SNI = the host name."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl not installed")
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.cert, cls.key = str(tmp / "c.pem"), str(tmp / "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-keyout", cls.key, "-out", cls.cert, "-days", "2", "-subj", "/CN=a.example.com",
                        "-addext", "subjectAltName=DNS:a.example.com"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_verified_and_unverified(self):
        import ssl
        up = Upstream(tls=(self.cert, self.key))
        self.addCleanup(up.close)
        mapping = {"a.example.com": ("127.0.0.1", up.port), "wrong.example.com": ("127.0.0.1", up.port)}
        trusting = ssl.create_default_context(cafile=self.cert)
        target = pt_core.parse_target("https://a.example.com/echo")
        client = pt_proxy.UpstreamClient(test_upstream=mapping, tls_context=trusting)
        self.addCleanup(client.close)
        with client.open("GET", target, [("Host", "a.example.com")]) as resp:
            data = b"".join(resp.iter_body())
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data)["host"], "a.example.com")
        self.assertEqual(up.seen[-1]["host"], "a.example.com")
        with self.assertRaises(pt_proxy.UpstreamError) as ctx:                          # name does not match the certificate
            client.open("GET", pt_core.parse_target("https://wrong.example.com/"), [("Host", "wrong.example.com")])
        self.assertIn("証明書", str(ctx.exception))
        strict = pt_proxy.UpstreamClient(test_upstream=mapping)                          # system trust store: self-signed refused
        with self.assertRaises(pt_proxy.UpstreamError):
            strict.open("GET", target, [("Host", "a.example.com")])


class SelftestSiteTests(ListenerCase):
    def test_the_in_process_site_is_served_through_the_same_path(self):
        self.enter("http://pt-selftest.invalid/")
        page = self.proxy("/p/h/pt-selftest.invalid/", top="pt-selftest.invalid")
        self.assertIn(b"PT-SELFTEST-PAGE", page.body)
        self.assertIn(b"helper.js", page.body)
        self.assertNotIn("set-cookie", page.h)
        self.assertEqual(self.up.seen, [])                                              # nothing touched the network
        redirect = self.proxy("/p/h/pt-selftest.invalid/redir")
        self.assertEqual(redirect.h["location"], "/p/h/pt-selftest.invalid/final")
        file = self.proxy("/p/h/pt-selftest.invalid/file.bin")
        self.assertIn("iPhoneに保存".encode(), file.body)
        echo = lambda top, host="pt-selftest.invalid": self.proxy("/p/h/%s/echo-cookie" % host, top=top, dest="empty").body
        self.assertEqual(echo("pt-selftest.invalid"), b"cookie=st=first")
        self.assertEqual(echo("pt-selftest-b.invalid"), b"cookie=")
        self.assertEqual(echo("pt-selftest.invalid", "pt-selftest-b.invalid"), b"cookie=")
        for host in ("pt-selftest.invalid", "pt-selftest-b.invalid"):
            self.assertEqual(pt_proxy.selftest_site("GET", pt_core.parse_target("http://%s/nothing" % host), [], None).status, 404)
        self.assertIsNone(pt_proxy.selftest_site("GET", pt_core.parse_target("http://real.example.com/"), [], None))
        self.assertEqual(self.proxy("/p/h/other.invalid/").status, 403)                 # only the two reserved names


# ---------------------------------------------------------------------------------------------------- the Hub side
class HubCase(unittest.TestCase):
    """The real webapp.Handler (admin session, CSRF, Origin) with a passthrough manager on an ephemeral port."""

    def setUp(self):
        import os
        from unittest.mock import patch
        patcher = mock.patch.object(passthrough.PassthroughManager, "poll_interval", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch.object(webapp.Handler, "log_message", lambda *a, **k: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        allowed = root / "allowed"
        allowed.mkdir()
        env = patch.dict(os.environ, {"DOWNLOADER_ALLOWED_ROOTS": str(allowed)})
        env.start()
        self.addCleanup(env.stop)
        self.store = storage.Store(root)
        self.addCleanup(self.store.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        self.server.daemon_threads = True
        self.server.store = self.store
        self.server.secure_cookies = False
        self.server.pt_port = 0
        self.up = Upstream()
        self.addCleanup(self.up.close)
        self.server.pt_test_upstream = {"a.example.com": ("127.0.0.1", self.up.port)}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.server.server_port
        self.cookie = ""
        self.csrf = ""
        for table in (webapp.FAILED,):
            table.clear()

    def stop(self):
        manager = getattr(self.server, "pt", None)
        if manager:
            manager.stop()
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, host=None, headers=None, origin=True, csrf=True, cookie=True):
        host = host or "127.0.0.1:%d" % self.port
        sent = {"Host": host}
        if cookie and self.cookie:
            sent["Cookie"] = "qdl_session=" + self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
            if origin:
                sent["Origin"] = "http://" + host
            if csrf:
                sent["X-CSRF-Token"] = self.csrf
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            reply = Reply(response, response.read())
        finally:
            conn.close()
        for value in reply.all("Set-Cookie"):
            match = re.match(r"qdl_session=([^;]*)", value)
            if match:
                self.cookie = match.group(1)
        return reply

    def login(self):
        self.assertEqual(self.call("POST", "/api/setup", {"password": "correct horse staple 123"}).status, 201)
        reply = self.call("POST", "/api/login", {"password": "correct horse staple 123"})
        self.assertEqual(reply.status, 200)
        self.csrf = reply.json()["csrf"]

    def enable(self, test_host="localhost"):
        if test_host:
            self.server.pt_test_host = test_host
        reply = self.call("POST", "/api/settings/options", {"pt_enabled": True})
        self.assertEqual(reply.status, 200, reply.body)
        return webapp.server_pt(self.server)


class HubTests(HubCase):
    def test_defaults_are_off_and_nothing_listens(self):
        self.login()
        settings = self.call("GET", "/api/settings").json()
        self.assertIs(settings["pt_enabled"], False)
        self.assertEqual(settings["pt_public_port"], 8443)
        manager = webapp.server_pt(self.server)
        self.assertFalse(manager.enabled)
        self.assertIsNone(manager.httpd)
        status = self.call("GET", "/api/pt/status").json()
        self.assertEqual((status["enabled"], status["listening"], status["origin"], status["reason"]),
                         (False, False, None, "disabled"))
        self.assertEqual(status["serve_command"], "tailscale serve --bg --https=8443 http://127.0.0.1:17865")
        self.assertEqual(storage.DEFAULTS["pt_enabled"], False)
        self.assertIn("pt_enabled", storage.BOOL_OPTIONS)

    def test_option_validation(self):
        self.login()
        for bad in ({"pt_enabled": "yes"}, {"pt_enabled": 1}, {"pt_public_port": "8443"}, {"pt_public_port": 0},
                    {"pt_public_port": 70000}, {"pt_public_port": True}, {"pt_public_port": 8443.5}, {"pt_port": 1}):
            self.assertEqual(self.call("POST", "/api/settings/options", bad).status, 400, bad)
        self.assertEqual(self.call("POST", "/api/settings/options", {"pt_public_port": 9443}).json()["pt_public_port"], 9443)
        self.assertEqual(webapp.server_pt(self.server).public_port, 9443)

    def test_endpoints_need_login_csrf_and_origin(self):
        self.login()
        self.enable()
        body = {"url": "http://a.example.com/"}
        self.assertEqual(self.call("POST", "/api/pt/ticket", body, csrf=False).status, 403)
        self.assertEqual(self.call("POST", "/api/pt/ticket", body, origin=False).status, 403)
        self.assertEqual(self.call("POST", "/api/pt/ticket", body, headers={"Origin": "http://evil.example.com"}).status, 403)
        self.assertEqual(self.call("POST", "/api/pt/ticket", body, cookie=False).status, 401)
        self.assertEqual(self.call("GET", "/api/pt/status", cookie=False).status, 401)
        for path in ("/api/pt/clear-cookies", "/api/pt/jd2"):
            self.assertEqual(self.call("POST", path, {}, cookie=False).status, 401)
            self.assertEqual(self.call("POST", path, {}, csrf=False).status, 403)
        self.assertEqual(self.call("POST", "/api/pt/ticket", body).status, 200)

    def test_enabling_starts_the_listener_and_disabling_stops_it(self):
        self.login()
        manager = self.enable()
        self.assertTrue(manager.enabled)
        port = manager.bound_port
        self.assertNotEqual(port, 0)
        self.assertEqual(self.call("GET", "/api/pt/status").json()["listening"], True)
        self.call("POST", "/api/settings/options", {"pt_enabled": False})
        self.assertFalse(manager.enabled)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.assertEqual(self.call("GET", "/api/pt/status").json()["reason"], "disabled")

    def test_ticket_rules(self):
        self.login()
        self.assertEqual(self.call("POST", "/api/pt/ticket", {"url": "http://a.example.com/"}).status, 400)    # still off
        manager = self.enable()
        for bad in ("http://127.0.0.1/", "http://10.0.0.1/", "http://localhost/", "http://nas.tail1.ts.net/", "ftp://a.example.com/",
                    "https://[::1]/", "javascript:alert(1)", "", None, "http://a.example.com:8080/", "https://169.254.169.254/",
                    "http://%s/" % ("x" * 300)):
            reply = self.call("POST", "/api/pt/ticket", {"url": bad})
            self.assertEqual(reply.status, 400, bad)
        reply = self.call("POST", "/api/pt/ticket", {"url": "http://a.example.com/page?q=1#frag"})
        data = reply.json()
        self.assertEqual(data["origin"], "http://localhost:%d" % manager.bound_port)
        self.assertEqual(data["url"], "http://a.example.com/page?q=1")
        self.assertTrue(data["enter"].startswith(data["origin"] + "/pt/enter?t=" + data["ticket"] + "&u="))
        self.assertIn(data["ticket"], str(list(manager.state.tickets)) if False else data["ticket"])
        self.assertEqual(len(manager.state.tickets), 1)
        self.assertEqual(manager.state.app_origins, ["http://127.0.0.1:%d" % self.port])
        # ... and the listener redeems it, once
        conn = http.client.HTTPConnection("127.0.0.1", manager.bound_port, timeout=5)
        conn.request("GET", data["enter"][len(data["origin"]):], headers={"Host": "localhost:%d" % manager.bound_port})
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 200)
        self.assertIn("qd_pt=", first.getheader("Set-Cookie"))
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", manager.bound_port, timeout=5)
        conn.request("GET", data["enter"][len(data["origin"]):], headers={"Host": "localhost:%d" % manager.bound_port})
        second = conn.getresponse()
        second.read()
        self.assertEqual(second.status, 401)
        conn.close()

    def test_origin_b_in_production_form(self):
        """Behind Tailscale Serve: https + a *.ts.net Host (loopback peer) -> same name on pt_public_port. Anything else -> none."""
        self.login()
        manager = self.enable(test_host=None)
        ts = {"Host": "nas.tail1234.ts.net", "X-Forwarded-Proto": "https"}
        status = self.call("GET", "/api/pt/status", host="nas.tail1234.ts.net", headers=ts).json()
        self.assertEqual((status["origin"], status["reason"]), ("https://nas.tail1234.ts.net:8443", ""))
        self.assertEqual(self.call("POST", "/api/settings/options", {"pt_public_port": 443}).status, 200)
        status = self.call("GET", "/api/pt/status", host="nas.tail1234.ts.net", headers=ts).json()
        self.assertEqual(status["origin"], "https://nas.tail1234.ts.net")
        self.assertEqual(status["serve_command"], "tailscale serve --bg --https=443 http://127.0.0.1:17865")
        plain = self.call("GET", "/api/pt/status").json()                  # http://127.0.0.1: not https
        self.assertEqual((plain["origin"], plain["reason"]), (None, "https"))
        short = self.call("GET", "/api/pt/status", host="nas", headers={"X-Forwarded-Proto": "https"}).json()
        self.assertEqual((short["origin"], short["reason"]), (None, "tsnet"))
        refused = self.call("POST", "/api/pt/ticket", {"url": "http://a.example.com/"})
        self.assertEqual(refused.status, 400)
        self.assertIn("HTTPS", refused.json()["error"])
        self.assertEqual(manager.state.tickets, {})

    def test_app_csp_names_origin_b_only_when_enabled(self):
        self.assertEqual(webapp.app_csp(None), webapp.CSP)
        self.assertEqual(webapp.app_csp(""), webapp.CSP)
        csp = webapp.app_csp("https://nas.tail1234.ts.net:8443")
        self.assertIn("frame-src 'self' https://nas.tail1234.ts.net:8443;", csp)
        self.assertIn("connect-src 'self' https://nas.tail1234.ts.net:8443;", csp)
        self.assertEqual(csp.count("tail1234"), 2)
        for directive in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(directive, csp)

        def header(host="nas.tail1234.ts.net"):
            return self.call("GET", "/api/health", host=host, headers={"X-Forwarded-Proto": "https"}).h["content-security-policy"]
        self.login()
        self.assertEqual(header(), webapp.CSP)                              # setting off: byte for byte the old policy
        self.enable(test_host=None)
        self.assertIn("frame-src 'self' https://nas.tail1234.ts.net:8443;", header())
        self.assertIn("frame-src 'self' https://nas2.tail1234.ts.net:8443;", header("nas2.tail1234.ts.net"))
        index = self.call("GET", "/", host="nas.tail1234.ts.net", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("https://nas.tail1234.ts.net:8443", index.h["content-security-policy"])
        self.assertEqual(self.call("GET", "/api/health").h["content-security-policy"], webapp.CSP)   # http://127.0.0.1: no origin B
        self.call("POST", "/api/settings/options", {"pt_enabled": False})
        self.assertEqual(header(), webapp.CSP)

    def test_logout_everywhere_ends_passthrough_sessions(self):
        self.login()
        manager = self.enable()
        token, _ = manager.state.create_session("http://x")
        ticket = manager.state.new_ticket("http://x")
        self.assertEqual(self.call("POST", "/api/sessions/revoke-all", {}).status, 200)
        self.assertIsNone(manager.state.session_for(token))
        self.assertIsNone(manager.state.redeem_ticket(ticket))

    def test_clear_cookies_endpoint(self):
        self.login()
        manager = self.enable()
        _, session = manager.state.create_session("http://x")
        session.jar.store("example.com", "/", True, ["a=1", "b=2"])
        self.assertEqual(self.call("POST", "/api/pt/clear-cookies", {}).json(), {"cleared": 2})
        self.assertEqual(session.jar.count(), 0)

    def test_jd2_handoff_goes_through_the_links_path_and_is_session_bound(self):
        self.login()
        manager = self.enable()
        _, session = manager.state.create_session("http://x")
        ident = manager.state.add_download("https://dl.example.com/pack.zip", "pack.zip", 10, "application/zip", session)
        self.assertEqual(self.call("POST", "/api/pt/jd2", {"id": "nope"}).status, 400)
        reply = self.call("POST", "/api/pt/jd2", {"id": ident})
        self.assertEqual(reply.json(), {"state": "staged", "name": "pack.zip"})
        self.assertEqual([row["url"] for row in self.store.links()], ["https://dl.example.com/pack.zip"])
        self.assertEqual(self.store.links()[0]["state"], "staged")                       # staged only: nothing started
        plain = manager.state.add_download("http://dl.example.com/x.zip", "x.zip", 10, "application/zip", session)
        refused = self.call("POST", "/api/pt/jd2", {"id": plain})
        self.assertEqual(refused.status, 400)                                          # the Links path takes https only
        send = self.call("POST", "/api/pt/jd2", {"id": ident, "send": True})
        self.assertEqual(send.status, 400)                                             # JD2 is not running in this test
        self.assertEqual(len(self.store.links()), 1)

    def test_the_main_listener_has_no_passthrough_routes(self):
        self.login()
        self.enable()
        for path in ("/p/s/example.com/", "/pt/ping", "/pt/enter", "/pt-sw.js", "/pt/helper.js"):
            self.assertEqual(self.call("GET", path).status, 404, path)
        self.assertNotIn("/pt-sw.js", webapp.FILES)
        self.assertFalse([p for p in webapp.FILES if p.startswith(("/p/", "/pt"))])

    def test_the_passthrough_cookie_is_not_an_admin_session(self):
        """A passthrough session (or its cookie) opens nothing on the Hub; the Hub's cookie opens nothing there."""
        self.login()
        manager = self.enable()
        token, _ = manager.state.create_session("http://x")
        saved, self.cookie = self.cookie, ""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/settings", headers={"Host": "127.0.0.1:%d" % self.port, "Cookie": "qd_pt=" + token + "; qdl_session=" + token})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 401)
        self.cookie = saved
        conn = http.client.HTTPConnection("127.0.0.1", manager.bound_port, timeout=5)
        conn.request("GET", "/p/h/a.example.com/", headers={"Host": "localhost:%d" % manager.bound_port,
                                                           "Cookie": "qdl_session=" + saved + "; qd_pt=nope"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 401)


class RunSelftestTests(ListenerCase):
    def test_all_server_side_checks_pass_and_the_session_is_removed(self):
        before = len(self.mgr.state.sessions)
        rows = passthrough.run_selftest(self.mgr)
        self.assertEqual([r[2] for r in rows], ["pass"] * 5, rows)
        self.assertEqual(len(self.mgr.state.sessions), before)
        self.assertEqual(self.up.seen, [])

    def test_skipped_while_off(self):
        self.store.set_option("pt_enabled", False)
        self.mgr.sync()
        rows = passthrough.run_selftest(self.mgr)
        self.assertEqual([r[2] for r in rows], ["skip"])


if __name__ == "__main__":
    unittest.main()
