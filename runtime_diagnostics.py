"""No-disk, bounded QnapDownloader diagnostics; no Docker socket access.

Read-only process I/O metrics come from Linux procfs. The optional JD2
local transport preflight only opens a short-lived TCP connection to the
fixed IPv4 loopback port; it does not send a JD2 command or enable an API.
"""
from collections import deque
from datetime import datetime, timezone
import socket
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


def local_jd2_preflight(connector=None):
    """Presence check only; no command, no proof of network isolation.

    Avoids Docker inspect/socket (privilege escalation) and any user-supplied
    destination. No downgrade to unauthenticated API operations occurs.
    """
    socket_fn = connector if connector is not None else socket.create_connection
    try:
        connection = socket_fn(("127.0.0.1", 3128), timeout=0.35)
        try:
            connection.close()
        finally:
            pass
        reachable = True
    except (OSError, TimeoutError):
        reachable = False
    return {
        "transport": "loopback-tcp-preflight",
        "address": "127.0.0.1",
        "port": 3128,
        "tcp_reachable": reachable,
        "api_authenticated": False,
        "network_isolation_verified": False,
        "jd2_identity_verified": False,
        "operations_enabled": False,
        "reason": ("TCP接続のみ確認。APIの認証・LAN隔離・JD2の実体は未検証です"
                   if reachable else
                   "NAS側の127.0.0.1:3128に接続できません。Docker側の設定を確認してください"),
    }


def snapshot():
    return {"process_io": process_io(),
            "events": events(), "local_only": True,
            "log_persistence": False}
