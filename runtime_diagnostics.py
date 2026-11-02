"""No-disk, bounded QnapDownloader diagnostics; no Docker socket access.

Read-only process I/O metrics come from Linux procfs. (The port 3128 loopback preflight was
removed in v0.1.33: the Hub-managed native engine runs its own checks.)
"""
from collections import deque
from datetime import datetime, timezone
import threading
from pathlib import Path

_EVENTS = deque(maxlen=64)
_EVENTS_LOCK = threading.Lock()
_VALID_EVENTS = frozenset(("web_started", "login_failed", "diagnostic_requested"))


def event(kind):
    if kind not in _VALID_EVENTS:
        raise ValueError("unsupported diagnostic event")
    with _EVENTS_LOCK:
        _EVENTS.append({"at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"), "type": kind})


def events():
    with _EVENTS_LOCK:
        return list(_EVENTS)


def process_io(path="/proc/self/io"):
    """Physical IO counters, not logical read/write calls. Never writes."""
    result = {"available": False, "read_bytes": None, "write_bytes": None,
              "cancelled_write_bytes": None}
    try:
        rows = Path(path).read_text(encoding="ascii").splitlines()
        counters = {}
        for line in rows:
            key, sep, value = line.partition(":")
            if sep and key in ("read_bytes", "write_bytes",
                               "cancelled_write_bytes"):
                number = int(value.strip())
                if number < 0:
                    raise ValueError("negative procfs counter")
                counters[key] = number
        if all(key in counters for key in (
                "read_bytes", "write_bytes", "cancelled_write_bytes")):
            result.update(counters, available=True)
    except (OSError, UnicodeError, ValueError):
        pass
    return result


def snapshot():
    return {"process_io": process_io(),
            "events": events(), "local_only": True,
            "log_persistence": False}
