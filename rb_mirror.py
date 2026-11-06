"""NAS browser "mirror" display (v0.1.39): a sanitized, script-free copy of the page.

The Chromium on the NAS renders the page. A fixed serializer, run in an ISOLATED WORLD of that
page (page scripts cannot see or change it), walks the DOM and returns plain HTML text plus the
page's CSS. This module is the Hub-side half: it re-parses and rebuilds that HTML with an
allow-list (`sanitize_html`), rewrites every url() / src to a Hub resource reference
(`rewrite_css`, `TokenMap`), and assembles the document the phone shows in a sandboxed iframe
without scripts. Nothing here talks to the network; the NAS browser fetches resources.

Stdlib only. Python 3.8 compatible.
"""
import base64
import hashlib
import html as htmllib
import ipaddress
import re
import secrets
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

# ------------------------------------------------------------------ limits
MAX_NODES = 30000
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_CSS_BYTES = 2 * 1024 * 1024
MAX_SHEETS = 200
TOKEN_CAP = 500                      # resource references per tab (RAM only, cleared on navigation)
FONT_MAX_BYTES = 2 * 1024 * 1024
IMAGE_MAX_BYTES = 4 * 1024 * 1024    # same as the reader's
INLINE_FONT_MAX = 512 * 1024         # data: fonts of a stylesheet are kept as Hub resources up to this
INLINE_IMAGE_MAX = 200 * 1024        # data: images stay inline up to this
CACHE_TOTAL_BYTES = 48 * 1024 * 1024  # fetched resources, all tabs together (LRU)
RESOURCE_TTL = 600.0                 # v0.1.54: a fetched resource is reused (any page, any tab) for this long
URL_MAX = 2048
RES_PATH = "/api/rb/mirror/res?tab=%s&r=%s"
RES_PATH_HASH = RES_PATH + "&h=%s"
HASH_RE = re.compile(r"[0-9a-f]{12}\Z")
FIRST_EAGER_IMAGES = 2               # v0.1.54: the first images of a page load at once; the rest are lazy
PRELOAD_IMAGES = 3                   # ... and are announced with <link rel=preload> in the document head
PRELOAD_FONTS = 2
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ------------------------------------------------------------------ document shell
DOC_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; "
           "media-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'")
# v0.1.41: no `sandbox` here or on the iframe. WebKit (iPhone Safari) does not run ANY event listener on
# a document whose frame may not run scripts, including the listeners the app attaches from outside,
# so taps never arrived. Scripts stay impossible without it: the Hub strips them and every handler,
# and this policy (header + <meta>) is default-src 'none', which blocks inline, external and eval code.
DOC_CSP_HEADER = DOC_CSP + "; frame-ancestors 'self'"
RES_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'"
OWN_CSS = (
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}"
    ":where(a[data-href]){color:-webkit-link;color:LinkText;text-decoration:underline}"
    "[data-href]{cursor:pointer;-webkit-tap-highlight-color:rgba(10,132,255,.2);-webkit-touch-callout:none;"
    "-webkit-user-select:none;user-select:none}"
    "[data-frag]{cursor:pointer}"
    "img{-webkit-touch-callout:none}"
    ".qdm-ph{display:flex;align-items:center;justify-content:center;box-sizing:border-box;max-width:100%;"
    "background:#f0f1f3;color:#6b7280;border:1px dashed #c3c8cf;border-radius:4px;overflow:hidden;"
    "font:13px/1.4 -apple-system,sans-serif;text-align:center}"
)


def css_for_style(css):
    """CSS text that cannot end its <style> element."""
    return css.replace("</", "<\\/")


_PRELOAD_PATH = re.compile(r"/api/rb/mirror/res\?tab=b[0-9]{1,6}&r=[0-9a-f]{16}(?:&h=[0-9a-f]{12})?\Z")


def preload_links(images=(), fonts=()):
    """<link rel=preload> tags for Hub resource paths (anything else is ignored): the phone starts the first
    images and fonts while it is still parsing the CSS (v0.1.54). CSP allows them (img-src / font-src 'self')."""
    out = []
    for path in list(images)[:PRELOAD_IMAGES]:
        if isinstance(path, str) and _PRELOAD_PATH.match(path):
            out.append('<link rel="preload" as="image" href="%s">' % htmllib.escape(path, quote=True))
    for path in list(fonts)[:PRELOAD_FONTS]:
        if isinstance(path, str) and _PRELOAD_PATH.match(path):
            out.append('<link rel="preload" as="font" crossorigin href="%s">' % htmllib.escape(path, quote=True))
    return "".join(out)


def build_document(css, body, html_attrs="", body_attrs="", preload=""):
    """The full HTML document of a capture (everything already sanitized by the caller)."""
    return ("<!doctype html><html%s><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"%s\">%s"
            "<style id=\"mirrorCss\">%s</style><style id=\"mirrorOwn\">%s</style></head>"
            "<body%s>%s</body></html>") % (
        html_attrs, DOC_CSP, preload, css_for_style(css), OWN_CSS, body_attrs, body)


# ------------------------------------------------------------------ URLs
_DENIED_TLDS = {"local", "localhost", "lan", "internal", "home", "arpa", "onion", "test", "invalid"}


def public_http_url(value):
    """`value` as an absolute http(s) URL with a public host, else None (checked before anything is registered)."""
    if (not isinstance(value, str) or not 1 <= len(value) <= URL_MAX or
            not (value.startswith("https://") or value.startswith("http://")) or
            any(ord(c) < 33 or ord(c) == 127 for c in value)):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port      # raises ValueError for a bad port
    except ValueError:
        return None
    if not host or parts.username is not None or parts.password is not None:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.lower().split(".")
        if len(labels) < 2 or labels[-1] in _DENIED_TLDS:
            return None
        return value
    return value if address.is_global else None


def resolve_url(base, ref):
    """`ref` resolved against `base` as a public http(s) URL without fragment, or None."""
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref or len(ref) > 8 * URL_MAX:
        return None
    try:
        joined = urljoin(base, ref)
    except ValueError:
        return None
    if not joined.startswith(("http://", "https://")):
        return None
    return public_http_url(joined.split("#", 1)[0])


# ------------------------------------------------------------------ resource references
def stable_token(url, kind):
    """The opaque reference of (url, kind): the same everywhere, so the phone's HTTP cache and the Hub's
    resource cache can recognise a resource on any page or tab (v0.1.54). Only URLs registered in a tab's
    table are ever fetched, so a guessable token grants nothing."""
    return hashlib.sha256(("%s\0%s" % (kind, url)).encode("utf-8", "replace")).hexdigest()[:16]


def content_hash(data):
    return hashlib.sha1(data).hexdigest()[:12]


def res_path(tab, token, chash=None):
    """Path of a resource; with the content hash the URL is immutable (Cache-Control: immutable is safe)."""
    if chash and HASH_RE.match(chash):
        return RES_PATH_HASH % (tab, token, chash)
    return RES_PATH % (tab, token)


class TokenMap:
    """Per-tab table: opaque token -> {url, kind} or inline bytes. Lives only in RAM."""

    def __init__(self, cap=TOKEN_CAP):
        self.cap = cap
        self._lock = threading.Lock()
        self._by_token = {}
        self._by_key = {}
        self._order = []

    def __len__(self):
        return len(self._by_token)

    def clear(self):
        with self._lock:
            self._by_token.clear()
            self._by_key.clear()
            del self._order[:]

    def _new_token(self, wanted):
        token = wanted
        while token is None or token in self._by_token:
            token = secrets.token_hex(8)
        return token

    def register(self, url, kind):
        """Token of `url` (kind "img" or "font"), or None when the table is full."""
        key = (url, kind)
        with self._lock:
            token = self._by_key.get(key)
            if token is not None:
                return token
            if len(self._by_token) >= self.cap:
                return None
            token = self._new_token(stable_token(url, kind))
            self._by_token[token] = {"url": url, "kind": kind}
            self._by_key[key] = token
            self._order.append(token)
            return token

    def register_data(self, data, mime, kind="font"):
        key = ("data", content_hash(data), len(data), mime, kind)
        with self._lock:
            token = self._by_key.get(key)
            if token is not None:
                return token
            if len(self._by_token) >= self.cap:
                return None
            token = self._new_token(stable_token("data:" + content_hash(data), kind))
            self._by_token[token] = {"url": None, "kind": kind, "data": data, "mime": mime}
            self._by_key[key] = token
            self._order.append(token)
            return token

    def get(self, token):
        with self._lock:
            return self._by_token.get(token)

    def ordered(self):
        """[(token, kind)] of the URL references in registration order (HTML images come first, then CSS)."""
        with self._lock:
            return [(t, self._by_token[t]["kind"]) for t in self._order
                    if t in self._by_token and self._by_token[t].get("url")]


class ResourceCache:
    """LRU of fetched resources keyed by token (= URL + kind), shared by every tab and page (v0.1.54).

    Entries expire after `ttl` seconds, so a changed image shows up again; the total size is capped.
    `drop_tab` forgets the tab's claim on its entries (an entry nobody claims is freed).
    """

    def __init__(self, cap_bytes=CACHE_TOTAL_BYTES, ttl=RESOURCE_TTL, clock=time.monotonic):
        self.cap = cap_bytes
        self.ttl = ttl
        self._clock = clock
        self.bytes = 0
        self._lock = threading.Lock()
        self._items = OrderedDict()      # token -> [data, mime, chash, expires, owners]

    def _remove(self, token):
        item = self._items.pop(token, None)
        if item is not None:
            self.bytes -= len(item[0])

    def get(self, tab, token):
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return None
            if item[3] <= self._clock():
                self._remove(token)
                return None
            self._items.move_to_end(token)
            item[4].add(tab)
            return item[0], item[1]

    def known_hash(self, token):
        """Content hash of the cached bytes of `token`, or None (not cached / expired)."""
        with self._lock:
            item = self._items.get(token)
            if item is None or item[3] <= self._clock():
                return None
            return item[2]

    def put(self, tab, token, data, mime):
        size = len(data)
        if size > self.cap:
            return
        now = self._clock()
        with self._lock:
            old = self._items.get(token)
            owners = old[4] if old is not None else set()
            self._remove(token)
            owners.add(tab)
            self._items[token] = [data, mime, content_hash(data), now + self.ttl, owners]
            self.bytes += size
            for stale in [t for t, v in self._items.items() if v[3] <= now]:
                self._remove(stale)
            while self.bytes > self.cap and self._items:
                _, dropped = self._items.popitem(last=False)
                self.bytes -= len(dropped[0])

    def drop_tab(self, tab):
        with self._lock:
            for token in list(self._items):
                owners = self._items[token][4]
                owners.discard(tab)
                if not owners:
                    self._remove(token)

    def clear(self):
        with self._lock:
            self._items.clear()
            self.bytes = 0

    def __len__(self):
        return len(self._items)


# ------------------------------------------------------------------ resource content checks
_HTMLISH = re.compile(rb"^\s*(?:<!doctype html|<html|<head|<body|<script)", re.I)
_SVG_HEAD = re.compile(rb"^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*(?:<!doctype svg[^>]*>\s*)?<svg[\s>]", re.I | re.S)
_SVG_TYPES = ("", "image/svg+xml", "text/xml", "application/xml", "application/octet-stream", "text/plain")


def sniff_image(data):
    """MIME type from the magic bytes (raster formats), "image/svg+xml" for an SVG document, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:2] == b"BM" and len(data) > 30:
        return "image/bmp"
    if _SVG_HEAD.match(data[:2048]):
        return "image/svg+xml"
    return None


def sniff_font(data):
    if data[:4] == b"wOFF":
        return "font/woff"
    if data[:4] == b"wOF2":
        return "font/woff2"
    if data[:4] == b"OTTO":
        return "font/otf"
    if data[:4] in (b"\x00\x01\x00\x00", b"true"):
        return "font/ttf"
    return None


class ResourceRefused(ValueError):
    pass


def check_resource(kind, data, content_type=None):
    """(data, mime) of a fetched resource, or ResourceRefused. `kind` is "img" or "font".

    Raster images and fonts are identified by their magic bytes (servers label fonts with all sorts
    of types). SVG is accepted only as an image document and is later served with a locked-down CSP.
    HTML and script are never accepted.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ResourceRefused("空のデータです")
    data = bytes(data)
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared in ("text/html", "application/xhtml+xml", "text/javascript", "application/javascript") or \
            _HTMLISH.match(data[:512]):
        raise ResourceRefused("対応していない形式です")
    if kind == "font":
        if len(data) > FONT_MAX_BYTES:
            raise ResourceRefused("フォントが大きすぎます")
        mime = sniff_font(data)
    else:
        if len(data) > IMAGE_MAX_BYTES:
            raise ResourceRefused("画像が大きすぎます")
        mime = sniff_image(data)
        if mime == "image/svg+xml" and declared not in _SVG_TYPES:
            mime = None
    if mime is None:
        raise ResourceRefused("対応していない形式です")
    return data, mime


# ------------------------------------------------------------------ CSS
_SPECIAL = re.compile(r"""/\*|["'@(){};]|(?<![A-Za-z0-9_\\-])[uU][rR][lL]\(""")
_BAD_DECL = re.compile(r"(?i)(?:^|(?<=[;{}\s]))(?:behavior|-moz-binding)\s*:[^;}]*;?")
_EXPRESSION = re.compile(r"(?i)[a-z-]*\s*:[^;{}]*\bexpression\s*\([^;}]*;?")
_MEDIA_OK = re.compile(r"^[\w\s,:()\-./<>=%]{1,300}$")
_IMPORT = re.compile(r"""(?is)@import\s+(?:url\(\s*)?(?:"([^"]*)"|'([^']*)'|([^\s"')]+))\s*\)?\s*([^;]*);""")
_LOCAL_REF = re.compile(r"#[\w\-:.]{1,100}\Z")
DATA_IMAGE = re.compile(r"data:image/(?:png|jpe?g|gif|webp|avif|svg\+xml|x-icon|bmp)[;,]", re.I)
DATA_FONT = re.compile(r"data:(?:font/[\w.+-]+|application/(?:x-)?font[\w.+-]*|application/vnd\.ms-fontobject|"
                       r"application/octet-stream)[;,]", re.I)
_UNSAFE_INLINE = re.compile(r"[\"'\\\r\n\x00]")


def _css_unescape(text):
    def one(match):
        if match.group(1):
            try:
                return chr(int(match.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return match.group(2)
    return re.sub(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", one, text, flags=re.S)


def inline_data_image(value):
    """`value` when it is a small data: image that may stay inline (CSP img-src allows data:)."""
    if DATA_IMAGE.match(value) and len(value) <= INLINE_IMAGE_MAX and not _UNSAFE_INLINE.search(value):
        return value
    return None


def _data_font(value):
    if not DATA_FONT.match(value) or len(value) > INLINE_FONT_MAX * 4 // 3 + 128:
        return None
    head, _, payload = value.partition(",")
    if ";base64" not in head.lower():
        return None
    try:
        data = base64.b64decode(payload, validate=False)
    except ValueError:
        return None
    mime = sniff_font(data)
    return (data, mime) if mime and len(data) <= INLINE_FONT_MAX else None


_ICON_FONT = re.compile(
    r"(?i)icon|awesome|glyph|symbol|material|fontello|ionicons|dashicons|feather|bootstrap|emoji|linea|themify|"
    r"elegant|et-line|typicons|entypo|octicons|\bmdi\b|\bfa[a-z]?\b|lucide|remix|boxicons|swiper|slick|iconfont")
_FACE_FAMILY = re.compile(r"(?i)font-family\s*:\s*([^;}]+)")


def _block_end(text, start):
    """Index of the `}` that closes the block whose body starts at `start`, or -1 (an @font-face body has no nesting)."""
    quote = ""
    j, n = start, len(text)
    while j < n:
        ch = text[j]
        if quote:
            if ch == "\\":
                j += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "}":
            return j
        j += 1
    return -1


def _icon_face(body):
    """True when an @font-face body declares an icon font (kept even when text web fonts are left out)."""
    m = _FACE_FAMILY.search(body)
    return bool(m and _ICON_FONT.search(m.group(1)))


def rewrite_css(text, base, register, register_data=None, web_fonts=True):
    """CSS with every url() / image-set() string turned into a Hub resource reference.

    `register(url, kind) -> path or None`; `register_data(bytes, mime) -> path or None`.
    v0.1.54: every kept @font-face gets `font-display:swap` (text shows at once in a system font, the web
    font replaces it when it arrives); with `web_fonts=False` text web fonts are removed entirely (icon fonts
    stay), so nothing is fetched for them.
    Comments, @import, @charset and constructs old engines executed (behavior, -moz-binding,
    expression()) are removed. Anything not understood as a URL becomes url("") instead of staying.
    """
    out = []
    n = len(text)
    i = 0
    blocks = []             # one flag per open {…}: is it an @font-face block
    prelude = []            # text since the last ; { }
    parens = []             # one flag per open (: is it an image-set( ?

    def url_for(raw):
        raw = _css_unescape(raw).strip()
        if raw.startswith("#"):
            return 'url("%s")' % raw if _LOCAL_REF.match(raw) else 'url("")'
        font = bool(blocks) and blocks[-1]
        if raw[:5].lower() == "data:":
            if font or DATA_FONT.match(raw):
                got = _data_font(raw)
                path = register_data(got[0], got[1]) if got and register_data else None
                return 'url("%s")' % path if path else 'url("")'
            kept = inline_data_image(raw)
            return 'url("%s")' % kept if kept else 'url("")'
        absolute = resolve_url(base, raw)
        path = register(absolute, "font" if font else "img") if absolute else None
        return 'url("%s")' % path if path else 'url("")'

    def emit(piece):
        out.append(piece)
        prelude.append(piece)

    while i < n:
        m = _SPECIAL.search(text, i)
        if m is None:
            emit(text[i:])
            break
        if m.start() > i:
            emit(text[i:m.start()])
            i = m.start()
        token = m.group()
        if token == "/*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif token in ("'", '"'):
            j = i + 1
            while j < n and text[j] != token:
                j += 2 if text[j] == "\\" else 1
            value = text[i + 1:j]
            i = j + 1
            if parens and parens[-1]:
                emit(url_for(value))
            else:
                emit(token + value + token)
        elif token[:3].lower() == "url":
            j = i + 4
            while j < n and text[j] in " \t\r\n\f":
                j += 1
            if j < n and text[j] in "\"'":
                q = text[j]
                k = j + 1
                while k < n and text[k] != q:
                    k += 2 if text[k] == "\\" else 1
                raw = text[j + 1:k]
                k += 1
            else:
                k = j
                while k < n and text[k] != ")" and text[k] not in " \t\r\n\f":
                    k += 2 if text[k] == "\\" else 1
                raw = text[j:k]
            while k < n and text[k] in " \t\r\n\f":
                k += 1
            if k >= n or text[k] != ")":
                break           # unterminated url(): drop the rest
            emit(url_for(raw))
            i = k + 1
        elif token == "@":
            word = re.match(r"@([A-Za-z-]+)", text[i:i + 40])
            if word and word.group(1).lower() in ("import", "charset"):
                j, quote_char, depth = i, "", 0
                while j < n:
                    ch = text[j]
                    if quote_char:
                        if ch == "\\":
                            j += 1
                        elif ch == quote_char:
                            quote_char = ""
                    elif ch in "\"'":
                        quote_char = ch
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth = max(0, depth - 1)
                    elif ch == ";" and not depth:
                        break
                    j += 1
                i = j + 1
            else:
                emit("@")
                i += 1
        elif token == "(":
            tail = (out[-1] if out else "")[-12:].lower()
            parens.append(tail.endswith("image-set"))
            emit("(")
            i += 1
        elif token == ")":
            if parens:
                parens.pop()
            emit(")")
            i += 1
        elif token == "{":
            face = "".join(prelude).strip().lower().startswith("@font-face")
            if face and not web_fonts:
                end = _block_end(text, i + 1)
                if end >= 0 and not _icon_face(text[i + 1:end]):
                    del out[len(out) - len(prelude):]       # the "@font-face" head already emitted
                    del prelude[:]
                    i = end + 1
                    continue
            blocks.append(face)
            del prelude[:]
            out.append("{")
            i += 1
        elif token == "}":
            if blocks and blocks.pop():
                tail = "".join(out[-8:]).rstrip()
                if tail and tail[-1] not in ";{":
                    out.append(";")
                out.append("font-display:swap;")
            del prelude[:]
            out.append("}")
            i += 1
        else:                   # ;
            del prelude[:]
            out.append(";")
            i += 1
    css = "".join(out)
    low = css.lower()
    if "behavior" in low or "-moz-binding" in low:
        css = _BAD_DECL.sub("", css)
    if "expression" in low:
        css = _EXPRESSION.sub("", css)
    return css


def split_imports(text):
    """[(url, media)] of the @import rules of a stylesheet's text."""
    found = []
    for m in _IMPORT.finditer(text):
        found.append((m.group(1) or m.group(2) or m.group(3), (m.group(4) or "").strip()))
    return found


def media_wrap(css, media):
    """`css` inside an @media block when the sheet has a media condition."""
    media = (media or "").strip()
    if not css or not media or media.lower() == "all":
        return css
    if not _MEDIA_OK.match(media):
        return ""
    return "@media %s{%s}" % (media, css)


# ------------------------------------------------------------------ HTML
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
DROP_WITH_CONTENT = frozenset((
    "script style noscript template iframe frame frameset object embed applet title head base meta link "
    "foreignobject image audio video source track canvas math").split())
HTML_TAGS = frozenset((
    "a abbr address area article aside b bdi bdo big blockquote body br button caption center cite code col "
    "colgroup dd del details dfn div dl dt em fieldset figcaption figure font footer h1 h2 h3 h4 h5 h6 header "
    "hgroup hr i img input ins kbd label legend li main mark menu meter nav ol optgroup option output p picture "
    "pre progress q rp rt ruby s samp section select small span strike strong sub summary sup table tbody td "
    "textarea tfoot th thead time tr tt u ul var wbr").split())
SVG_TAGS = frozenset((
    "svg g path rect circle ellipse line polyline polygon text tspan textpath defs lineargradient radialgradient "
    "stop clippath mask pattern symbol use desc marker").split())
ALLOWED_TAGS = HTML_TAGS | SVG_TAGS
# Never copied from the page whatever the element: links, sources, executable or navigating attributes.
DROP_ATTRS = frozenset((
    "src", "srcset", "sizes", "href", "xlink:href", "srcdoc", "action", "formaction", "ping", "poster",
    "background", "data", "codebase", "manifest", "http-equiv", "content", "target", "rel", "download", "form",
    "list", "autofocus", "contenteditable", "tabindex", "draggable", "integrity", "nonce", "crossorigin",
    "referrerpolicy", "loading", "decoding", "is", "slot", "accesskey", "enterkeyhint", "inputmode",
    "autocomplete", "usemap", "ismap", "dirname", "formmethod", "formtarget", "formenctype", "formnovalidate",
    "capture", "accept", "readonly"))
_ATTR_NAME = re.compile(r"[a-z_][a-z0-9_:.\-]{0,60}\Z")
_BAD_VALUE = re.compile(r"^\s*(?:javascript|vbscript|livescript|data\s*:\s*text/html)", re.I)
_FRAG = re.compile(r"[\w:.%\-]{1,200}\Z")
_SIZE = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,3})?%?\Z")
_SVG_EXTERNAL_URL = re.compile(r"(?i)url\(\s*(?!['\"]?#)")


class MirrorError(ValueError):
    pass


class _Sanitizer(HTMLParser):
    def __init__(self, base, register, register_data=None, limit=MAX_HTML_BYTES * 2):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.register = register
        self.register_data = register_data
        self.out = []
        self.size = 0
        self.limit = limit
        self.stack = []
        self.skip = []               # open drop-with-content elements
        self.after_pre = False
        self.truncated = False
        self.images = 0
        self.first_images = []       # v0.1.54: Hub paths of the first images (preloaded by the document)
        self.links = 0
        self.dropped = 0

    def _put(self, text):
        if self.size > self.limit:
            self.truncated = True
            return
        self.out.append(text)
        self.size += len(text)

    def _style(self, value):
        return rewrite_css(value, self.base, self.register, self.register_data)

    def _attrs(self, tag, attrs):
        parts = []
        seen = set()
        svg = tag in SVG_TAGS
        for name, value in attrs:
            if name in seen or not _ATTR_NAME.match(name):
                continue
            seen.add(name)
            value = value if value is not None else ""
            if name.startswith("on") or name == "xmlns:xlink":
                continue
            if name == "data-m":
                if value.isdigit() and len(value) <= 9:
                    parts.append(' data-m="%s"' % value)
                continue
            if name == "data-href":
                href = public_http_url(value) if len(value) <= URL_MAX else None
                if href:
                    parts.append(' data-href="%s"' % htmllib.escape(href, quote=True))
                    self.links += 1
                continue
            if name == "data-frag":
                if _FRAG.match(value):
                    parts.append(' data-frag="%s"' % htmllib.escape(value, quote=True))
                continue
            if name == "style":
                css = self._style(value)
                if css:
                    parts.append(' style="%s"' % htmllib.escape(css, quote=True))
                continue
            if name in ("href", "xlink:href") and tag == "use" and value.startswith("#"):
                if _FRAG.match(value[1:]):
                    parts.append(' href="%s"' % htmllib.escape(value, quote=True))
                continue
            if name in DROP_ATTRS or _BAD_VALUE.match(value) or len(value) > 2000:
                continue
            if tag == "input" and name == "type" and value.lower() in ("file", "image"):
                value = "text"
            if tag == "button" and name == "type":
                value = "button"
            if svg and name != "id" and _SVG_EXTERNAL_URL.search(value):
                continue
            if name in ("width", "height") and not svg and not _SIZE.match(value.strip()):
                continue
            parts.append(' %s="%s"' % (name, htmllib.escape(value, quote=True)))
        if tag in ("input", "textarea", "select"):
            # Phase 1 is read-only: never raise the phone's keyboard from the copy.
            parts.append(' readonly inputmode="none"')
        return "".join(parts)

    def _img_src(self, attrs):
        for name, value in attrs:
            if name != "src" or not value:
                continue
            if value[:5].lower() == "data:":
                return inline_data_image(value)
            absolute = resolve_url(self.base, value)
            return self.register(absolute, "img") if absolute else None
        return None

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in DROP_WITH_CONTENT and tag not in VOID:
                self.skip.append(tag)
            return
        if tag in DROP_WITH_CONTENT:
            self.dropped += 1
            if tag not in VOID:
                self.skip.append(tag)
            return
        if tag == "form":
            tag = "div"
        elif tag not in ALLOWED_TAGS and "-" not in tag:
            self.dropped += 1
            return           # unknown element: its children stay
        body = self._attrs(tag, attrs)
        if tag == "img":
            path = self._img_src(attrs)
            if path:
                self.images += 1
                body += ' src="%s"' % htmllib.escape(path, quote=True)
                if len(self.first_images) < PRELOAD_IMAGES:
                    self.first_images.append(path)
            # v0.1.54: the first images are what the first screen shows: load them at once (the first with
            # high priority); the rest wait until they are near the screen
            if path and self.images <= FIRST_EAGER_IMAGES:
                body += ' decoding="async"' + (' fetchpriority="high"' if self.images == 1 else "")
            else:
                body += ' loading="lazy" decoding="async"'
        self._put("<%s%s>" % (tag, body))
        if tag in ("pre", "textarea"):
            self.after_pre = True
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag == "form":
            tag = "div"
        if tag in VOID or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self._put("</%s>" % top)
            if top == tag:
                break

    def handle_data(self, data):
        if self.skip or not data:
            return
        if self.after_pre:
            self.after_pre = False
            if data.startswith("\n"):
                data = "\n" + data
        self._put(htmllib.escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def finish(self):
        self.close()
        while self.stack:
            self._put("</%s>" % self.stack.pop())
        return "".join(self.out)


def sanitize_html(raw, base, register, register_data=None):
    """(html, stats): `raw` rebuilt with the allow-list. Only text, allowed elements and safe attributes survive."""
    if not isinstance(raw, str):
        raise MirrorError("ページの内容が不正です")
    parser = _Sanitizer(base, register, register_data)
    try:
        parser.feed(CONTROL.sub("", raw))
        out = parser.finish()
    except (AssertionError, ValueError, RecursionError):
        raise MirrorError("ページの内容を整形できませんでした") from None
    return out, {"images": parser.images, "links": parser.links, "dropped": parser.dropped,
                 "truncated": parser.truncated, "first_images": parser.first_images}


def clean_attr_string(raw, base, register, register_data=None):
    """Attribute string of <html>/<body>, passed through the same allow-list."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 8000:
        return ""
    parser = _Sanitizer(base, register, register_data)
    parser.feed(CONTROL.sub("", "<div%s>" % raw))
    parser.close()
    text = "".join(parser.out)
    if text.startswith("<div") and text.endswith(">"):
        return text[len("<div"):-1]
    return ""


# ------------------------------------------------------------------ page-side scripts
# Run inside an isolated world of the page. The observer counts DOM changes (at most one per
# 800 ms); the Hub polls the counter. Nothing in the page can reach this state.
_OBSERVER_JS = r"""
var S=self.__qdm;
if(!S){S=self.__qdm={n:0,last:0,timer:0,mo:null,ids:new WeakMap(),map:new Map(),next:1,on:true};}
S.on=true;
if(!S.mo){
 try{
  S.mo=new MutationObserver(function(){
   if(!S.on)return;
   var t=Date.now();
   if(t-S.last>=800){S.last=t;S.n++;}
   else if(!S.timer){S.timer=setTimeout(function(){S.timer=0;S.last=Date.now();S.n++;},800-(t-S.last));}
  });
  S.mo.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
 }catch(e){}
}else{
 try{if(S.mo.takeRecords().length){S.n++;}}catch(e){}
}
"""
WATCH_JS = "(function(){" + _OBSERVER_JS + "return S.n;})()"
COUNT_JS = "(function(){var S=self.__qdm;return S&&S.mo?S.n:-1;})()"
OFF_JS = ("(function(){var S=self.__qdm;if(S){S.on=false;S.map.clear();try{if(S.mo){S.mo.disconnect();S.mo=null;}}"
          "catch(e){}}return true;})()")

# Resolves an element id of the last capture and prepares a click: a link answers {link}, anything
# else is scrolled into view and answers the viewport point to click.
CLICK_JS = r"""
(function(m,fx,fy){
var S=self.__qdm;if(!S)return {err:'gone'};
var r=S.map.get(m),el=r?(r.deref?r.deref():r):null;
if(!el||!el.isConnected)return {err:'gone'};
var a=el.closest?el.closest('a[href],area[href]'):null;
// v0.1.42: menu toggles are often links to "#" or to this very page, or carry ARIA/toggle
// markers; the page's own script must handle those, so they are clicked, never navigated.
var toggle=el.closest?el.closest('[aria-haspopup],[aria-expanded],[aria-controls],[data-toggle],[data-bs-toggle],[role=button],summary,button'):null;
if(a&&!(toggle&&a.contains(toggle))&&!(toggle&&toggle.contains(a))){
 try{
  var u=new URL(a.getAttribute('href'),document.baseURI);
  var cur=new URL(location.href);
  var same=u.href.split('#')[0]===cur.href.split('#')[0];
  if(/^https?:$/.test(u.protocol)&&u.href.length<=2048&&!same)return {link:u.href};
 }catch(e){}
}
try{el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});}catch(e){}
var rs=el.getClientRects(),rc=null;
for(var i=0;i<rs.length;i++){if(rs[i].width>0&&rs[i].height>0){rc=rs[i];break;}}
if(!rc)return {err:'hidden'};
var x=Math.max(1,Math.min(window.innerWidth-1,rc.left+rc.width*fx));
var y=Math.max(1,Math.min(window.innerHeight-1,rc.top+rc.height*fy));
return {x:x,y:y};
})(%d,%.4f,%.4f)
"""

# The serializer. Reads the DOM only (it never changes it) and returns plain data.
MIRROR_JS = r"""
(function(P){
__OBSERVER__
var started=Date.now();
var domN=S.n;
if(S.map.size>P.nodes*3)S.map.clear();
var NODE_CAP=P.nodes,BYTE_CAP=P.bytes,CSS_CAP=P.css,TARGET=P.target,SVG_CAP=1500,DEPTH_CAP=256;
var buf=[],bytes=0,nodes=0,trunc=false,ph=0,cssTrunc=false,partial=false,FOLD=0;
// v0.1.54 "above the fold first": on a large page, block elements that start below the first screens are
// sent as empty boxes of their measured height (the layout and the scroll height stay right); the Hub
// follows with the complete copy. Fixed / sticky / positioned content is always kept.
if(P.fold>0&&document.getElementsByTagName('*').length>=P.foldMin)FOLD=window.scrollY+P.fold;
var NONASCII=/[^\x00-\x7f]/;
var SVGNS='http://www.w3.org/2000/svg';
var DROP_TAGS={script:1,noscript:1,template:1,style:1,link:1,meta:1,base:1,title:1,head:1,datalist:1,
 param:1,source:1,track:1,math:1};
var EMBED_TAGS={iframe:1,frame:1,frameset:1,object:1,embed:1,applet:1};
var SKIP_ATTR={srcdoc:1,formaction:1,action:1,ping:1,href:1,src:1,srcset:1,sizes:1,poster:1,background:1,
 'data-m':1,'data-href':1,'data-frag':1,target:1,rel:1,download:1,autofocus:1,contenteditable:1,tabindex:1,
 draggable:1,integrity:1,nonce:1,crossorigin:1,referrerpolicy:1,loading:1,decoding:1,is:1,slot:1,form:1,
 list:1,accesskey:1,enterkeyhint:1,inputmode:1,autocomplete:1,usemap:1,ismap:1,data:1,codebase:1,
 manifest:1,'http-equiv':1,content:1,disabled:1,'xlink:href':1,capture:1,accept:1};
var SVG_OK={svg:1,g:1,path:1,rect:1,circle:1,ellipse:1,line:1,polyline:1,polygon:1,text:1,tspan:1,textpath:1,
 defs:1,lineargradient:1,radialgradient:1,stop:1,clippath:1,mask:1,pattern:1,symbol:1,use:1,desc:1,marker:1};
function cost(s){return NONASCII.test(s)?s.length*3:s.length;}
function push(s){buf.push(s);bytes+=cost(s);if(bytes>BYTE_CAP)trunc=true;}
function esc(s){return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escT(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function idOf(el){
 var id=S.ids.get(el);
 if(!id){id=S.next++;S.ids.set(el,id);}
 S.map.set(id,typeof WeakRef==='function'?new WeakRef(el):el);
 return id;
}
function dm(el){return ' data-m="'+idOf(el)+'"';}
function absUrl(v,keepHash){
 try{
  var u=new URL(v,document.baseURI);
  if(!/^https?:$/.test(u.protocol))return null;
  var h=keepHash?u.href:u.href.split('#')[0];
  return h.length<=2048?h:null;
 }catch(e){return null;}
}
/*PICK*/
function parseSrcset(str,baseW){
 var out=[],s=String(str||''),i=0,n=s.length;
 while(i<n){
  while(i<n&&/[\s,]/.test(s.charAt(i)))i++;
  if(i>=n)break;
  var st=i;while(i<n&&!/\s/.test(s.charAt(i)))i++;
  var url=s.slice(st,i),desc='';
  if(/,+$/.test(url)){url=url.replace(/,+$/,'');}
  else{var ds=i;while(i<n&&s.charAt(i)!==',')i++;desc=s.slice(ds,i).replace(/^\s+|\s+$/g,'');}
  if(!url)continue;
  var w=0,m=/^(\d+(?:\.\d+)?)([wx])$/i.exec(desc);
  if(m){w=parseFloat(m[1]);if(m[2].toLowerCase()==='x')w=baseW>0?w*baseW:0;}
  else if(!desc&&baseW>0)w=baseW;
  out.push({u:url,w:Math.round(w)});
 }
 return out;
}
function pickBest(list,target){
 var known=[],all=[],i;
 for(i=0;i<list.length;i++){if(!list[i]||!list[i].u)continue;all.push(list[i]);if(list[i].w>0)known.push(list[i]);}
 if(!all.length)return null;
 if(!known.length)return all[all.length-1];
 var best=null;
 for(i=0;i<known.length;i++){if(known[i].w>=target&&(!best||known[i].w<best.w))best=known[i];}
 if(best)return best;
 for(i=0;i<known.length;i++){if(!best||known[i].w>best.w)best=known[i];}
 return best;
}
/*ENDPICK*/
function isData(s){return /^data:/i.test(s);}
function pictureSets(el,baseW,list){
 var pic=el.parentElement;
 if(!pic||pic.localName!=='picture')return;
 for(var c=pic.firstElementChild;c;c=c.nextElementSibling){
  if(c.localName!=='source')continue;
  var ty=(c.getAttribute('type')||'').toLowerCase();
  if(ty&&!/^image\/(jpeg|png|webp|avif|gif)$/.test(ty))continue;
  var md=c.getAttribute('media');
  if(md){try{if(!window.matchMedia(md).matches)continue;}catch(e){continue;}}
  var set=parseSrcset(c.getAttribute('srcset')||c.getAttribute('data-srcset'),baseW);
  for(var k=0;k<set.length;k++)list.push(set[k]);
 }
}
function imageUrl(el,rw){
 var dpr=P.dpr||window.devicePixelRatio||1;
 var need=Math.min(Math.max(rw>0?rw:window.innerWidth,1)*dpr,TARGET);
 var cands=[],q;
 pictureSets(el,rw>0?rw:0,cands);
 var a1=parseSrcset(el.getAttribute('srcset'),rw);
 var a2=parseSrcset(el.getAttribute('data-srcset')||el.getAttribute('data-lazy-srcset'),rw);
 for(q=0;q<a1.length;q++)cands.push(a1[q]);
 for(q=0;q<a2.length;q++)cands.push(a2[q]);
 var src=el.currentSrc||el.getAttribute('src')||'';
 if(!src||isData(src)){
  src=el.getAttribute('data-src')||el.getAttribute('data-original')||el.getAttribute('data-lazy-src')||
   el.getAttribute('data-lazy')||(isData(src)?'':src);
 }
 if(src){var nw=el.naturalWidth||0;cands.push({u:src,w:el.currentSrc&&nw>0?nw:0});}
 var good=[];
 for(q=0;q<cands.length;q++){
  var c=cands[q];
  if(!c.u)continue;
  if(isData(c.u)){if(c.u.length<=200000&&/^data:image\//i.test(c.u))good.push(c);continue;}
  var abs=absUrl(c.u,false);
  if(abs)good.push({u:abs,w:c.w});
 }
 var pick=pickBest(good,need);
 return pick?pick.u:null;
}
function sizeAttr(el,name){
 var v=el.getAttribute(name);
 return v&&/^[0-9]{1,5}(\.[0-9]{1,3})?%?$/.test(v.trim())?v.trim():'';
}
function attrString(el,tag,extra,styleExtra){
 var out='',a=el.attributes,styled=false;
 for(var i=0;i<a.length;i++){
  var name=a[i].name,v=a[i].value;
  if(name.charAt(0)==='o'&&name.charAt(1)==='n')continue;
  if(SKIP_ATTR[name])continue;
  if(name==='style'){
   if(v.length>8000)continue;
   styled=true;
   out+=' style="'+esc(styleExtra?v.replace(/;?\s*$/,';')+styleExtra:v)+'"';
   continue;
  }
  if(name.indexOf('data-')===0){if(v.length>256)continue;}
  else if(v.length>2000)continue;
  if(tag==='input'&&(name==='value'||name==='checked'||name==='type'))continue;
  if(tag==='option'&&name==='selected')continue;
  if(tag==='img'&&(name==='width'||name==='height'))continue;
  out+=' '+name+'="'+esc(v)+'"';
 }
 if(styleExtra&&!styled)out+=' style="'+esc(styleExtra)+'"';
 return out+(extra||'');
}
var FOLD_NO={img:1,input:1,textarea:1,select:1,button:1,br:1,hr:1,wbr:1,svg:1,picture:1,video:1,audio:1,canvas:1,
 iframe:1,object:1,embed:1,area:1,col:1,option:1};
function foldBox(el,tag,cs){
 if(FOLD_NO[tag])return null;
 var pos=cs.position;if(pos!=='static'&&pos!=='relative')return null;
 var d=cs.display;
 if(d!=='block'&&d!=='flex'&&d!=='grid'&&d!=='list-item'&&d!=='flow-root'&&d!=='table')return null;
 var r=el.getBoundingClientRect();
 if(r.height<2||r.width<2||r.top+window.scrollY<FOLD)return null;
 return r;
}
function placeholder(el,label,rc){
 var w=Math.round(rc?rc.width:0),h=Math.round(rc?rc.height:0);
 if(w<=0&&h<=0)return;
 nodes++;ph++;
 push('<div class="qdm-ph"'+dm(el)+' style="width:'+Math.min(w,4000)+'px;height:'+Math.min(h,4000)+'px">'+escT(label)+'</div>');
}
function linkAttrs(el){
 var h=el.getAttribute('href');
 if(h===null)return '';
 var tr=h.replace(/^\s+/,'');
 if(tr.charAt(0)==='#'){
  if(tr.length<2)return '';
  var f=tr.slice(1);
  try{f=decodeURIComponent(f);}catch(e){}
  return ' data-frag="'+esc(f.slice(0,200))+'"';
 }
 var abs=absUrl(h,true);
 if(!abs)return '';
 if(abs.indexOf('#')>0&&abs.split('#')[0]===location.href.split('#')[0]){
  var f2=abs.split('#')[1];
  try{f2=decodeURIComponent(f2);}catch(e){}
  return f2?' data-frag="'+esc(f2.slice(0,200))+'"':'';
 }
 return ' data-href="'+esc(abs)+'"';
}
function walkSvg(el,depth,budget,hide){
 var tag=el.localName.toLowerCase();
 if(!SVG_OK[tag]||budget.n++>SVG_CAP)return;
 var out='<'+tag,a=el.attributes;
 for(var i=0;i<a.length;i++){
  var name=a[i].name,v=a[i].value;
  if(name.charAt(0)==='o'&&name.charAt(1)==='n')continue;
  if(name==='href'||name==='xlink:href'){
   if(tag==='use'&&v.charAt(0)==='#'&&v.length<200)out+=' href="'+esc(v)+'"';
   continue;
  }
  if(name==='data-m'||name==='data-href'||v.length>4000)continue;
  if(hide&&depth===0&&name==='style')continue;
  out+=' '+name+'="'+esc(v)+'"';
 }
 if(depth===0){out+=dm(el);if(hide)out+=' style="display:none"';}
 push(out+'>');
 for(var c=el.firstChild;c;c=c.nextSibling){
  if(c.nodeType===1&&c.namespaceURI===SVGNS)walkSvg(c,depth+1,budget,false);
  else if(c.nodeType===3&&(tag==='text'||tag==='tspan'||tag==='textpath'||tag==='desc'))push(escT(c.nodeValue));
 }
 push('</'+tag+'>');
}
function svgRoot(el){
 var hidden=getComputedStyle(el).display==='none';
 if(hidden&&!el.querySelector('symbol,defs,lineargradient,clippath,mask,pattern'))return;
 nodes++;
 var before=buf.length,budget={n:0};
 walkSvg(el,0,budget,hidden);
 if(budget.n>SVG_CAP){buf.length=before;placeholder(el,'図',el.getBoundingClientRect());}
}
function walk(el,depth,pre){
 if(trunc)return;
 if(++nodes>NODE_CAP){trunc=true;return;}
 var tag=el.localName;
 if(el.namespaceURI===SVGNS){if(tag==='svg')svgRoot(el);return;}
 if(el.namespaceURI&&el.namespaceURI!=='http://www.w3.org/1999/xhtml')return;
 tag=tag.toLowerCase();
 if(DROP_TAGS[tag])return;
 var cs=getComputedStyle(el);
 if(cs.display==='none')return;
 if(EMBED_TAGS[tag]){placeholder(el,'埋め込みコンテンツ',el.getBoundingClientRect());return;}
 if(tag==='canvas'){placeholder(el,'キャンバス',el.getBoundingClientRect());return;}
 if(tag==='video'||tag==='audio'){
  var poster=tag==='video'?el.getAttribute('poster'):null,pu=poster?absUrl(poster,false):null;
  var vr=el.getBoundingClientRect();
  if(pu){
   nodes++;
   push('<img'+dm(el)+' src="'+esc(pu)+'" alt="" style="width:'+Math.round(vr.width)+'px;height:'+Math.round(vr.height)+'px;object-fit:cover">');
  }else placeholder(el,tag==='video'?'動画':'音声',vr);
  return;
 }
 if(tag==='input'&&(el.type||'').toLowerCase()==='file'){placeholder(el,'ファイル',el.getBoundingClientRect());return;}
 var otag=tag==='form'?'div':tag;
 if(FOLD>0){
  var fb=foldBox(el,tag,cs);
  if(fb){
   partial=true;
   push('<'+otag+attrString(el,tag,dm(el),'height:'+Math.round(fb.height)+'px;overflow:hidden;box-sizing:border-box')+'></'+otag+'>');
   return;
  }
 }
 var extra=dm(el),styleExtra='';
 if(tag==='a'||tag==='area')extra+=linkAttrs(el);
 if(tag==='img'){
  var rc=el.getBoundingClientRect();
  var url=imageUrl(el,Math.round(rc.width));
  var ws=sizeAttr(el,'width'),hs=sizeAttr(el,'height');
  var nw=el.naturalWidth||0,nh=el.naturalHeight||0;
  if(!(ws&&hs)&&nw>0&&nh>0&&(el.getAttribute('style')||'').indexOf('aspect-ratio')<0)styleExtra='aspect-ratio:'+nw+' / '+nh;
  if(ws)extra+=' width="'+ws+'"';
  if(hs)extra+=' height="'+hs+'"';
  if(url)extra+=' src="'+esc(url)+'"';
  else if(rc.width>0&&rc.height>0){placeholder(el,'',rc);return;}
 }else if(tag==='input'){
  var ty=(el.type||'text').toLowerCase();
  extra+=' type="'+esc(ty)+'"';
  if(ty==='checkbox'||ty==='radio'){if(el.checked)extra+=' checked=""';}
  else if(ty!=='password'&&el.value&&el.value.length<=2000)extra+=' value="'+esc(el.value)+'"';
 }else if(tag==='option'){
  if(el.selected)extra+=' selected=""';
 }else if(tag==='button'){
  extra+=' type="button"';
 }
 push('<'+otag+attrString(el,tag,extra,styleExtra)+'>');
 if(tag==='br'||tag==='hr'||tag==='img'||tag==='input'||tag==='wbr'||tag==='area'||tag==='col')return;
 if(tag==='textarea'){push(escT(el.value||'').slice(0,20000)+'</textarea>');return;}
 if(depth>=DEPTH_CAP){
  var tx=(el.textContent||'').slice(0,2000);
  if(tx)push(escT(tx));
  push('</'+otag+'>');
  return;
 }
 var keep=pre||/^(pre|break-spaces)/.test(cs.whiteSpace||'');
 if(tag==='picture'){
  for(var k=el.firstElementChild;k;k=k.nextElementSibling){if(k.localName==='img')walk(k,depth+1,keep);}
 }else{
  for(var c=el.firstChild;c&&!trunc;c=c.nextSibling){
   if(c.nodeType===1)walk(c,depth+1,keep);
   else if(c.nodeType===3){
    var v=c.nodeValue;
    if(!v)continue;
    if(++nodes>NODE_CAP){trunc=true;break;}
    if(!keep)v=v.replace(/[ \t\r\n\f]+/g,' ');
    push(escT(v));
   }
  }
 }
 push('</'+otag+'>');
}
function rootAttrs(el){
 var s='',a=el.attributes;
 for(var i=0;i<a.length;i++){
  var name=a[i].name,v=a[i].value;
  if(name.charAt(0)==='o'&&name.charAt(1)==='n')continue;
  if(SKIP_ATTR[name]||v.length>2000)continue;
  s+=' '+name+'="'+esc(v)+'"';
 }
 return s;
}
function collectSheets(){
 var out=[],total=0;
 function item(base,media,text,cdp){
  if(out.length>=P.sheets){cssTrunc=true;return;}
  if(text!==null){
   total+=text.length;
   if(total>CSS_CAP){cssTrunc=true;return;}
  }
  out.push({base:base,media:media,text:text,cdp:cdp});
 }
 function addSheet(sh,depth){
  if(!sh||depth>4||out.length>=P.sheets)return;
  try{if(sh.disabled)return;}catch(e){}
  var media='';
  try{media=(sh.media&&sh.media.mediaText)||'';}catch(e){}
  var base=sh.href||document.baseURI;
  var rules=null;
  try{rules=sh.cssRules;}catch(e){rules=null;}
  if(rules===null){
   if(sh.href)item(sh.href,media,null,sh.href);
   return;
  }
  var parts=[];
  for(var i=0;i<rules.length;i++){
   var r=rules[i];
   if(r.type===3){
    if(parts.length){item(base,media,parts.join('\n'),null);parts=[];}
    addSheet(r.styleSheet,depth+1);
   }else parts.push(r.cssText);
  }
  if(parts.length)item(base,media,parts.join('\n'),null);
 }
 try{
  var list=document.styleSheets,i;
  for(i=0;i<list.length;i++)addSheet(list[i],0);
  var ad=document.adoptedStyleSheets||[];
  for(i=0;i<ad.length;i++)addSheet(ad[i],0);
 }catch(e){}
 return out;
}
var html=document.documentElement,body=document.body;
var hattrs=html?rootAttrs(html):'',battrs=body?rootAttrs(body):'';
if(body){
 for(var c=body.firstChild;c&&!trunc;c=c.nextSibling){
  if(c.nodeType===1)walk(c,0,false);
  else if(c.nodeType===3&&/[^ \t\r\n\f]/.test(c.nodeValue)){nodes++;push(escT(c.nodeValue.replace(/[ \t\r\n\f]+/g,' ')));}
 }
}
var sheets=collectSheets();
return {url:location.href,base:document.baseURI,title:String(document.title||'').slice(0,300),html:buf.join(''),
 hattrs:hattrs,battrs:battrs,sheets:sheets,nodes:nodes,bytes:bytes,truncated:trunc,css_truncated:cssTrunc,
 placeholders:ph,domN:domN,partial:partial,ms:Date.now()-started,w:window.innerWidth,dpr:window.devicePixelRatio||1};
})(__PARAMS__)
""".replace("__OBSERVER__", _OBSERVER_JS)


IMAGE_TARGET_PX = 1800      # images are never chosen wider than this (device pixels)


PARTIAL_MIN_ELEMENTS = 1500   # v0.1.54: smaller pages are always copied in full
PARTIAL_SCREENS = 2           # ... larger ones first as far as this many screens down


def serializer_script(dpr=2.0, fold=0):
    """MIRROR_JS with its fixed limits filled in (numbers only: no page or user data goes into the script).
    `fold` (CSS px, 0 = off) is the depth of a first, partial copy."""
    dpr = min(3.0, max(1.0, float(dpr)))
    fold = max(0, min(20000, int(fold)))
    params = ("{nodes:%d,bytes:%d,css:%d,sheets:%d,target:%d,dpr:%.2f,fold:%d,foldMin:%d}" %
              (MAX_NODES, MAX_HTML_BYTES, MAX_CSS_BYTES // 2, MAX_SHEETS, IMAGE_TARGET_PX, dpr, fold,
               PARTIAL_MIN_ELEMENTS))
    return MIRROR_JS.replace("__PARAMS__", params)


def click_script(m, fx=0.5, fy=0.5):
    return CLICK_JS % (int(m), float(fx), float(fy))


# ------------------------------------------------------------------ assembling a capture
class _Sheets:
    def __init__(self, register, register_data, css_text, web_fonts=True):
        self.register = register
        self.register_data = register_data
        self.css_text = css_text
        self.web_fonts = web_fonts


def _sheet_css(text, base, media, depth, ctx):
    """One stylesheet (and its @imports, inlined) as rewritten CSS inside its media condition."""
    pieces = []
    for url, import_media in split_imports(text):
        if depth >= 3:
            break
        target = resolve_url(base, url)
        child = ctx.css_text(target) if target else None
        if isinstance(child, str) and child:
            pieces.append(_sheet_css(child, target, import_media, depth + 1, ctx))
    pieces.append(rewrite_css(text, base, ctx.register, ctx.register_data, ctx.web_fonts))
    return media_wrap("".join(pieces), media)


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


SHEET_CACHE_MAX = 400


def assemble(raw, register, register_data=None, css_text=None, web_fonts=True, cache=None):
    """Hub-side processing of the serializer's output: validate, sanitize, rewrite.

    Returns {url, base, title, html, css, html_attrs, body_attrs, truncated, css_truncated, nodes,
    html_bytes, css_bytes, images, links, sheets, placeholders}. `css_text(url) -> str|None`
    supplies the text of a stylesheet the page could not read itself (cross-origin).
    v0.1.54: `web_fonts=False` leaves text web fonts out; `cache` (a dict kept by the caller for one page)
    remembers the rewritten CSS of a stylesheet by the hash of its text, so a recapture of the same page does
    not parse the same 100 KB again. The result also holds `first_images` (paths to preload) and `partial`.
    """
    if not isinstance(raw, dict):
        raise MirrorError("ページの内容を取得できませんでした")
    url = public_http_url(_text(raw.get("url"), URL_MAX))
    base = public_http_url(_text(raw.get("base"), URL_MAX)) or url
    if url is None or base is None:
        raise MirrorError("このページは表示できません")
    html, stats = sanitize_html(raw.get("html"), base, register, register_data)
    html_attrs = clean_attr_string(raw.get("hattrs"), base, register, register_data)
    body_attrs = clean_attr_string(raw.get("battrs"), base, register, register_data)
    ctx = _Sheets(register, register_data, css_text or (lambda _url: None), web_fonts)
    css_parts, css_bytes, css_trunc = [], 0, raw.get("css_truncated") is True
    sheets = raw.get("sheets") if isinstance(raw.get("sheets"), list) else []
    used = 0
    for sheet in sheets[:MAX_SHEETS]:
        if not isinstance(sheet, dict):
            continue
        sbase = public_http_url(_text(sheet.get("base"), URL_MAX)) or base
        media = _text(sheet.get("media"), 300)
        text = sheet.get("text")
        if text is None and isinstance(sheet.get("cdp"), str):
            text = ctx.css_text(public_http_url(sheet["cdp"]) or "")
        if not isinstance(text, str) or not text:
            continue
        key = None
        if cache is not None:
            key = (hashlib.sha1(text[:MAX_CSS_BYTES].encode("utf-8", "replace")).hexdigest(), sbase, media, web_fonts)
        piece = cache.get(key) if key is not None else None
        if piece is None:
            piece = _sheet_css(text[:MAX_CSS_BYTES], sbase, media, 0, ctx)
            if key is not None:
                if len(cache) >= SHEET_CACHE_MAX:
                    cache.clear()
                cache[key] = piece
        size = len(piece.encode("utf-8", "replace"))
        if css_bytes + size > MAX_CSS_BYTES:
            css_trunc = True
            break
        css_parts.append(piece)
        css_bytes += size
        used += 1
    css = "\n".join(css_parts)
    nodes = raw.get("nodes") if isinstance(raw.get("nodes"), int) and not isinstance(raw.get("nodes"), bool) else 0
    return {
        "url": url, "base": base, "title": CONTROL.sub("", _text(raw.get("title"), 300)).strip(),
        "html": html, "css": css, "html_attrs": html_attrs, "body_attrs": body_attrs,
        "truncated": raw.get("truncated") is True or stats["truncated"], "css_truncated": css_trunc,
        "nodes": max(0, nodes), "html_bytes": len(html.encode("utf-8", "replace")), "css_bytes": css_bytes,
        "images": stats["images"], "links": stats["links"], "sheets": used,
        "placeholders": raw.get("placeholders") if isinstance(raw.get("placeholders"), int) else 0,
        "first_images": stats["first_images"], "partial": raw.get("partial") is True,
    }
