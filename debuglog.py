"""General-purpose debug log (v0.1.35): an in-memory ring, never written to disk.

Off by default. While off every call is one attribute test. Enabling turns itself off after
AUTO_OFF_SECONDS. Entries: seq, ts, level, category, message (<= 500 chars), optional small JSON
data (<= 2 KB). Secrets are never stored: keys that look like credentials are replaced and URLs
are reduced to scheme://host (a query string or path can identify an account).
"""
import json
import re
import threading
import time
from collections import deque

CATEGORIES = ("ui", "render", "net", "rb", "jd2", "engine", "disk", "http", "app", "pt")
LEVELS = ("debug", "info", "warn", "error")
MAX_ENTRIES = 5000
MAX_BYTES = 2 * 1024 * 1024
MAX_MESSAGE = 500
MAX_DATA_BYTES = 2048
AUTO_OFF_SECONDS = 3600
MAX_BATCH = 100
ENTRY_OVERHEAD = 96

_SECRET_KEY = re.compile(r"password|passwd|token|secret|cookie|authorization|session|api[_-]?key", re.I)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_SECRET_PAIR = re.compile(r"(?i)\b(password|passwd|token|secret|cookie|authorization|session(?:id)?)"
                          r"(\s*[:=]\s*)[^\s,;&]+")
_REDACTED = "[redacted]"


def _url_host(match):
    text = match.group(0)
    scheme, _, rest = text.partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
    return "%s://%s" % (scheme, host)


def clean_text(value, limit=MAX_MESSAGE):
    """A printable single string with credentials removed and URLs reduced to their host."""
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = _URL.sub(_url_host, text)
    text = _BEARER.sub(lambda m: m.group(1) + " " + _REDACTED, text)
    text = _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + _REDACTED, text)
    return text[:limit]


def _clean_value(value, depth):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return clean_text(value, 200)
    if depth >= 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:20]:
            name = clean_text(key, 40)
            out[name] = _REDACTED if _SECRET_KEY.search(str(key)) else _clean_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean_value(item, depth + 1) for item in list(value)[:20]]
    return clean_text(value, 80)


def sanitize_data(data):
    """JSON-safe, secret-free, <= MAX_DATA_BYTES copy of `data` (None stays None)."""
    if data is None:
        return None
    cleaned = _clean_value(data, 0)
    size = len(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_DATA_BYTES:
        return {"_truncated": True, "_bytes": size}
    return cleaned


class DebugLog:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._ring = deque()
        self._bytes = 0
        self._seq = 0
        self._enabled = False
        self._level = 0
        self._categories = frozenset()
        self._until = 0.0
        self._until_wall = 0

    # ---- switch -----------------------------------------------------------------------
    def enable(self, level="debug", categories=None, duration=AUTO_OFF_SECONDS):
        if level not in LEVELS:
            raise ValueError("レベルが不正です")
        chosen = CATEGORIES if categories is None else tuple(categories)
        if not isinstance(categories, (type(None), list, tuple)) or any(c not in CATEGORIES for c in chosen):
            raise ValueError("カテゴリが不正です")
        with self._lock:
            self._level = LEVELS.index(level)
            self._categories = frozenset(chosen)
            duration = max(1, min(int(duration), AUTO_OFF_SECONDS))
            self._until = self._clock() + duration
            self._until_wall = int(self._wall() + duration)
            self._enabled = True

    def disable(self):
        with self._lock:
            self._enabled = False

    def _expired(self):
        if self._enabled and self._clock() >= self._until:
            self._enabled = False
        return not self._enabled

    def is_enabled(self, category=None, level="debug"):
        if not self._enabled or self._expired():
            return False
        if category is not None and category not in self._categories:
            return False
        return LEVELS.index(level) >= self._level if level in LEVELS else False

    def status(self):
        self._expired()
        with self._lock:
            enabled = self._enabled
            remaining = max(0, int(self._until - self._clock())) if enabled else 0
            return {"enabled": enabled, "level": LEVELS[self._level],
                    "categories": [c for c in CATEGORIES if c in self._categories],
                    "until": self._until_wall if enabled else None,
                    "remaining": remaining, "count": len(self._ring), "bytes": self._bytes,
                    "seq": self._seq}

    def client_status(self):
        """What the client needs (stable between changes, so it does not bump the state revision)."""
        full = self.status()
        return {key: full[key] for key in ("enabled", "level", "categories", "until")}

    # ---- writing ----------------------------------------------------------------------
    def log(self, category, level, message, data=None, source="server"):
        """Append one entry when the log is on and the level/category pass; returns its seq or None."""
        if not self._enabled or not self.is_enabled(category, level):
            return None
        text = clean_text(message)
        try:
            payload = sanitize_data(data)
        except (TypeError, ValueError):
            payload = {"_unserializable": True}
        size = ENTRY_OVERHEAD + len(text.encode("utf-8")) + (
            0 if payload is None else len(json.dumps(payload, ensure_ascii=False)))
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": round(self._wall(), 3), "level": level,
                     "category": category, "message": text, "src": source}
            if payload is not None:
                entry["data"] = payload
            self._ring.append((entry, size))
            self._bytes += size
            while len(self._ring) > MAX_ENTRIES or self._bytes > MAX_BYTES:
                _, old = self._ring.popleft()
                self._bytes -= old
            return self._seq

    def log_client(self, items):
        """Validate and append a client batch (<= MAX_BATCH); returns how many were stored."""
        if not self._enabled or not isinstance(items, list):
            return 0
        stored = 0
        for item in items[:MAX_BATCH]:
            if not isinstance(item, dict):
                continue
            category, level, message = item.get("category"), item.get("level"), item.get("message")
            if category not in CATEGORIES or level not in LEVELS or not isinstance(message, str):
                continue
            data = item.get("data")
            if data is not None and not isinstance(data, (dict, list)):
                data = None
            if self.log(category, level, message[:MAX_MESSAGE], data, source="client") is not None:
                stored += 1
        return stored

    # ---- reading ----------------------------------------------------------------------
    def entries(self, since=0, limit=200):
        """Entries with seq > since in ascending order. since=0 returns the newest `limit`."""
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            items = [e for e, _ in self._ring]
            oldest = items[0]["seq"] if items else 0
            if since <= 0:
                chosen = items[-limit:]
            else:
                chosen = [e for e in items if e["seq"] > since][:limit]
            return {"entries": [dict(e) for e in chosen], "seq": self._seq, "oldest": oldest,
                    "gap": since > 0 and oldest > since + 1}

    def clear(self):
        with self._lock:
            self._ring.clear()
            self._bytes = 0


LOG = DebugLog()


def enabled(category=None, level="debug"):
    return LOG.is_enabled(category, level)


def log(category, level, message, data=None):
    if LOG._enabled:
        return LOG.log(category, level, message, data)
    return None
