"""Bounded offline Rapidgator URL extraction from user-pasted text or HTML.

No server-side HTTP access, headless browser, script evaluation, redirects,
Cloudflare handling or background download is performed.
"""
from html.parser import HTMLParser
from html import unescape
import hashlib
import re
from urllib.parse import urlsplit

MAX_INPUT = 65536
DEFAULT_LIMIT = 20
HARD_LIMIT = 100
TOKEN = re.compile(r'https://[^\s<>"\x27]+', re.IGNORECASE)
ID = re.compile(r'(?:[a-z0-9]{32}|[0-9]+)\Z', re.IGNORECASE)
HTML_TAG = re.compile(r'<[a-zA-Z][^>]*>', re.DOTALL)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name in ("script", "style", "template"):
            self.suppressed += 1
        if name == "a" and not self.suppressed:
            for key, value in attrs:
                if key == "href" and isinstance(value, str):
                    self.fragments.append(value)

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "template") and self.suppressed:
            self.suppressed -= 1

    def handle_data(self, data):
        if not self.suppressed:
            self.fragments.append(data)


def normalize(candidate):
    if not isinstance(candidate, str) or len(candidate) > 4096:
        return None
    candidate = unescape(candidate.strip().rstrip(".,;:)]}"))
    try:
        url = urlsplit(candidate)
        if (url.scheme.lower() != "https" or
                url.hostname not in ("rapidgator.net", "www.rapidgator.net",
                                     "rg.to", "www.rg.to") or
                url.username or url.password or url.port not in (None, 443)):
            return None
    except ValueError:
        return None
    pieces = url.path.split("/")
    if len(pieces) < 3 or pieces[1].lower() != "file":
        return None
    identifier = pieces[2]
    if not ID.fullmatch(identifier):
        return None
    # Canonical IDs are sufficient for JD2; remove tracking, query,
    # fragment and filename suffix to eliminate duplicate intents.
    return "https://rapidgator.net/file/" + identifier.lower()


def extract(source, limit=DEFAULT_LIMIT):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_INPUT:
        raise ValueError("貼り付け文字列は64KiB以下にしてください")
    if type(limit) is not int or not 1 <= limit <= HARD_LIMIT:
        raise ValueError("最大件数は1〜100の整数にしてください")
    chunks = []
    if HTML_TAG.search(source):
        parser = LinkParser()
        parser.feed(source)
        parser.close()
        chunks = parser.fragments
    else:
        chunks = [source]
    found, seen = [], set()
    for chunk in chunks:
        for match in TOKEN.finditer(unescape(chunk)):
            url = normalize(match.group())
            if url and url not in seen:
                found.append(url)
                seen.add(url)
                if len(found) >= limit:
                    return found
    return found


def digest(source):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_INPUT:
        raise ValueError("貼り付け文字列は64KiB以下にしてください")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
