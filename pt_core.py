"""v0.1.57: passthrough core (pure logic, no sockets of its own except the local-address probe).

The passthrough display renders a page with the phone's own engine while every network request of that page
is made by the Hub (the NAS). This module holds everything that can be tested without a listener:

* the URL mapping  /p/<s|h>/<host>[:port]/<path>?<query>  <->  https://host/path?query
* the SSRF guard (what an upstream request may reach)
* the per-session cookie jar and the first-party rule
* request / response header rewriting
* the HTML injector (puts the helper script at the start of <head> without buffering the page)
* the download decision
* one-time tickets, passthrough sessions, download records (RAM only)
"""
import hashlib
import html
import ipaddress
import re
import secrets
import socket
import threading
import time
import zlib
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

MAX_URL = 4096
DEFAULT_PORTS = {"http": 80, "https": 443}
ALLOWED_PORTS = (80, 443)                  # v0.1.57: nothing else is reached (still public hosts only)
SCHEME_OF = {"s": "https", "h": "http"}
LETTER_OF = {"https": "s", "http": "h"}
LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
PROXY_PATH = re.compile(r"^/p/([sh])/([^/?#]+)((?:/[^?#]*)?)(\?[^#]*)?$")

# Hostnames that never leave the NAS or the tailnet.
DENIED_TLDS = frozenset(("local", "localhost", "lan", "internal", "home", "arpa", "onion", "test",
                         "invalid", "corp", "intranet", "private", "localdomain", "home.arpa"))
SELFTEST_HOST = "pt-selftest.invalid"      # served in-process by the passthrough listener, never resolved

# Sizes and limits (the NAS is a Celeron).
BODY_MAX = 16 * 1024 * 1024                # request body accepted from the phone
HEADER_VALUE_MAX = 8192
HEADER_COUNT_MAX = 48
DOWNLOAD_BYTES = 20 * 1024 * 1024          # a non-viewable file larger than this is offered as a download
TICKET_TTL = 60.0
SESSION_IDLE = 12 * 3600.0
SESSION_ABSOLUTE = 7 * 86400.0
SESSION_MAX = 8
TICKET_MAX = 64
DOWNLOAD_TTL = 600.0
DOWNLOAD_MAX = 32
COOKIE_MAX_TOTAL = 300
COOKIE_MAX_PER_SITE = 60
COOKIE_MAX_BYTES = 4096
COOKIE_HEADER_MAX = 8192

DEFAULT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


class TargetError(ValueError):
    """The URL cannot be mapped (not http/https, IPv6, credentials, malformed)."""


class Blocked(ValueError):
    """The destination is refused by the SSRF guard (private / own / tailnet address or name)."""


class ResolveError(ValueError):
    """The name does not resolve."""


# ------------------------------------------------------------------------------------ URL mapping
class Target(NamedTuple):
    scheme: str
    host: str
    port: int       # 0 = the scheme's default
    path: str       # begins with "/"
    query: str      # without "?"

    @property
    def real_port(self):
        return self.port or DEFAULT_PORTS[self.scheme]

    @property
    def hostport(self):
        return self.host + (":%d" % self.port if self.port else "")

    @property
    def origin(self):
        return "%s://%s" % (self.scheme, self.hostport)

    @property
    def url(self):
        return self.origin + self.path + ("?" + self.query if self.query else "")

    @property
    def request_target(self):
        return self.path + ("?" + self.query if self.query else "")

    @property
    def proxy_path(self):
        return "/p/%s/%s%s" % (LETTER_OF[self.scheme], self.hostport, self.request_target)


def normalize_host(raw):
    """Lower-case ASCII (punycode) host name or dotted IPv4. IPv6 and malformed names raise TargetError."""
    if not isinstance(raw, str) or not raw:
        raise TargetError("ホスト名がありません")
    host = raw.strip().lower().rstrip(".")
    if not host or ":" in host or "[" in host or "]" in host:
        raise TargetError("IPv6アドレスやホストの書式は扱えません")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in host) or "@" in host or "\\" in host or "%" in host:
        raise TargetError("ホスト名が不正です")
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise TargetError("ホスト名が不正です") from None
    if len(host) > 253 or any(LABEL.fullmatch(label) is None for label in host.split(".")):
        raise TargetError("ホスト名が不正です")
    return host


def _escape(value, extra):
    out = []
    for ch in value:
        if ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch in extra:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
        else:
            out.append(ch)
    return "".join(out)


def parse_target(url):
    """Any absolute http(s) URL -> Target (fragment dropped, host punycoded, default port omitted)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise TargetError("URLが不正です")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise TargetError("URLに制御文字は含められません")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise TargetError("URLが不正です") from None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not hostname:
        raise TargetError("httpまたはhttpsのURLだけ開けます")
    if parts.username is not None or parts.password is not None:
        raise TargetError("認証情報つきのURLは開けません")
    if port is not None and not 0 < port < 65536:
        raise TargetError("ポートが不正です")
    host = normalize_host(hostname)
    path = parts.path.replace("\\", "/") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return Target(scheme, host, 0 if port in (None, DEFAULT_PORTS[scheme]) else port,
                  _escape(path, '"<>`{}'), _escape(parts.query, '"<>'))


def encode_target(url):
    """https://host/path?query  ->  /p/s/host/path?query (what the phone requests from origin B)."""
    return parse_target(url).proxy_path


def decode_target(raw_path):
    """/p/s/host[:port]/path?query -> Target. Anything else raises TargetError."""
    match = PROXY_PATH.match(raw_path) if isinstance(raw_path, str) and len(raw_path) <= MAX_URL else None
    if not match:
        raise TargetError("パススルーのURLではありません")
    letter, hostport, path, query = match.groups()
    hostport = unquote(hostport)
    host, sep, port_text = hostport.partition(":")
    port = 0
    if sep:
        if not port_text.isdigit() or len(port_text) > 5 or not 0 < int(port_text) < 65536:
            raise TargetError("ポートが不正です")
        port = int(port_text)
    scheme = SCHEME_OF[letter]
    host = normalize_host(host)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_path):
        raise TargetError("URLに制御文字は含められません")
    if "\\" in path:
        raise TargetError("URLが不正です")
    return Target(scheme, host, 0 if port == DEFAULT_PORTS[scheme] else port,
                  _escape(path or "/", '"<>`{}'), _escape((query or "")[1:], '"<>'))


_SLD = frozenset(("co", "com", "ne", "or", "ac", "go", "org", "net", "gov", "edu", "ad", "gr", "ed", "lg"))


def site_of(host):
    """Approximate registrable domain (no public suffix list in the stdlib): the last two labels, or three for
    country-code second levels such as co.jp / com.au. IP addresses are their own site."""
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels[-1]) == 2 and labels[-2] in _SLD and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def top_site(value):
    """The site named by an X-QD-PT-Top header (a host name, or a full URL), or ''."""
    if not isinstance(value, str) or not value or len(value) > 300:
        return ""
    candidate = value.strip()
    if "/" in candidate:
        try:
            candidate = urlsplit(candidate).hostname or ""
        except ValueError:
            return ""
    try:
        return site_of(normalize_host(candidate.split(":")[0]))
    except TargetError:
        return ""


# ------------------------------------------------------------------------------------ SSRF guard
_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/96", "::1/128", "::/128", "100::/64", "2001::/32", "2001:db8::/32", "fc00::/7", "fe80::/10",
    "fec0::/10", "ff00::/8", "64:ff9b::/96", "2002::/16"))


def ip_blocked(value):
    """Reason text when the address may not be reached, else ''. Understands IPv4-mapped, 6to4 and NAT64 forms."""
    try:
        addr = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) \
            else ipaddress.ip_address(str(value).split("%")[0])
    except ValueError:
        return "不正なIPアドレス"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return ip_blocked(addr.ipv4_mapped)
        packed = addr.packed
        if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):          # NAT64 64:ff9b::/96
            return ip_blocked(ipaddress.IPv4Address(packed[12:])) or "NAT64"
        if packed[:2] == b"\x20\x02":                                        # 6to4: embedded v4
            return ip_blocked(ipaddress.IPv4Address(packed[2:6])) or "6to4"
    for net in _BLOCKED_NETS:
        if addr.version == net.version and addr in net:
            return "非公開アドレス(%s)" % net
    if not addr.is_global:
        return "公開されていないアドレス"
    return ""


_LOCAL_CACHE = {}
_LOCAL_LOCK = threading.Lock()
_BIND_RELIABLE = []


def _bind_probe_reliable():
    """True when binding to an address that is not ours fails (ip_nonlocal_bind=0), so a successful bind means "ours"."""
    if not _BIND_RELIABLE:
        ok = True
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("192.0.2.77", 0))       # TEST-NET-1: never local
                ok = False
        except OSError:
            ok = True
        _BIND_RELIABLE.append(ok)
    return _BIND_RELIABLE[0]


def is_local_address(ip, clock=time.monotonic):
    """True when `ip` is an address of this machine (bind() to it succeeds). Cached for a minute."""
    now = clock()
    with _LOCAL_LOCK:
        hit = _LOCAL_CACHE.get(ip)
        if hit and now - hit[0] < 60:
            return hit[1]
    result = False
    if _bind_probe_reliable():
        try:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.bind((ip, 0))
                result = True
        except OSError:
            result = False
    with _LOCAL_LOCK:
        if len(_LOCAL_CACHE) > 256:
            _LOCAL_CACHE.clear()
        _LOCAL_CACHE[ip] = (now, result)
    return result


def host_blocked_reason(host, own_names=()):
    """Reason text for a host NAME the proxy must not contact, else ''. IP literals are checked as addresses."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return "ホストがありません"
    try:
        return ip_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    if "." not in host:
        return "ドットのない名前(MagicDNS短縮名など)"
    labels = host.split(".")
    last = labels[-1]
    if host == "ts.net" or host.endswith(".ts.net"):
        return "Tailscaleの名前"
    if last in DENIED_TLDS or ".".join(labels[-2:]) in DENIED_TLDS or last == "localhost":
        return "内部用の名前"
    if not re.search(r"[a-z]", last):
        return "数字だけの末尾ラベル(IPの別表記)"
    if host in own_names:
        return "このHub自身の名前"
    return ""


def resolve_public(host, port, resolver=socket.getaddrinfo, own_names=(), local=is_local_address):
    """The addresses to connect to for `host`, all of which passed the guard. The caller connects to one of THESE
    (never resolves again), so a DNS answer that changes between the check and the connect cannot redirect it."""
    reason = host_blocked_reason(host, own_names)
    if reason:
        raise Blocked(reason)
    try:
        ipaddress.ip_address(host)
        found = [host]
    except ValueError:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            raise ResolveError("名前を解決できません") from None
        found = []
        for info in infos:
            ip = info[4][0].split("%")[0]
            if ip not in found:
                found.append(ip)
    if not found:
        raise ResolveError("名前を解決できません")
    for ip in found:
        why = ip_blocked(ip)
        if not why and local(ip):
            why = "このNAS自身のアドレス"
        if why:
            raise Blocked("%s は %s に解決されます" % (host, why))
    found.sort(key=lambda ip: ":" in ip)      # IPv4 first: the NAS may have no IPv6 route
    return found


def check_target(target, own_names=(), resolver=socket.getaddrinfo, local=is_local_address):
    """Scheme/port policy + resolve. Returns addresses. Raises Blocked / ResolveError."""
    if target.real_port not in ALLOWED_PORTS:
        raise Blocked("ポート%dには接続できません" % target.real_port)
    return resolve_public(target.host, target.real_port, resolver, own_names, local)


# ------------------------------------------------------------------------------------ cookies
class Cookie:
    __slots__ = ("name", "value", "domain", "host_only", "path", "secure", "expires", "created")

    def __init__(self, name, value, domain, host_only, path, secure, expires, created):
        self.name, self.value, self.domain, self.host_only = name, value, domain, host_only
        self.path, self.secure, self.expires, self.created = path, secure, expires, created


def _default_path(path):
    if not path.startswith("/") or path.count("/") <= 1:
        return "/"
    return path[:path.rindex("/")] or "/"


def parse_set_cookie(text):
    """(name, value, attrs dict) or None. Attribute names are lower-cased; flags are True."""
    if not isinstance(text, str) or len(text) > COOKIE_MAX_BYTES + 1024:
        return None
    first, *rest = text.split(";")
    name, sep, value = first.partition("=")
    name, value = name.strip(), value.strip()
    if not sep or not name or any(ord(c) < 33 or c in '()<>@,;:\\"/[]?={}' for c in name):
        return None
    if len(name) + len(value) > COOKIE_MAX_BYTES or any(ord(c) < 32 and c != "\t" for c in value):
        return None
    attrs = {}
    for item in rest:
        key, _, val = item.partition("=")
        key = key.strip().lower()
        if key:
            attrs[key] = val.strip() if _ else True
    return name, value.strip('"') if value.startswith('"') and value.endswith('"') and len(value) > 1 else value, attrs


class CookieJar:
    """Cookies set by upstream sites, kept in RAM per passthrough session. Never sent to the phone."""

    def __init__(self, max_total=COOKIE_MAX_TOTAL, max_per_site=COOKIE_MAX_PER_SITE, clock=time.time):
        self.max_total, self.max_per_site, self.clock = max_total, max_per_site, clock
        self.lock = threading.Lock()
        self.by_site = {}          # site -> {(domain, host_only, path, name): Cookie}

    def count(self):
        with self.lock:
            return sum(len(v) for v in self.by_site.values())

    def clear(self):
        with self.lock:
            self.by_site.clear()

    def sites(self):
        with self.lock:
            return sorted(self.by_site)

    def store(self, host, path, https, values):
        """Apply Set-Cookie header values received from `host` for request path `path`. Returns cookies accepted."""
        accepted = 0
        now = self.clock()
        for text in values:
            parsed = parse_set_cookie(text)
            if not parsed:
                continue
            name, value, attrs = parsed
            domain, host_only = host, True
            if "domain" in attrs and attrs["domain"] is not True:
                wanted = str(attrs["domain"]).lower().lstrip(".")
                if not wanted or (host != wanted and not host.endswith("." + wanted)) or site_of(wanted) != site_of(host) \
                        or wanted.count(".") < 1:
                    continue                     # a cookie for somebody else's domain / a public suffix
                domain, host_only = wanted, False
            cpath = attrs.get("path")
            cpath = cpath if isinstance(cpath, str) and cpath.startswith("/") else _default_path(path)
            secure = "secure" in attrs
            if secure and not https:
                continue
            lowered = name.lower()
            if lowered.startswith("__secure-") and not (secure and https):
                continue
            if lowered.startswith("__host-") and not (secure and https and cpath == "/" and host_only):
                continue
            expires = None
            max_age = attrs.get("max-age")
            if isinstance(max_age, str) and re.fullmatch(r"-?\d{1,10}", max_age):
                expires = now + int(max_age)
            elif isinstance(attrs.get("expires"), str):
                try:
                    expires = parsedate_to_datetime(attrs["expires"]).timestamp()
                except (TypeError, ValueError, IndexError, OverflowError):
                    expires = None
            site = site_of(host)
            key = (domain, host_only, cpath, name)
            with self.lock:
                bucket = self.by_site.setdefault(site, {})
                if expires is not None and expires <= now:
                    bucket.pop(key, None)         # a deletion
                    continue
                bucket[key] = Cookie(name, value, domain, host_only, cpath, secure, expires, now)
                accepted += 1
                while len(bucket) > self.max_per_site:
                    oldest = min(bucket, key=lambda k: bucket[k].created)
                    bucket.pop(oldest)
                total = sum(len(v) for v in self.by_site.values())
                while total > self.max_total:
                    site_name, cookies = min(((s, c) for s, c in self.by_site.items() if c),
                                             key=lambda sc: min(x.created for x in sc[1].values()))
                    cookies.pop(min(cookies, key=lambda k: cookies[k].created))
                    total -= 1
        return accepted

    def header_for(self, host, path, https):
        """The Cookie header for a request to `host` + `path`, or ''. Expired cookies are dropped on the way."""
        now = self.clock()
        site = site_of(host)
        chosen = []
        with self.lock:
            bucket = self.by_site.get(site)
            if not bucket:
                return ""
            for key in list(bucket):
                cookie = bucket[key]
                if cookie.expires is not None and cookie.expires <= now:
                    del bucket[key]
                    continue
                if cookie.secure and not https:
                    continue
                if cookie.host_only:
                    if host != cookie.domain:
                        continue
                elif host != cookie.domain and not host.endswith("." + cookie.domain):
                    continue
                cp = cookie.path
                if not (path == cp or (path.startswith(cp) and (cp.endswith("/") or path[len(cp):len(cp) + 1] == "/"))):
                    continue
                chosen.append(cookie)
        chosen.sort(key=lambda c: (-len(c.path), c.created))
        header = "; ".join("%s=%s" % (c.name, c.value) for c in chosen)
        return header[:COOKIE_HEADER_MAX]


# ------------------------------------------------------------------------------------ headers
REQ_FORWARD = frozenset(("accept", "accept-language", "content-type", "range", "if-range", "if-none-match",
                         "if-modified-since", "cache-control", "pragma", "authorization", "x-requested-with"))
REQ_DENY_PREFIX = ("sec-", "proxy-", "x-forwarded-", "x-qd-", "x-real-ip", "x-tailscale", "tailscale-", "cf-", "x-csrf-token-hub")
REQ_DENY = frozenset(("host", "cookie", "cookie2", "connection", "keep-alive", "te", "trailer", "transfer-encoding",
                      "upgrade", "content-length", "expect", "forwarded", "via", "origin", "referer", "user-agent",
                      "accept-encoding", "dnt", "x-csrf-token-hub"))
RES_DROP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade", "content-length", "set-cookie", "set-cookie2",
    "content-security-policy", "content-security-policy-report-only", "x-frame-options",
    "strict-transport-security", "alt-svc", "clear-site-data", "cross-origin-opener-policy",
    "cross-origin-embedder-policy", "cross-origin-resource-policy", "referrer-policy", "link", "public-key-pins",
    "public-key-pins-report-only", "expect-ct", "report-to", "nel", "reporting-endpoints", "content-location",
    "timing-allow-origin", "x-xss-protection", "permissions-policy", "feature-policy", "origin-agent-cluster",
    "document-policy", "x-powered-by", "server", "via", "alt-used", "date"))


def is_navigation(dest, mode="", accept=""):
    """Document-like request (top-level or frame), from the SW-supplied destination or Sec-Fetch-Dest."""
    dest = (dest or "").lower()
    if dest:
        return dest in ("document", "iframe", "frame")
    return (mode or "").lower() == "navigate" or (accept or "").lower().startswith("text/html")


def real_referer(raw_referer, target):
    """The Referer to send upstream: the phone's B-form referrer mapped back to the real page URL, trimmed to its
    origin when it is another site (the browser default policy), or '' when it is not a proxied page."""
    if not raw_referer:
        return ""
    try:
        parts = urlsplit(raw_referer)
        ref = decode_target(parts.path + (("?" + parts.query) if parts.query else ""))
    except (TargetError, ValueError):
        return ""
    if site_of(ref.host) == site_of(target.host) and ref.scheme == target.scheme:
        return ref.url
    if ref.scheme == "https" and target.scheme == "http":
        return ""
    return ref.origin + "/"


def build_upstream_headers(incoming, target, referer, cookie, user_agent, body_length, method):
    """Header list for the upstream request. `incoming` is a list of (name, value) from the phone."""
    out = [("Host", target.hostport), ("User-Agent", user_agent or DEFAULT_UA), ("Accept-Encoding", "gzip, deflate")]
    seen = 0
    for name, value in incoming:
        lowered = name.lower()
        if lowered in REQ_DENY or lowered.startswith(REQ_DENY_PREFIX):
            continue
        if lowered not in REQ_FORWARD and not lowered.startswith("x-"):
            continue
        if len(value) > HEADER_VALUE_MAX or "\r" in value or "\n" in value:
            continue
        seen += 1
        if seen > HEADER_COUNT_MAX:
            break
        out.append((name, value))
    if referer:
        out.append(("Referer", referer))
    if method not in ("GET", "HEAD") or any(n.lower() == "origin" for n, _ in incoming):
        # The origin of the page the phone showed (from its referrer), else the destination itself.
        origin = ""
        if referer:
            ref = urlsplit(referer)
            origin = "%s://%s" % (ref.scheme, ref.netloc)
        out.append(("Origin", origin or target.origin))
    if cookie:
        out.append(("Cookie", cookie))
    if body_length is not None and (method not in ("GET", "HEAD") or body_length):
        out.append(("Content-Length", str(body_length)))
    return out


def rewrite_location(value, base):
    """Location / Refresh target -> B-form path, or None when it cannot be proxied."""
    try:
        return encode_target(urljoin(base.url, value.strip()))
    except (TargetError, ValueError):
        return None


def rewrite_response_headers(items, target, b_origin, page_origin):
    """(headers, set_cookie_values, location_failed) for the phone.

    `items` is the upstream header list. Dropped: CSP, X-Frame-Options, HSTS, COOP/COEP/CORP and friends (they would break
    the rewritten loads); Set-Cookie goes to the jar instead. Location / Refresh are mapped to origin B. A CORS
    allow-origin naming the page's real origin (or *) is answered with origin B.
    """
    out, cookies, failed = [], [], False
    for name, value in items:
        lowered = name.lower()
        if lowered in ("set-cookie", "set-cookie2"):
            cookies.append(value)
            continue
        if lowered in RES_DROP:
            continue
        if lowered == "location":
            mapped = rewrite_location(value, target)
            if mapped is None:
                failed = True
            else:
                out.append(("Location", mapped))
            continue
        if lowered == "refresh":
            match = re.match(r"^\s*(\d+)\s*(?:[;,]\s*url\s*=\s*['\"]?([^'\"]*)['\"]?)?\s*$", value, re.I)
            if match and match.group(2):
                mapped = rewrite_location(match.group(2), target)
                if mapped:
                    out.append(("Refresh", "%s; url=%s" % (match.group(1), mapped)))
            elif match:
                out.append(("Refresh", match.group(1)))
            continue
        if lowered == "access-control-allow-origin":
            if value.strip() == "*" or value.strip().rstrip("/") == (page_origin or "").rstrip("/"):
                out.append((name, b_origin))
            continue
        if lowered.startswith("access-control-") and lowered != "access-control-allow-credentials" and \
                lowered not in ("access-control-expose-headers", "access-control-allow-methods",
                                "access-control-allow-headers", "access-control-max-age"):
            continue
        out.append((name, value))
    return out, cookies, failed


def html_csp(app_origin):
    """CSP header added to proxied HTML: only the app may frame it, forms and frames stay on origin B."""
    ancestors = app_origin or "'none'"
    return ("frame-ancestors %s; form-action 'self'; frame-src 'self'; object-src 'none'" % ancestors)


# ------------------------------------------------------------------------------------ content types
_VIEWABLE_APP = re.compile(r"^application/(?:json|xml|xhtml\+xml|javascript|x-javascript|ecmascript|pdf|wasm|ld\+json|"
                           r"rss\+xml|atom\+xml|manifest\+json|x-www-form-urlencoded|vnd\.apple\.mpegurl|x-mpegurl|"
                           r"dash\+xml|x-shockwave-flash|octet-stream-viewable)$")


def main_type(value):
    return (value or "").split(";")[0].strip().lower()


def is_html(value):
    return main_type(value) in ("text/html", "application/xhtml+xml")


def viewable_type(value):
    kind = main_type(value)
    if not kind:
        return False
    if kind.startswith(("text/", "image/", "audio/", "video/", "font/")):
        return True
    if kind.endswith(("+xml", "+json")):
        return True
    return bool(_VIEWABLE_APP.match(kind))


def filename_from(disposition, target):
    name = ""
    if disposition:
        star = re.search(r"filename\*\s*=\s*([^;]+)", disposition, re.I)
        plain = re.search(r'filename\s*=\s*("([^"]*)"|[^;]*)', disposition, re.I)
        if star:
            raw = star.group(1).strip().strip('"')
            raw = raw.split("'", 2)[-1] if raw.count("'") >= 2 else raw
            name = unquote(raw)
        elif plain:
            name = (plain.group(2) if plain.group(2) is not None else plain.group(1)).strip()
    if not name:
        name = unquote(target.path.rsplit("/", 1)[-1]) or target.host
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", name).strip(" .") or "download"
    return name[:120]


def download_decision(method, status, headers, navigation, target, ranged=False, threshold=DOWNLOAD_BYTES):
    """None, or {"name","size","type"} when a document-like GET answer should become the download page instead of
    being streamed to the phone: Content-Disposition: attachment, or a non-viewable type larger than `threshold`."""
    if method != "GET" or status != 200 or not navigation or ranged:
        return None
    lowered = {k.lower(): v for k, v in headers}
    disposition = lowered.get("content-disposition", "")
    ctype = lowered.get("content-type", "")
    try:
        size = int(lowered.get("content-length", ""))
    except ValueError:
        size = -1
    attachment = bool(re.match(r"\s*attachment\b", disposition, re.I))
    big = size > threshold and not viewable_type(ctype) and not is_html(ctype)
    if not (attachment or big):
        return None
    return {"name": filename_from(disposition, target), "size": size, "type": main_type(ctype) or "application/octet-stream"}


# ------------------------------------------------------------------------------------ HTML injection
def inject_snippet(app_origin, version):
    """Bytes put at the start of <head>: same-origin referrer policy (so every request carries the page address the
    SW and the server map back) and the helper. The app origin is the only place the helper may post to."""
    return ('<meta name="referrer" content="same-origin"><script src="/pt/helper.js?v=%s" data-qd-app="%s"></script>'
            % (html.escape(version, quote=True), html.escape(app_origin or "", quote=True))).encode("ascii", "replace")


_RE_HEAD = re.compile(rb"<head(?=[\s/>])[^>]*>", re.I)
_RE_HTML = re.compile(rb"<html(?=[\s/>])[^>]*>", re.I)
_RE_META_CHARSET = re.compile(rb"<meta\b[^>]*?charset[^>]*>", re.I)


def find_insert_point(buf, final):
    """Index in `buf` where the snippet goes, -1 for "do not inject" (UTF-16), or None for "need more data".

    Never before the doctype (that would switch the page into quirks mode); after a leading BOM; after the <meta
    charset> that sits at the start of <head> (it must stay within the first 1024 bytes for the encoding sniffer)."""
    if buf[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return -1
    n = len(buf)
    pos = 3 if buf[:3] == b"\xef\xbb\xbf" else 0
    fallback = pos            # after the BOM and the doctype
    html_end = None

    def give_up():
        return (html_end if html_end is not None else fallback) if final else None

    while True:
        while pos < n and buf[pos] in b" \t\r\n\x0c":
            pos += 1
        if pos >= n:
            return give_up()
        if buf.startswith(b"<!--", pos):
            end = buf.find(b"-->", pos + 4)
            if end < 0:
                return give_up()
            pos = end + 3
            continue
        if buf.startswith((b"<!", b"<?"), pos):
            end = buf.find(b">", pos)
            if end < 0:
                return give_up()
            if buf[pos:pos + 9].lower() == b"<!doctype":
                fallback = end + 1
            pos = end + 1
            continue
        match = _RE_HTML.match(buf, pos)
        if match:
            html_end = pos = match.end()
            continue
        head = _RE_HEAD.match(buf, pos)
        if head:
            meta = _RE_META_CHARSET.search(buf, head.end())
            if meta and meta.start() < 1024:
                return meta.end()
            if not final and n < 1024:
                return None           # a charset <meta> may still arrive inside the sniffed window
            return head.end()
        if n - pos < 8 and not final:
            return None               # a partial tag name such as "<he"
        return html_end if html_end is not None else fallback


class HtmlInjector:
    """Feeds decoded HTML through, inserting `snippet` at the right place. Buffers at most LIMIT bytes."""
    LIMIT = 8192

    def __init__(self, snippet):
        self.snippet = snippet
        self.buf = b""
        self.done = False

    def _emit(self, point):
        out = self.buf if point < 0 else self.buf[:point] + self.snippet + self.buf[point:]
        self.buf = b""
        self.done = True
        return out

    def feed(self, data):
        if self.done:
            return data
        self.buf += data
        point = find_insert_point(self.buf, False)
        if point is None and len(self.buf) < self.LIMIT:
            return b""
        if point is None:
            point = find_insert_point(self.buf, True)
        return self._emit(point)

    def finish(self):
        if self.done:
            return b""
        return self._emit(find_insert_point(self.buf, True))


class Decoder:
    """Incremental gzip / deflate decoder with a per-call output cap (no memory bombs)."""

    def __init__(self, encoding):
        encoding = (encoding or "").strip().lower()
        if encoding in ("", "identity"):
            self.mode = None
        elif encoding in ("gzip", "x-gzip"):
            self.mode = "gzip"
        elif encoding == "deflate":
            self.mode = "deflate"
        else:
            raise ValueError("未対応のContent-Encoding: " + encoding[:20])
        self.dec = None
        self.pending = b""

    def feed(self, data):
        if self.mode is None:
            yield data
            return
        if self.dec is None:
            if self.mode == "gzip":
                self.dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
            else:
                self.pending += data
                if len(self.pending) < 2:
                    return
                head = self.pending[:2]
                zlib_wrapped = (head[0] & 0x0F) == 8 and ((head[0] << 8) + head[1]) % 31 == 0
                self.dec = zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)
                data, self.pending = self.pending, b""
        chunk = data
        while chunk:
            out = self.dec.decompress(chunk, 1 << 20)
            if out:
                yield out
            chunk = self.dec.unconsumed_tail
            if not chunk:
                break

    def flush(self):
        if self.dec is not None:
            tail = self.dec.flush()
            if tail:
                return tail
        return b""


# ------------------------------------------------------------------------------------ state
def _digest(token):
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


class Session:
    __slots__ = ("created", "last", "app_origin", "jar", "ident")

    def __init__(self, now, app_origin, jar):
        self.created, self.last, self.app_origin, self.jar = now, now, app_origin, jar
        self.ident = secrets.token_hex(6)


class PassthroughState:
    """Tickets, passthrough sessions, download records and the app origins. Everything is RAM only: a Hub restart or
    "log out all devices" forgets it all."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.tickets = {}        # sha256 -> (expires, app_origin)
        self.sessions = {}       # sha256 -> Session
        self.downloads = {}      # id -> dict
        self.app_origins = []    # origins of the app seen in authenticated requests (for CORS of /pt/ping)
        self.own_names = set()   # Host names of this Hub (never reachable through the proxy)

    # tickets
    def new_ticket(self, app_origin):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            for key in [k for k, (exp, _) in self.tickets.items() if exp <= now]:
                del self.tickets[key]
            while len(self.tickets) >= TICKET_MAX:
                del self.tickets[min(self.tickets, key=lambda k: self.tickets[k][0])]
            self.tickets[_digest(token)] = (now + TICKET_TTL, app_origin)
        return token

    def redeem_ticket(self, token):
        """Single use. Returns a (session token, Session) or None."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        with self.lock:
            entry = self.tickets.pop(_digest(token), None)
            if entry is None or entry[0] <= self.clock():
                return None
            return self.create_session(entry[1])

    # sessions
    def create_session(self, app_origin):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self.expire_sessions(now)
            while len(self.sessions) >= SESSION_MAX:
                del self.sessions[min(self.sessions, key=lambda k: self.sessions[k].created)]
            session = Session(now, app_origin, CookieJar(clock=self.clock))
            self.sessions[_digest(token)] = session
        return token, session

    def expire_sessions(self, now=None):
        now = self.clock() if now is None else now
        for key, s in list(self.sessions.items()):
            if now - s.last > SESSION_IDLE or now - s.created > SESSION_ABSOLUTE:
                del self.sessions[key]

    def session_for(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        now = self.clock()
        with self.lock:
            session = self.sessions.get(_digest(token))
            if session is None:
                return None
            if now - session.last > SESSION_IDLE or now - session.created > SESSION_ABSOLUTE:
                del self.sessions[_digest(token)]
                return None
            session.last = now
            return session

    def revoke_all(self):
        with self.lock:
            count = len(self.sessions)
            self.sessions.clear()
            self.tickets.clear()
            self.downloads.clear()
        return count

    def clear_cookies(self):
        with self.lock:
            sessions = list(self.sessions.values())
        total = 0
        for s in sessions:
            total += s.jar.count()
            s.jar.clear()
        return total

    # downloads (the "JD2" / "save" buttons refer to one of these)
    def add_download(self, url, name, size, ctype, session):
        ident = secrets.token_urlsafe(12)
        now = self.clock()
        with self.lock:
            for key in [k for k, d in self.downloads.items() if now - d["created"] > DOWNLOAD_TTL]:
                del self.downloads[key]
            while len(self.downloads) >= DOWNLOAD_MAX:
                del self.downloads[min(self.downloads, key=lambda k: self.downloads[k]["created"])]
            self.downloads[ident] = {"url": url, "name": name, "size": size, "type": ctype, "created": now,
                                     "session": session.ident}
        return ident

    def get_download(self, ident, session=None):
        now = self.clock()
        with self.lock:
            record = self.downloads.get(ident) if isinstance(ident, str) else None
            if record is None or now - record["created"] > DOWNLOAD_TTL:
                return None
            if session is not None and record["session"] != session.ident:
                return None
            return dict(record)

    # app origins / own names
    def note_app_origin(self, origin):
        if not isinstance(origin, str) or not origin or len(origin) > 200:
            return
        with self.lock:
            if origin not in self.app_origins:
                self.app_origins.append(origin)
                del self.app_origins[:-4]

    def note_own_name(self, host):
        if isinstance(host, str) and host and len(self.own_names) < 64:
            self.own_names.add(host.lower().rstrip("."))
