"""v0.1.68: the Hub's WebSocket endpoint for the live display (RFC 6455 server side, stdlib only).

Why: behind `tailscale serve` (a buffering reverse proxy) neither the multipart (MJPEG) nor the event stream
(SSE) reached the phone, but reverse proxies pass WebSocket upgrades through unbuffered.

What is here: the handshake check, one `LiveSocket` per upgraded connection, and a `Registry` that caps the
number of live sockets. The frames themselves come from `RemoteBrowser.stream(..., ws=True)` (the producer SSE and
MJPEG already use: newest frame only, so a slow viewer skips frames instead of queueing them). Frame encoding and
the unmasking helper are reused from remote_browser.py (the CDP client's RFC 6455 code).

Server -> client: one binary message per screencast frame (the JPEG bytes as they are) and a ping every
PING_SECONDS. Client -> server: masked control frames (ping / pong / close) and the text message "ack"
(a keep-alive the phone may send); everything else closes the socket (fragmentation, binary, unmasked,
oversized frames, other text).
"""
import base64
import hashlib
import socket
import struct
import threading
import time

import remote_browser as rb

MAX_SOCKETS = 4                 # concurrent live sockets (the oldest is closed when a fifth arrives)
CLIENT_MAX_FRAME = 4096         # the biggest frame payload accepted from the phone
PING_SECONDS = 20.0             # idle proxies drop quiet connections: a ping at least this often
DEAD_SECONDS = 70.0             # no byte from the phone (not even a pong) for this long: the peer is gone
IO_TIMEOUT = 10.0               # a send that does not finish in this long ends the socket (slow / dead viewer)
SEND_BUFFER = 128 * 1024        # small kernel buffer: a slow viewer blocks the producer, which then skips frames
CLOSE_WAIT = 1.0                # after our close frame, wait this long for the phone's echo

CLOSE_NORMAL, CLOSE_GOING_AWAY, CLOSE_PROTOCOL, CLOSE_UNSUPPORTED = 1000, 1001, 1002, 1003
CLOSE_POLICY, CLOSE_TOO_BIG = 1008, 1009
CLOSE_REPLACED = 4001           # private use: a newer socket took this one's place (cap reached)

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = (
    rb.OP_CONT, rb.OP_TEXT, rb.OP_BINARY, rb.OP_CLOSE, rb.OP_PING, rb.OP_PONG)
CLIENT_TEXT_OK = (b"ack",)


def accept_key(key):
    """Sec-WebSocket-Accept for a Sec-WebSocket-Key (RFC 6455 section 4.2.2)."""
    return base64.b64encode(hashlib.sha1((key + rb.WS_GUID).encode("ascii")).digest()).decode("ascii")


def key_ok(key):
    """A Sec-WebSocket-Key is the base64 of exactly 16 random bytes."""
    if not isinstance(key, str) or len(key) != 24:
        return False
    try:
        return len(base64.b64decode(key.encode("ascii"), validate=True)) == 16
    except (ValueError, UnicodeEncodeError):
        return False


def handshake_error(method, headers):
    """None when `headers` ask for a valid WebSocket upgrade, else (status, message)."""
    if method != "GET":
        return 400, "WebSocketはGETで接続してください"
    if "websocket" not in [p.strip().lower() for p in (headers.get("Upgrade") or "").split(",")]:
        return 400, "WebSocketのアップグレード要求ではありません"
    if "upgrade" not in [p.strip().lower() for p in (headers.get("Connection") or "").split(",")]:
        return 400, "WebSocketのアップグレード要求ではありません"
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        return 400, "WebSocketのバージョンは13だけ使えます"
    if not key_ok((headers.get("Sec-WebSocket-Key") or "").strip()):
        return 400, "Sec-WebSocket-Keyが不正です"
    return None


def handshake_response(key):
    """The 101 response (HTTP/1.1 on purpose: browsers want it, the Hub's handler speaks 1.0)."""
    return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\nCache-Control: no-store\r\nX-Accel-Buffering: no\r\n"
            "X-Content-Type-Options: nosniff\r\n\r\n" % accept_key(key)).encode("ascii")


class ClientClose(Exception):
    """The phone's frame is unacceptable: close with `code`."""

    def __init__(self, code, reason=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def read_client_frame(read_exact, max_payload=CLIENT_MAX_FRAME):
    """(fin, opcode, payload) of one client frame (unmasked here). Raises ClientClose for anything the Hub does
    not accept: an unmasked frame or reserved bits (1002), a payload over `max_payload` (1009, before it is read)."""
    head = read_exact(2)
    if head[0] & 0x70:
        raise ClientClose(CLOSE_PROTOCOL, "reserved bits")
    opcode = head[0] & 0x0F
    if opcode not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
        raise ClientClose(CLOSE_PROTOCOL, "opcode")
    if not head[1] & 0x80:
        raise ClientClose(CLOSE_PROTOCOL, "unmasked frame")      # RFC 6455 5.1: the client MUST mask
    fin = bool(head[0] & 0x80)
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack(">H", read_exact(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", read_exact(8))[0]
    if opcode >= OP_CLOSE and (not fin or size > 125):
        raise ClientClose(CLOSE_PROTOCOL, "control frame")
    if size > max_payload:
        raise ClientClose(CLOSE_TOO_BIG, "frame too large")
    key = read_exact(4)
    payload = rb._xor(read_exact(size), key) if size else b""
    return fin, opcode, payload


class LiveSocket:
    """One upgraded connection. The producer thread sends (send_binary / send_ping); a reader thread answers
    the phone's control frames. All sends share a lock; either side ends it by `closed`."""

    def __init__(self, sock, max_client_frame=CLIENT_MAX_FRAME, clock=time.monotonic):
        self.sock = sock
        self.max_client_frame = max_client_frame
        self._clock = clock
        self._send_lock = threading.Lock()
        self.closed = threading.Event()         # set once the socket must not be used any more
        self.close_sent = False
        self.close_code = None                  # what the phone (or we) closed with
        self.frames_sent = 0
        self.acks = 0
        self.last_rx = clock()
        self.reader = None
        for level, option, value in ((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                                     (socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)):
            try:
                sock.setsockopt(level, option, value)
            except OSError:
                pass                              # (not a TCP socket, as in the unit tests' socketpair)
        sock.settimeout(IO_TIMEOUT)

    # -- sending
    def _send(self, opcode, payload=b""):
        data = rb.encode_frame(opcode, payload, mask=False)
        with self._send_lock:
            if self.closed.is_set() or self.close_sent:
                raise OSError("websocket closed")
            try:
                self.sock.sendall(data)
            except OSError:
                self.closed.set()
                raise

    def send_binary(self, payload):
        try:
            self._send(OP_BINARY, payload)
        except rb.WebSocketError:
            return                               # absurdly large frame (> 1 MiB): skip it, the next one follows
        self.frames_sent += 1

    def send_ping(self):
        self._send(OP_PING, b"qd")

    def send_close(self, code, reason=""):
        """Our close frame (once). Never raises."""
        text = reason.encode("utf-8")[:100]
        with self._send_lock:
            if self.close_sent or self.closed.is_set():
                return
            self.close_sent = True
            try:
                self.sock.sendall(rb.encode_frame(OP_CLOSE, struct.pack(">H", code) + text, mask=False))
            except (OSError, rb.WebSocketError):
                self.closed.set()

    # -- receiving
    def _read_exact(self, size):
        chunks, got = [], 0
        while got < size:
            if self.closed.is_set():
                raise EOFError
            try:
                data = self.sock.recv(size - got)
            except socket.timeout:
                if self._clock() - self.last_rx > DEAD_SECONDS:
                    raise EOFError from None
                continue
            except OSError:
                raise EOFError from None
            if not data:
                raise EOFError
            self.last_rx = self._clock()
            chunks.append(data)
            got += len(data)
        return b"".join(chunks)

    def handle_frame(self, fin, opcode, payload):
        """Act on one decoded client frame; raises ClientClose when it is not acceptable. True = keep going."""
        if opcode == OP_PING:
            self._send(OP_PONG, payload)
        elif opcode == OP_PONG:
            pass
        elif opcode == OP_CLOSE:
            code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else CLOSE_NORMAL
            self.close_code = code
            self.send_close(code if 1000 <= code < 5000 and code not in (1004, 1005, 1006, 1015) else CLOSE_NORMAL)
            return False
        elif opcode == OP_CONT or not fin:
            raise ClientClose(CLOSE_PROTOCOL, "fragmented messages are not accepted")
        elif opcode == OP_TEXT:
            if payload not in CLIENT_TEXT_OK:
                raise ClientClose(CLOSE_POLICY, "unexpected message")
            self.acks += 1
        else:
            raise ClientClose(CLOSE_UNSUPPORTED, "binary messages are not accepted")
        return True

    def read_loop(self):
        """Reader thread body: runs until the phone closes, misbehaves or the socket dies; then sets `closed`."""
        try:
            while not self.closed.is_set():
                fin, opcode, payload = read_client_frame(self._read_exact, self.max_client_frame)
                if not self.handle_frame(fin, opcode, payload):
                    break
        except ClientClose as error:
            self.close_code = error.code
            self.send_close(error.code, error.reason)
        except (EOFError, OSError):
            pass
        finally:
            self.closed.set()

    def start_reader(self):
        self.reader = threading.Thread(target=self.read_loop, name="ws-live-reader", daemon=True)
        self.reader.start()
        return self.reader

    # -- ending
    def alive(self):
        return not self.closed.is_set()

    def evict(self):
        """The cap was reached: tell the phone, then end the producer (it polls `alive`)."""
        self.send_close(CLOSE_REPLACED, "replaced")
        self.closed.set()

    def finish(self, code=CLOSE_GOING_AWAY, reason=""):
        """Producer ended (tab closed, lifetime over, error): close frame, short wait for the echo, drop the socket."""
        if self.alive():
            self.send_close(code, reason)
            reader = self.reader
            if reader is not None:
                end = self._clock() + CLOSE_WAIT
                while reader.is_alive() and self._clock() < end:
                    reader.join(0.05)
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self.reader is not None:
            self.reader.join(2.0)


class Registry:
    """The live sockets of this Hub: at most `limit`; a newer one evicts the oldest (a socket whose phone vanished
    without a FIN would otherwise block a reconnect until the dead-peer timeout)."""

    def __init__(self, limit=MAX_SOCKETS):
        self.limit = limit
        self._lock = threading.Lock()
        self._items = []

    def add(self, live):
        with self._lock:
            evicted = []
            while len(self._items) >= self.limit:
                evicted.append(self._items.pop(0))
            self._items.append(live)
        for old in evicted:
            old.evict()
        return len(evicted)

    def remove(self, live):
        with self._lock:
            if live in self._items:
                self._items.remove(live)

    def count(self):
        with self._lock:
            return len(self._items)
