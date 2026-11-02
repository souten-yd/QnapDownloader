"""Remote browser: Chromium in a Hub-managed container, driven over CDP.

docs/container-runtime.md section 4. The phone never talks to the remote
site: the Hub holds a fixed set of Chrome DevTools Protocol commands (no raw
passthrough), streams JPEG screencast frames (latest frame only, in RAM) and
translates a small validated set of inputs. Memory is released aggressively:
a tab close closes its renderer, an idle container is stopped and removed.

Stdlib only (minimal RFC 6455 client below). Python 3.8 compatible. Real
Container Station / Chromium behaviour is unverified (no NAS during
development); everything external is injectable for tests.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import browser_safety
import container_runtime as cr
import rapidgator_extract

MAX_TABS = 3
MIN_AVAILABLE_KIB = cr.BROWSER_MIN_AVAILABLE_KIB     # MemAvailable >= 2 GiB
MEM_LIMIT_PERCENT = 80.0
STATS_TTL = 5.0
VIEWER_STOP_AFTER = 5.0        # stop the screencast after this long without a poll
NO_TAB_IDLE = 300.0            # no tabs for 5 minutes -> stop the container
NO_VIEWER_IDLE = 600.0         # no viewer activity for 10 minutes -> stop
MONITOR_INTERVAL = 5.0
FRAME_WAIT_MAX = 10.0
MIN_FRAME_INTERVAL = 0.1       # at most 10 frames per second
START_TIMEOUT = 45.0
CDP_TIMEOUT = 15.0
WS_MAX_MESSAGE = 8 * 1024 * 1024
WS_MAX_SEND = 1024 * 1024
FRAME_MAX_CHARS = 6 * 1024 * 1024
LINK_CAP = 5000
LINK_RESULT_CAP = rapidgator_extract.HARD_LIMIT
SCREENCAST = {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 2000,
              "everyNthFrame": 1}
DEFAULT_SIZE = (1280, 2000)
SETUP_CACHE_TTL = 30.0
CS_CACHE_TTL = 60.0
WS_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]{1,100}\Z")
TARGET_ID = re.compile(r"[A-Za-z0-9]{8,64}\Z")
TAB_ID = re.compile(r"b[0-9]{1,6}\Z")
BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")

LOG_LINES = 200
TOAST_TAIL_CHARS = 300
UPDATE_NEEDED = "NASブラウザの更新が必要です（初期設定を再実行）"

STEP_DEFS = (
    ("precheck", "Container Stationの検出と事前確認"),
    ("network", "専用ネットワークの作成"),
    ("build", "ブラウザイメージの作成（Chromiumの取得。数分かかります）"),
    ("sandbox", "Chromiumサンドボックスの確認"),
    ("done", "完了"),
)

# key -> (code, Windows virtual key code, text sent with keyDown)
KEYS = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None), "Escape": ("Escape", 27, None),
    "Delete": ("Delete", 46, None),
    "ArrowLeft": ("ArrowLeft", 37, None), "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None), "ArrowDown": ("ArrowDown", 40, None),
}
INPUT_TYPES = ("tap", "scroll", "text", "key", "back", "forward", "reload")
MAX_COORD = 20000.0
MAX_SCROLL = 5000.0
MAX_TEXT = 500

LINKS_JS = (
    "(function(){var o=[],s=new Set(),l=document.querySelectorAll('a[href]');"
    "for(var i=0;i<l.length&&o.length<%d;i++){var h=l[i].href;"
    "if(typeof h==='string'&&h.length<=4096&&!s.has(h)){s.add(h);o.push(h);}}return o;})()"
) % LINK_CAP


class BrowserError(RuntimeError):
    """User-facing (Japanese) reason an operation was refused or failed."""


class SetupBusy(BrowserError):
    pass


class TabGone(BrowserError):
    pass


class WebSocketError(Exception):
    pass


# ------------------------------------------------------------------ RFC 6455
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_OPCODES = (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _xor(data, key):
    size = len(data)
    if not size:
        return b""
    stream = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(size, "big")


def encode_frame(opcode, payload=b"", *, fin=True, mask=True, key=None):
    """One WebSocket frame. Clients MUST mask (RFC 6455 section 5.3)."""
    if opcode not in _OPCODES:
        raise WebSocketError("unknown opcode")
    if len(payload) > WS_MAX_SEND:
        raise WebSocketError("payload too large")
    if opcode >= OP_CLOSE and (not fin or len(payload) > 125):
        raise WebSocketError("control frame too large or fragmented")
    size = len(payload)
    mask_bit = 0x80 if mask else 0
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([mask_bit | size])
    elif size < 65536:
        head += bytes([mask_bit | 126]) + struct.pack(">H", size)
    else:
        head += bytes([mask_bit | 127]) + struct.pack(">Q", size)
    if not mask:
        return head + payload
    key = os.urandom(4) if key is None else key
    if len(key) != 4:
        raise WebSocketError("bad mask key")
    return head + key + _xor(payload, key)


def read_frame(read_exact, max_payload=WS_MAX_MESSAGE, *, allow_masked=False):
    """(fin, opcode, payload). Rejects reserved bits, bad control frames and
    oversize payloads before reading them. Server frames must be unmasked."""
    head = read_exact(2)
    fin = bool(head[0] & 0x80)
    if head[0] & 0x70:
        raise WebSocketError("reserved bits set")
    opcode = head[0] & 0x0F
    if opcode not in _OPCODES:
        raise WebSocketError("unknown opcode")
    masked = bool(head[1] & 0x80)
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack(">H", read_exact(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", read_exact(8))[0]
        if size >> 63:
            raise WebSocketError("bad length")
    if opcode >= OP_CLOSE and (not fin or size > 125):
        raise WebSocketError("bad control frame")
    if masked and not allow_masked:
        raise WebSocketError("server frame is masked")
    if size > max_payload:
        raise WebSocketError("frame too large")
    key = read_exact(4) if masked else b""
    payload = read_exact(size) if size else b""
    if masked:
        payload = _xor(payload, key)
    return fin, opcode, payload


class WebSocket:
    """Client over an already connected socket-like object (sendall/makefile/close)."""

    def __init__(self, sock, max_message=WS_MAX_MESSAGE):
        self._sock = sock
        self._fp = sock.makefile("rb", 65536)
        self._max = max_message
        self._send_lock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, host, port, path, timeout=5.0, create=socket.create_connection,
                rng=os.urandom):
        if not WS_PATH.match(path):
            raise WebSocketError("unexpected websocket path")
        sock = create((host, port), timeout)
        try:
            key = base64.b64encode(rng(16)).decode("ascii")
            sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                          "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode("ascii"))
            fp = sock.makefile("rb", 65536)
            status = fp.readline(8192).decode("latin-1")
            if not status.startswith("HTTP/1.1 101"):
                raise WebSocketError("handshake refused")
            headers = {}
            for _ in range(64):
                line = fp.readline(8192).decode("latin-1")
                if line in ("\r\n", "\n", ""):
                    break
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode()
            if headers.get("sec-websocket-accept") != expected:
                raise WebSocketError("bad accept key")
            sock.settimeout(None)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        # The handshake reader may hold buffered bytes: keep using it.
        ws = cls.__new__(cls)
        ws._sock, ws._fp, ws._max = sock, fp, WS_MAX_MESSAGE
        ws._send_lock, ws.closed = threading.Lock(), False
        return ws

    def _read_exact(self, size):
        data = self._fp.read(size)
        if data is None or len(data) != size:
            raise WebSocketError("connection closed")
        return data

    def _send(self, opcode, payload=b""):
        frame = encode_frame(opcode, payload)
        with self._send_lock:
            self._sock.sendall(frame)

    def send_text(self, text):
        self._send(OP_TEXT, text.encode("utf-8"))

    def recv(self):
        """Next complete text message, or None once the peer closed."""
        parts, total, started = [], 0, False
        while True:
            fin, opcode, payload = read_frame(self._read_exact, self._max)
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send(OP_CLOSE, payload[:2])
                except (OSError, WebSocketError):
                    pass
                self.closed = True
                return None
            if opcode == OP_CONT:
                if not started:
                    raise WebSocketError("unexpected continuation")
            else:
                if started:
                    raise WebSocketError("new message inside a fragmented one")
                if opcode == OP_BINARY:
                    raise WebSocketError("binary messages are not used by CDP")
                started = True
            total += len(payload)
            if total > self._max:
                raise WebSocketError("message too large")
            parts.append(payload)
            if fin:
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise WebSocketError("invalid utf-8") from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send(OP_CLOSE, struct.pack(">H", 1000))
        except (OSError, WebSocketError):
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def fetch_ws_path(host, port, timeout=3.0):
    """Path of the browser websocket from /json/version.

    Only the path is used: the connection always goes to the inspect-verified
    container address, never to a host named inside the response.
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/json/version", headers={"Host": "%s:%d" % (host, port)})
        response = conn.getresponse()
        if response.status != 200:
            raise WebSocketError("version endpoint refused")
        data = json.loads(response.read(65536).decode("utf-8", "replace"))
    except (OSError, ValueError, http.client.HTTPException):
        raise WebSocketError("version endpoint unavailable") from None
    finally:
        conn.close()
    url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    path = urlsplit(url).path if isinstance(url, str) else ""
    if not WS_PATH.match(path):
        raise WebSocketError("unexpected websocket url")
    return path


def default_connect(ip):
    return WebSocket.connect(ip, cr.BROWSER_CDP_PORT, fetch_ws_path(ip, cr.BROWSER_CDP_PORT))


# ----------------------------------------------------------------------- CDP
class CDPError(RuntimeError):
    pass


class CDPConnection:
    """Request/response matching and event dispatch over a websocket-like object
    (send_text / recv / close). Events are handled on the reader thread, which
    therefore must never wait for a response."""

    def __init__(self, ws, on_event, on_close=None):
        self._ws, self._on_event, self._on_close = ws, on_event, on_close
        self._lock = threading.Lock()
        self._next = 1
        self._pending = {}
        self.closed = False
        self._thread = threading.Thread(target=self._reader, daemon=True)

    def start(self):
        self._thread.start()

    def _send(self, method, params, session):
        with self._lock:
            if self.closed:
                raise CDPError("closed")
            ident = self._next
            self._next += 1
        message = {"id": ident, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        return ident, json.dumps(message, separators=(",", ":"))

    def send_nowait(self, method, params=None, session=None):
        ident, text = self._send(method, params, session)
        try:
            self._ws.send_text(text)
        except (OSError, WebSocketError):
            raise CDPError("send failed") from None

    def call(self, method, params=None, session=None, timeout=CDP_TIMEOUT):
        ident, text = self._send(method, params, session)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[ident] = slot
        try:
            self._ws.send_text(text)
        except (OSError, WebSocketError):
            with self._lock:
                self._pending.pop(ident, None)
            raise CDPError("send failed") from None
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(ident, None)
            raise CDPError("timeout")
        message = slot[1]
        if message is None:
            raise CDPError("closed")
        if "error" in message:
            raise CDPError(str((message["error"] or {}).get("message", "error"))[:200])
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _reader(self):
        try:
            while True:
                text = self._ws.recv()
                if text is None:
                    break
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                ident = message.get("id")
                if isinstance(ident, int):
                    with self._lock:
                        slot = self._pending.pop(ident, None)
                    if slot is not None:
                        slot[1] = message
                        slot[0].set()
                elif isinstance(message.get("method"), str):
                    try:
                        self._on_event(message)
                    except Exception:
                        pass     # a bad event must not kill the reader
        except (OSError, WebSocketError):
            pass
        finally:
            with self._lock:
                self.closed = True
                pending, self._pending = list(self._pending.values()), {}
            for slot in pending:
                slot[0].set()
            if self._on_close:
                self._on_close()

    def close(self):
        with self._lock:
            self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------- manager
class Tab:
    def __init__(self, ident, now):
        self.id = ident
        self.target_id = None
        self.session = None
        self.url = ""
        self.title = ""
        self.loading = set()
        self.frame = None          # latest JPEG as a base64 string (RAM only)
        self.seq = 0
        self.size = DEFAULT_SIZE
        self.screencast = False
        self.pending_ack = None
        self.last_ack = 0.0
        self.last_poll = now
        self.pollers = 0
        self.blocked = None


def _scheme_ok(url):
    return (not url or url.startswith("https://") or url == "about:blank" or
            url.startswith("chrome-error://"))


class RemoteBrowser:
    def __init__(self, runtime, context_dir, *, meminfo_path="/proc/meminfo", connect=None,
                 clock=time.monotonic, sleep=time.sleep, monitor=True,
                 frame_interval=MIN_FRAME_INTERVAL, start_timeout=START_TIMEOUT):
        self.runtime = runtime
        self.context_dir = str(context_dir)
        self._meminfo = meminfo_path
        self._connect = connect if connect is not None else default_connect
        self._clock = clock
        self._sleep = sleep
        self._monitor_on = monitor
        self._frame_interval = frame_interval
        self._start_timeout = start_timeout
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._open_lock = threading.Lock()
        self._state = "stopped"
        self._conn = None
        self._tabs = {}
        self._by_session = {}
        self._anchor = None
        self._next_id = 1
        self._last_activity = clock()
        self._no_tabs_since = clock()
        self._stop_evt = threading.Event()
        self._stats_cache = None
        self._tag_cache = False
        self._image_cache = None
        self._cs_cache = None
        self._setup_active = False
        self._steps = self._fresh_steps()
        self._error = None
        self._stale = False
        self._ring = deque(maxlen=LOG_LINES)
        self._ring_lock = threading.Lock()

    # ------------------------------------------------------------- RAM log
    def log(self, text):
        with self._ring_lock:
            for line in str(text).splitlines() or [""]:
                for start in range(0, max(len(line), 1), 500):
                    self._ring.append(line[start:start + 500])

    def log_lines(self):
        with self._ring_lock:
            return list(self._ring)

    # ------------------------------------------------------------- basics
    @staticmethod
    def _fresh_steps():
        return [{"id": i, "label": l, "status": "pending", "detail": "", "progress": None}
                for i, l in STEP_DEFS]

    def _touch(self):
        with self._lock:
            self._last_activity = self._clock()

    def _tag(self):
        if self._tag_cache is False:
            try:
                self._tag_cache = self.runtime.browser_tag(self.context_dir)
            except cr.ContainerError:
                self._tag_cache = None
        return self._tag_cache

    def container_station(self, refresh=False):
        now = self._clock()
        with self._lock:
            cached = self._cs_cache
        if cached is not None and not refresh and now - cached[0] < CS_CACHE_TTL:
            return cached[1]
        version = self.runtime.version()
        with self._lock:
            self._cs_cache = (now, version)
        return version

    def image_ready(self, refresh=False):
        tag = self._tag()
        if tag is None:
            return False
        now = self._clock()
        with self._lock:
            cached = self._image_cache
        if cached is not None and not refresh and now - cached[0] < SETUP_CACHE_TTL:
            return cached[1]
        stale = False
        try:
            ready = self.runtime.browser_image_present(tag)
            if not ready:
                # An older Hub-built image exists: the shipped files changed, so
                # the tag no longer matches and setup must be re-run.
                stale = bool(self.runtime.browser_image_tags())
        except cr.ContainerError:
            ready = False
        with self._lock:
            self._image_cache = (now, ready)
            self._stale = stale and not ready
        return ready

    def _stats(self):
        now = self._clock()
        with self._lock:
            cached = self._stats_cache
        if cached is not None and now - cached[0] < STATS_TTL:
            if cached[2] is not None:
                raise cached[2]
            return cached[1]
        try:
            value, error = self.runtime.stats(cr.BROWSER_NAME), None
        except cr.ContainerError:
            value, error = None, BrowserError(
                "ブラウザコンテナのメモリ使用量を確認できないため開きません")
        with self._lock:
            self._stats_cache = (now, value, error)
        if error is not None:
            raise error
        return value

    def memory_reason(self):
        """Japanese reason a new tab must not be opened now, or None."""
        available = cr.read_mem_available_kib(self._meminfo)
        if available is None:
            return "空きメモリ(MemAvailable)を確認できないため開きません"
        if available < MIN_AVAILABLE_KIB:
            return ("空きメモリが不足しています（MemAvailable %d MiB < 必要 %d MiB）。"
                    "他のアプリを止めずに、開くのを見送りました" % (
                        available // 1024, MIN_AVAILABLE_KIB // 1024))
        with self._lock:
            running = self._state == "running"
        if running:
            try:
                stats = self._stats()
            except BrowserError as error:
                return str(error)
            if stats["percent"] >= MEM_LIMIT_PERCENT:
                return ("NASブラウザのメモリ使用量が上限の%d%%に達しています（%.0f%%）。"
                        "タブを閉じてから開いてください" % (MEM_LIMIT_PERCENT, stats["percent"]))
        return None

    # ------------------------------------------------------------- status
    def status(self):
        station = self.container_station()
        ready = bool(station) and self.image_ready()
        with self._lock:
            stale = self._stale
            state = self._state
            setup = self._setup_active
            tabs = [{"id": t.id, "url": t.url, "title": t.title} for t in self._tabs.values()]
            steps = [dict(s) for s in (self._steps if (setup or self._error or
                     any(s["status"] != "pending" for s in self._steps)) else self._fresh_steps())]
            error = self._error
        memory = None
        if state == "running":
            try:
                stats = self._stats()
                memory = {"used_mib": stats["used_bytes"] // (1024 * 1024),
                          "limit_mib": stats["limit_bytes"] // (1024 * 1024),
                          "percent": stats["percent"]}
            except BrowserError:
                memory = None
        available = cr.read_mem_available_kib(self._meminfo)
        reason = None
        if ready and state in ("stopped", "running"):
            reason = self.memory_reason()
            if reason is None and len(tabs) >= MAX_TABS:
                reason = "NASブラウザのタブは最大%d枚です" % MAX_TABS
        if bool(station) and not ready and stale and not setup:
            reason = UPDATE_NEEDED
        return {
            "container_station": {"available": bool(station), "version": station},
            "image_ready": ready,
            "update_needed": bool(station) and not ready and stale,
            "phase": "setup" if setup else state,
            "setup_running": setup,
            "steps": steps,
            "error": error,
            "tabs": tabs,
            "max_tabs": MAX_TABS,
            "memory": memory,
            "available_mib": None if available is None else available // 1024,
            "can_open": bool(ready and reason is None),
            "reason": reason,
        }

    # -------------------------------------------------------------- setup
    def _step(self, sid, status, detail=None):
        with self._lock:
            for item in self._steps:
                if item["id"] == sid:
                    item["status"] = status
                    if detail is not None:
                        item["detail"] = detail

    def start_setup(self):
        self._begin_setup()
        threading.Thread(target=self._setup_thread, daemon=True).start()

    def run_setup(self):
        self._begin_setup()
        self._setup_thread()

    def _begin_setup(self):
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
            if self._state not in ("stopped",):
                raise BrowserError("NASブラウザを停止してから実行してください")
            self._setup_active = True
            self._error = None
            self._steps = self._fresh_steps()

    def _setup_thread(self):
        try:
            self._execute_setup()
        finally:
            with self._lock:
                self._setup_active = False

    def _execute_setup(self):
        current = "precheck"
        try:
            self._step("precheck", "running")
            version = self.container_station(refresh=True)
            if not version:
                raise BrowserError("Container Stationが見つかりません。"
                                   "App CenterでContainer Stationをインストールしてください")
            tag = self.runtime.browser_tag(self.context_dir)
            self._step("precheck", "done", "Container Station (Docker %s) を検出しました" % version)
            current = "network"
            self._step("network", "running")
            created = self.runtime.network_ensure(cr.BROWSER_NET)
            self._step("network", "done" if created else "skipped",
                       "%s を作成しました（ポート非公開）" % cr.BROWSER_NET if created else "作成済みです")
            current = "build"
            self._step("build", "running", "ビルド中…（同梱のDockerfileのみ・Debian署名付きapt）")
            if self.runtime.browser_image_present(tag):
                self._step("build", "skipped", "作成済みのイメージを使います")
            else:
                self.runtime.browser_build(self.context_dir)
                self._step("build", "done", "イメージ %s を作成しました" % tag)
            self.runtime.browser_prune_old(tag)
            current = "sandbox"
            self._step("sandbox", "running")
            self.runtime.browser_selftest(tag)
            self._step("sandbox", "done", "ユーザー名前空間を確認しました（サンドボックスは有効のまま）")
            current = "done"
            with self._lock:
                self._image_cache = None
            self._step("done", "done", "初期設定が完了しました")
        except (BrowserError, cr.ContainerError) as error:
            self._step(current, "failed", str(error))
            with self._lock:
                self._error = str(error)
        except Exception as error:
            reason = "予期しないエラー: %s" % type(error).__name__
            self._step(current, "failed", reason)
            with self._lock:
                self._error = reason

    # ---------------------------------------------------------- CDP access
    def _call(self, method, params=None, session=None, timeout=CDP_TIMEOUT):
        conn = self._conn
        if conn is None or conn.closed:
            raise BrowserError("NASブラウザが起動していません")
        try:
            return conn.call(method, params, session, timeout)
        except CDPError as error:
            raise BrowserError("ブラウザの操作に失敗しました（%s: %s）" % (method, error)) from None

    # ------------------------------------------------------------ lifecycle
    def _ensure_clean_container(self):
        info = self.runtime.container_state(cr.BROWSER_NAME)
        if not info["exists"]:
            return
        if not info["managed"]:
            raise BrowserError("同名のコンテナがHub管理外のため使用しません")
        self.runtime.stop(cr.BROWSER_NAME, grace=cr.BROWSER_STOP_GRACE)
        self.runtime.remove(cr.BROWSER_NAME)

    def _early_exit_message(self, code):
        """Message for a container that exited early; reads its logs before removal."""
        base = cr.BROWSER_EXIT_MESSAGES.get(code)
        base = ("%s（終了コード %s）" % (base, code) if base else
                "ブラウザコンテナが起動直後に終了しました（終了コード %s）" % code)
        try:
            tail = self.runtime.logs_tail(cr.BROWSER_NAME)
        except cr.ContainerError:
            tail = ""
        self.log(base)
        if not tail:
            return base
        self.log(tail)
        flat = " ".join(tail.split())[-TOAST_TAIL_CHARS:]
        return "%s: %s" % (base, flat)

    def _wait_connect(self):
        deadline = self._clock() + self._start_timeout
        while True:
            info = self.runtime.container_state(cr.BROWSER_NAME)
            if not info["running"]:
                code = info.get("exit_code")
                if code == cr.BROWSER_EXIT_NO_CHROOT:
                    raise BrowserError(cr.NO_CHROOT_MESSAGE)
                if code == cr.BROWSER_EXIT_NO_USERNS:
                    raise BrowserError(cr.NO_USERNS_MESSAGE)
                if code == cr.BROWSER_EXIT_CAPS:
                    raise BrowserError(cr.CAPS_MESSAGE)
                raise BrowserError(self._early_exit_message(code))
            try:
                ip = self.runtime.inspect_ip(cr.BROWSER_NAME, cr.BROWSER_NET)
                return self._connect(ip)
            except (cr.ContainerError, OSError, WebSocketError, ValueError):
                pass
            if self._clock() >= deadline:
                raise BrowserError("ブラウザコンテナに接続できませんでした（時間切れ）")
            self._sleep(1.0)

    def _start(self):
        with self._lock:
            self._state = "starting"
            self._stop_evt = threading.Event()
        try:
            tag = self._tag()
            if tag is None:
                raise BrowserError("ブラウザイメージのビルド用ファイルを確認できません")
            self._ensure_clean_container()
            self.runtime.network_ensure(cr.BROWSER_NET)
            self.runtime.run_browser(tag)
            ws = self._wait_connect()
            conn = CDPConnection(ws, self._on_event, self._on_close)
            conn.start()
            self._conn = conn
            self._call("Target.setDiscoverTargets", {"discover": True})
            # Downloads are never allowed; failing to set this aborts the start.
            self._call("Browser.setDownloadBehavior", {"behavior": "deny"})
            pages = [t for t in self._call("Target.getTargets").get("targetInfos", [])
                     if isinstance(t, dict) and t.get("type") == "page"]
            anchor = pages[0].get("targetId") if pages else None
            if not anchor:
                anchor = self._call("Target.createTarget", {"url": "about:blank",
                                                             "newWindow": True}).get("targetId")
            if not isinstance(anchor, str) or not TARGET_ID.match(anchor):
                raise BrowserError("ブラウザの初期ページを確認できません")
            with self._lock:
                self._anchor = anchor    # keeps the browser alive when the last tab closes
                self._state = "running"
                self._last_activity = self._no_tabs_since = self._clock()
            if self._monitor_on:
                threading.Thread(target=self._monitor, args=(self._stop_evt,), daemon=True).start()
        except Exception as error:
            self._teardown()
            if isinstance(error, (BrowserError,)):
                raise
            if isinstance(error, cr.ContainerError):
                raise BrowserError(str(error)) from None
            raise BrowserError("ブラウザを起動できません: %s" % type(error).__name__) from None

    def _teardown(self):
        conn, self._conn = self._conn, None
        self._stop_evt.set()
        if conn is not None:
            conn.close()
        for action in (lambda: self.runtime.stop(cr.BROWSER_NAME, grace=cr.BROWSER_STOP_GRACE),
                       lambda: self.runtime.remove(cr.BROWSER_NAME)):
            try:
                action()
            except cr.ContainerError:
                pass
        with self._cv:
            self._tabs.clear()
            self._by_session.clear()
            self._anchor = None
            self._state = "stopped"
            self._stats_cache = None
            self._cv.notify_all()

    def stop(self):
        """Close every target, then stop and remove the container (memory freed)."""
        with self._lock:
            if self._state == "stopping":
                return False
            was = self._state
            self._state = "stopping"
            targets = [t.target_id for t in self._tabs.values() if t.target_id]
            self._cv.notify_all()
        if was == "running":
            for target in targets:
                try:
                    self._call("Target.closeTarget", {"targetId": target}, timeout=3.0)
                except BrowserError:
                    pass
        self._teardown()
        return True

    def shutdown(self):
        try:
            self.stop()
        except Exception:
            pass

    def startup_cleanup(self):
        """After a Hub crash a labelled browser container may still hold memory."""
        try:
            if not self.runtime.available():
                return False
            info = self.runtime.container_state(cr.BROWSER_NAME)
            if info["exists"] and info["managed"]:
                self._teardown()
                return True
        except cr.ContainerError:
            pass
        return False

    def _on_close(self):
        with self._cv:
            self._cv.notify_all()

    def _monitor(self, stop_evt):
        while not stop_evt.wait(MONITOR_INTERVAL):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        """Housekeeping (called every few seconds while running)."""
        now = self._clock()
        with self._lock:
            if self._state != "running":
                return
            conn = self._conn
            idle_tabs = not self._tabs and now - self._no_tabs_since >= NO_TAB_IDLE
            idle_view = now - self._last_activity >= NO_VIEWER_IDLE
            lost = conn is None or conn.closed
            expired = [t for t in self._tabs.values()
                       if t.screencast and t.pollers == 0 and now - t.last_poll > VIEWER_STOP_AFTER]
        if lost or idle_tabs or idle_view:
            self.stop()
            return
        for tab in expired:
            self._stop_screencast(tab)

    # ----------------------------------------------------------------- tabs
    def _tab(self, ident):
        if not isinstance(ident, str) or not TAB_ID.match(ident):
            raise BrowserError("タブの指定が不正です")
        with self._lock:
            tab = self._tabs.get(ident)
        if tab is None:
            raise TabGone("このタブはすでに閉じられています")
        return tab

    def open_tab(self, url):
        checked = browser_safety.validate(url)
        with self._open_lock:
            self._touch()
            with self._lock:
                if self._state == "stopping":
                    raise BrowserError("停止処理中です。数秒後にもう一度お試しください")
                if len(self._tabs) >= MAX_TABS:
                    raise BrowserError("NASブラウザのタブは最大%d枚です。不要なタブを閉じてください" % MAX_TABS)
                running = self._state == "running"
            if not running:
                if not self.image_ready(refresh=True):
                    with self._lock:
                        stale = self._stale
                    raise BrowserError(UPDATE_NEEDED if stale else
                                       "NASブラウザの初期設定（イメージ作成）が必要です")
            reason = self.memory_reason()
            if reason:
                raise BrowserError(reason)
            if not running:
                self._start()
            return self._create_tab(checked)

    def _create_tab(self, checked):
        created = self._call("Target.createTarget", {
            "url": "about:blank", "newWindow": True,
            "width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1]})
        target = created.get("targetId")
        if not isinstance(target, str) or not TARGET_ID.match(target):
            raise BrowserError("タブを作成できません")
        with self._lock:
            ident = "b%d" % self._next_id
            self._next_id += 1
            tab = Tab(ident, self._clock())
            tab.target_id = target
            self._tabs[ident] = tab
        try:
            session = self._call("Target.attachToTarget",
                                 {"targetId": target, "flatten": True}).get("sessionId")
            if not isinstance(session, str) or not session:
                raise BrowserError("タブに接続できません")
            with self._lock:
                tab.session = session
                self._by_session[session] = tab
            self._call("Page.enable", session=session)
            self._call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session=session)
            self._call("Page.navigate", {"url": checked}, session=session)
        except BrowserError:
            self._drop_tab(tab, close=True)
            raise
        self._touch()
        return {"id": tab.id, "url": checked}

    def _drop_tab(self, tab, close):
        with self._cv:
            self._tabs.pop(tab.id, None)
            if tab.session:
                self._by_session.pop(tab.session, None)
            if not self._tabs:
                self._no_tabs_since = self._clock()
            tab.frame = None
            self._cv.notify_all()
        if close and tab.target_id:
            try:
                self._call("Target.closeTarget", {"targetId": tab.target_id}, timeout=5.0)
            except BrowserError:
                pass

    def close_tab(self, ident):
        tab = self._tab(ident)
        self._touch()
        self._drop_tab(tab, close=True)
        return True

    def navigate(self, ident, url):
        checked = browser_safety.validate(url)
        tab = self._tab(ident)
        self._touch()
        tab.blocked = None
        self._call("Page.navigate", {"url": checked}, session=tab.session)
        return {"id": tab.id, "url": checked}

    # ------------------------------------------------------------ events
    def _find_by_target(self, target):
        for tab in self._tabs.values():
            if tab.target_id == target:
                return tab
        return None

    def _on_event(self, message):
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        conn = self._conn
        if method == "Page.screencastFrame":
            self._on_frame(message.get("sessionId"), params, conn)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
            self._on_target_info(method, info, conn)
        elif method in ("Target.targetDestroyed", "Target.targetCrashed"):
            with self._cv:
                tab = self._find_by_target(params.get("targetId"))
            if tab is not None:
                self._drop_tab(tab, close=False)
        elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading"):
            with self._cv:
                tab = self._by_session.get(message.get("sessionId"))
                frame_id = params.get("frameId")
                if tab is not None and isinstance(frame_id, str):
                    if method.endswith("StartedLoading"):
                        tab.loading.add(frame_id)
                    else:
                        tab.loading.discard(frame_id)
                    self._cv.notify_all()
        elif method == "Page.javascriptDialogOpening":
            tab = self._by_session.get(message.get("sessionId"))
            if tab is not None and conn is not None:
                # Dialogs would freeze the page: always dismiss, never accept.
                conn.send_nowait("Page.handleJavaScriptDialog", {"accept": False}, session=tab.session)

    def _on_frame(self, session, params, conn):
        data = params.get("data")
        ack = params.get("sessionId")
        with self._cv:
            tab = self._by_session.get(session)
            if tab is None:
                tab = None
            else:
                if (isinstance(data, str) and 0 < len(data) <= FRAME_MAX_CHARS and
                        BASE64.match(data)):
                    tab.frame = data
                    tab.seq += 1
                    meta = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
                    width, height = meta.get("deviceWidth"), meta.get("deviceHeight")
                    if (isinstance(width, (int, float)) and isinstance(height, (int, float)) and
                            0 < width <= 20000 and 0 < height <= 20000):
                        tab.size = (int(width), int(height))
                if isinstance(ack, int):
                    # Acked lazily by the next viewer poll (flow control).
                    tab.pending_ack = ack
                self._cv.notify_all()

    def _on_target_info(self, method, info, conn):
        target = info.get("targetId")
        if info.get("type") != "page" or not isinstance(target, str):
            return
        with self._cv:
            tab = self._find_by_target(target)
            known = tab is not None or target == self._anchor
        if tab is None:
            # A popup opened by a page (window.open / target=_blank): never keep it.
            if not known and info.get("openerId") and conn is not None:
                try:
                    conn.send_nowait("Target.closeTarget", {"targetId": target})
                except CDPError:
                    pass
            return
        url = info.get("url") if isinstance(info.get("url"), str) else ""
        title = info.get("title") if isinstance(info.get("title"), str) else ""
        block = not _scheme_ok(url)
        with self._cv:
            tab.url = url[:2048] if not block else tab.url
            tab.title = title[:300]
            if block:
                tab.blocked = "https以外のアドレスはブロックしました"
            self._cv.notify_all()
        if block and conn is not None and tab.session:
            try:
                conn.send_nowait("Page.navigate", {"url": "about:blank"}, session=tab.session)
            except CDPError:
                pass

    # ----------------------------------------------------------- screencast
    def _start_screencast(self, tab):
        self._call("Target.activateTarget", {"targetId": tab.target_id})
        self._call("Page.startScreencast", dict(SCREENCAST), session=tab.session)

    def _stop_screencast(self, tab):
        with self._lock:
            if not tab.screencast:
                return
            tab.screencast = False
            tab.pending_ack = None
        try:
            self._call("Page.stopScreencast", session=tab.session, timeout=5.0)
        except BrowserError:
            pass

    def _flush_ack(self, tab):
        with self._lock:
            ack, tab.pending_ack = tab.pending_ack, None
            last = tab.last_ack
        if ack is None or self._conn is None:
            return
        wait = self._frame_interval - (self._clock() - last)
        if wait > 0:
            self._sleep(wait)
        try:
            self._conn.send_nowait("Page.screencastFrameAck", {"sessionId": ack}, session=tab.session)
        except CDPError:
            pass
        tab.last_ack = self._clock()

    def frame(self, ident, since, wait=FRAME_WAIT_MAX):
        """Long-poll: newest frame with seq > since, else meta only after `wait`."""
        tab = self._tab(ident)
        wait = max(0.0, min(float(wait), FRAME_WAIT_MAX))
        with self._lock:
            tab.pollers += 1
            tab.last_poll = self._clock()
            starting = not tab.screencast
            tab.screencast = True
        self._touch()
        try:
            if starting:
                try:
                    self._start_screencast(tab)
                except BrowserError:
                    with self._lock:
                        tab.screencast = False
                    raise
            self._flush_ack(tab)
            deadline = self._clock() + wait
            with self._cv:
                while (tab.seq <= since and self._tabs.get(tab.id) is tab and
                       self._state == "running"):
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    self._cv.wait(min(remaining, 1.0))
                    tab.last_poll = self._clock()
                if self._tabs.get(tab.id) is not tab:
                    raise TabGone("このタブはすでに閉じられています")
                fresh = tab.seq > since
                result = {
                    "seq": tab.seq,
                    "jpeg_base64": tab.frame if fresh else None,
                    "width": tab.size[0], "height": tab.size[1],
                    "url": tab.url, "title": tab.title,
                    "loading": bool(tab.loading), "blocked": tab.blocked,
                }
            self._touch()
            return result
        finally:
            with self._lock:
                tab.pollers -= 1
                tab.last_poll = self._clock()

    # ---------------------------------------------------------------- input
    @staticmethod
    def _number(value, low, high, name):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                value != value or not low <= value <= high):
            raise BrowserError("%sの値が範囲外です" % name)
        return float(value)

    def send_input(self, ident, event):
        if not isinstance(event, dict) or event.get("type") not in INPUT_TYPES:
            raise BrowserError("入力の種類が不正です")
        tab = self._tab(ident)
        self._touch()
        kind, session = event["type"], tab.session
        if kind == "tap":
            x = self._number(event.get("x"), 0, MAX_COORD, "x")
            y = self._number(event.get("y"), 0, MAX_COORD, "y")
            base = {"x": x, "y": y, "button": "left", "clickCount": 1}
            self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
            self._call("Input.dispatchMouseEvent", dict(base, type="mousePressed", buttons=1), session)
            self._call("Input.dispatchMouseEvent", dict(base, type="mouseReleased", buttons=0), session)
        elif kind == "scroll":
            x = self._number(event.get("x"), 0, MAX_COORD, "x")
            y = self._number(event.get("y"), 0, MAX_COORD, "y")
            dx = self._number(event.get("dx", 0), -MAX_SCROLL, MAX_SCROLL, "dx")
            dy = self._number(event.get("dy", 0), -MAX_SCROLL, MAX_SCROLL, "dy")
            self._call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                    "deltaX": dx, "deltaY": dy}, session)
        elif kind == "text":
            text = event.get("text")
            if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or
                    any(ord(c) < 32 and c not in "\n\t" for c in text)):
                raise BrowserError("入力文字列が不正です（1〜%d文字）" % MAX_TEXT)
            self._call("Input.insertText", {"text": text}, session)
        elif kind == "key":
            spec = KEYS.get(event.get("key"))
            if spec is None:
                raise BrowserError("対応していないキーです")
            code, vk, text = spec
            down = {"type": "keyDown" if text else "rawKeyDown", "key": event["key"], "code": code,
                    "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            if text:
                down["text"] = text
            self._call("Input.dispatchKeyEvent", down, session)
            self._call("Input.dispatchKeyEvent", {"type": "keyUp", "key": event["key"], "code": code,
                                                  "windowsVirtualKeyCode": vk,
                                                  "nativeVirtualKeyCode": vk}, session)
        elif kind == "reload":
            tab.blocked = None
            self._call("Page.reload", session=session)
        else:
            history = self._call("Page.getNavigationHistory", session=session)
            entries, index = history.get("entries"), history.get("currentIndex")
            step = -1 if kind == "back" else 1
            if (not isinstance(entries, list) or not isinstance(index, int) or
                    not 0 <= index + step < len(entries)):
                return {"ok": True, "moved": False}
            entry = entries[index + step].get("id") if isinstance(entries[index + step], dict) else None
            if not isinstance(entry, int):
                return {"ok": True, "moved": False}
            self._call("Page.navigateToHistoryEntry", {"entryId": entry}, session=session)
        return {"ok": True}

    # ---------------------------------------------------------------- links
    @staticmethod
    def extract_links(hrefs):
        chunks, current, size = [], [], 0
        for href in hrefs:
            if not isinstance(href, str) or len(href) > 4096:
                continue
            cost = len(href.encode("utf-8", "replace")) + 1
            if size + cost > 60000 and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(href)
            size += cost
        if current:
            chunks.append("\n".join(current))
        found, seen = [], set()
        for chunk in chunks:
            for url in rapidgator_extract.extract(chunk, LINK_RESULT_CAP):
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if len(found) >= LINK_RESULT_CAP:
                        return found
        return found

    def links(self, ident):
        tab = self._tab(ident)
        self._touch()
        result = self._call("Runtime.evaluate", {"expression": LINKS_JS, "returnByValue": True,
                                                  "timeout": 5000}, session=tab.session)
        value = (result.get("result") or {}).get("value")
        hrefs = value if isinstance(value, list) else []
        items = self.extract_links(hrefs[:LINK_CAP])
        return {"items": items, "count": len(items), "scanned": min(len(hrefs), LINK_CAP)}
