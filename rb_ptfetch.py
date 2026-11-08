"""v0.1.74: helpers of the NAS browser's passthrough "fetcher" tabs (no CDP here; remote_browser.py drives them).

A site listed under 「NASブラウザ経由」 has its passthrough requests sent through a hidden tab of the NAS Chromium, so they
leave with Chromium's own TLS fingerprint, user agent and cookie jar (the Cloudflare clearance the user got once in a
normal NAS tab applies). This module holds what needs no browser: the limits, the request script, header filters, the
per-request job records and the short-lived store for a redirect's body.
"""
import json
import threading
from urllib.parse import unquote, urlsplit

FETCHER_MAX = 2                    # hidden fetcher tabs at once (the least recently used one is closed for a new site)
FETCHER_IDLE = 300.0               # a fetcher tab nobody used for this long is closed
PER_SITE_MAX = 6                   # requests in flight per site
SLOT_WAIT = 20.0                   # a request waits this long for a free slot
NAV_TIMEOUT = 20.0                 # a document navigation (to DOMContentLoaded)
FETCH_TIMEOUT = 30.0               # any other request
RESPONSE_CAP = 16 * 1024 * 1024    # the most body bytes handed back
DOWNLOAD_SIZE = 20 * 1024 * 1024   # a non-viewable document above this is answered as a download (empty body)
POST_BODY_MAX = 512 * 1024         # the request body travels inside one CDP message (1 MiB limit)
BIG_BODY = 2 * 1024 * 1024         # bodies above this are read one or two at a time (memory of the Hub)
KEEP_TTL = 15.0                    # a redirected document's body is kept this long for the follow-up request
KEEP_MAX = 8
CHALLENGE_RELOAD_GAP = 5.0         # a fetcher tab on a check page is reloaded at most this often
VERDICT_OK_TTL = 60.0              # the guard's answer for a host name is remembered this long (per host)
VERDICT_BLOCKED_TTL = 20.0
VERDICT_MAX = 400
# The fetcher tab loads the real page too (its JavaScript runs on the NAS); what only costs memory is not loaded.
SKIP_TYPES = frozenset(("Image", "Media", "Font", "Stylesheet", "TextTrack", "Ping", "Prefetch"))
OWN_TYPES = frozenset(("XHR", "Fetch"))      # DevTools names our script's requests like a page's own XHR / fetch

# Request headers a page script may set on a fetch() to a URL that needs CORS handling.
CORS_HEADERS = frozenset(("content-type", "accept", "accept-language", "x-requested-with", "x-csrf-token",
                          "x-xsrf-token", "authorization"))
# ... and the only ones a no-cors request may carry.
SIMPLE_HEADERS = frozenset(("accept", "accept-language"))
HEADER_NAME = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_!#$%&'*+.^`|~")


def url_key(url):
    """Comparable form of an http(s) URL: scheme, host, effective port, decoded path and query (no fragment)."""
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return None
    return (scheme, host, port or (80 if scheme == "http" else 443), unquote(parts.path or "/"), unquote(parts.query))


def request_headers(items, cors):
    """The headers (a dict) the request script may set, from the upstream header list the Hub built.

    Only an allow-list is ever passed on: everything the browser owns (Cookie, Host, Origin, Referer, the user agent, Sec-*,
    connection and length headers) stays the browser's. `cors`: a request that goes through CORS may also carry the
    usual XHR headers and any other `x-` header; a no-cors request only Accept and Accept-Language."""
    out = {}
    for name, value in items:
        lowered = name.lower()
        if not lowered or any(ch not in HEADER_NAME for ch in lowered) or "\r" in value or "\n" in value:
            continue
        if lowered.startswith(("x-qd-", "x-forwarded-", "x-real-", "x-tailscale", "x-csrf-token-hub")):
            continue
        if lowered in SIMPLE_HEADERS or (cors and (lowered in CORS_HEADERS or lowered.startswith("x-"))):
            out[lowered] = value
    return out


def fetch_script(job_id, url, method, headers, body_b64, mode, referrer):
    """The expression run in the fetcher tab. Its value says how the request ended; the answer itself is read through
    DevTools (the Fetch domain's response stage), which sees the response whatever CORS allows the page to read."""
    init = {"method": method, "credentials": "include", "redirect": "follow", "cache": "no-store", "mode": mode}
    if headers:
        init["headers"] = headers
    if referrer:
        init["referrer"] = referrer
    if body_b64 is not None:
        init["bodyB64"] = body_b64
    return ("(function(id,url,init){var w=window;if(!w.__qdPt||Object.keys(w.__qdPt).length>200)w.__qdPt={};"
            "var c=new AbortController();w.__qdPt[id]=c;"
            "init.signal=c.signal;"
            "if(init.bodyB64!==undefined){var s=atob(init.bodyB64),a=new Uint8Array(s.length),i=0;"
            "for(;i<s.length;i++)a[i]=s.charCodeAt(i);init.body=a;delete init.bodyB64;}"
            "return fetch(url,init).then(function(r){var t=r.type,v='ok:'+t+':'+r.status;"
            "if(t==='opaque'||t==='opaqueredirect')return v;"
            "return r.arrayBuffer().then(function(){delete w.__qdPt[id];return v},function(){return v});},"
            "function(e){delete w.__qdPt[id];return 'err:'+String(e&&e.name||e)});})"
            "(%d,%s,%s)" % (job_id, json.dumps(url), json.dumps(init)))


def abort_script(job_id):
    return "(function(i){var c=window.__qdPt&&window.__qdPt[i];if(c){c.abort();delete window.__qdPt[i];}})(%d)" % int(job_id)


def response_headers(raw):
    """DevTools' header dict ({name: value}, repeated headers joined with a newline) -> [(name, value)]."""
    out = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            for part in value.split("\n"):
                out.append((name, part))
    return out


# Element loads (img, script, link, media, ...) are no-cors requests in a page; fetch() / XHR ("empty") use CORS.
NO_CORS_DESTS = frozenset(("image", "script", "style", "font", "audio", "video", "track", "object", "embed", "manifest"))


def request_mode(dest, same_origin, custom_headers):
    """"no-cors" for element loads of another origin, else "cors" (same-origin loads are readable either way).

    A no-cors answer of an HTML / JSON / XML type is dropped by Chromium's cross-origin read blocking, which is why
    fetch()/XHR style requests (dest "empty") must go through CORS like they would in the real page."""
    if custom_headers:
        return "cors"
    if same_origin:
        return "cors"
    return "no-cors" if (dest or "").lower() in NO_CORS_DESTS else "cors"


def header_of(items, name, default=""):
    lowered = name.lower()
    for key, value in items:
        if key.lower() == lowered:
            return value
    return default


def is_attachment(items):
    return header_of(items, "content-disposition").lstrip().lower().startswith("attachment")


def content_length(items):
    text = header_of(items, "content-length")
    return int(text) if text.isdigit() and len(text) < 12 else -1


class Job:
    """One request in flight in a fetcher tab (all fields are guarded by the fetcher's condition)."""

    def __init__(self, ident, method, key):
        self.id, self.method, self.key = ident, method, key
        self.net = None            # DevTools request id once the request was seen
        self.status = None
        self.reason = ""
        self.headers = []
        self.url = ""              # the URL of the answer (after redirects)
        self.finished = False
        self.body = b""
        self.error = ""            # "toobig" / "unreadable" / a network error reason
        self.denied = False        # the guard refused a hop


class Doc:
    """The document of the fetcher tab's current navigation."""

    def __init__(self):
        self.net = None
        self.status = None
        self.reason = ""
        self.headers = []
        self.url = ""
        self.finished = False
        self.body = b""
        self.failed = ""
        self.download = False      # attachment, or a non-viewable file above DOWNLOAD_SIZE: no body wanted
        self.at = 0.0              # clock when it finished


class Fetcher:
    def __init__(self, site, now):
        self.site = site
        self.target_id = None
        self.session = None
        self.url = ""              # the page the tab is on (empty: never navigated)
        self.cond = threading.Condition()
        self.navigating = False
        self.inflight = 0
        self.doc = None
        self.dcl = 0               # DOMContentLoaded events seen
        self.jobs = []             # pending Job records
        self.by_net = {}           # DevTools request id -> Job
        self.denied = 0            # hops the guard refused
        self.job_seq = 0
        self.last_used = now
        self.challenge = False     # the tab was on a check page the last time we looked
        self.challenge_at = -1e9
        self.warmed = False        # the tab was taken to the site once before its first non-navigation request
        self.fixture = None        # self-test only: callable(url) -> (status, content type, bytes) for SELFTEST_ORIGIN
        self.closing = False

    @property
    def origin(self):
        parts = urlsplit(self.url) if self.url else None
        return "%s://%s" % (parts.scheme, parts.netloc) if parts and parts.scheme in ("http", "https") else ""


class KeepStore:
    """The body of a redirected navigation, kept for KEEP_TTL so the phone's follow-up request needs no second load."""

    def __init__(self, clock):
        self._clock = clock
        self._items = {}
        self._lock = threading.Lock()

    def put(self, key, value):
        if key is None:
            return
        now = self._clock()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            if len(self._items) >= KEEP_MAX:
                self._items.pop(min(self._items, key=lambda k: self._items[k][0]))
            self._items[key] = (now + KEEP_TTL, value)

    def take(self, key):
        if key is None:
            return None
        now = self._clock()
        with self._lock:
            item = self._items.pop(key, None)
        return item[1] if item and item[0] > now else None

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        with self._lock:
            return len(self._items)
