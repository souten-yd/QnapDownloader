"""v0.1.68: the Hub's WebSocket endpoint for the live display (ws_hub.py): handshake, frames, close, caps."""
import socket
import struct
import threading
import time
import unittest
from unittest import mock

import remote_browser as rb
import ws_hub


def masked(opcode, payload=b"", fin=True, key=b"\x37\xfa\x21\x3d"):
    return rb.encode_frame(opcode, payload, fin=fin, mask=True, key=key)


def reader_of(data):
    state = {"pos": 0}

    def read_exact(size):
        chunk = data[state["pos"]:state["pos"] + size]
        if len(chunk) < size:
            raise EOFError
        state["pos"] += size
        return chunk
    read_exact.state = state
    return read_exact


class HandshakeTests(unittest.TestCase):
    def test_rfc_6455_example_accept_key(self):
        self.assertEqual(ws_hub.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        response = ws_hub.handshake_response("dGhlIHNhbXBsZSBub25jZQ==").decode("ascii")
        self.assertTrue(response.startswith("HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n", response)
        self.assertIn("Upgrade: websocket\r\n", response)
        self.assertTrue(response.endswith("\r\n\r\n"))

    def test_key_must_be_16_random_bytes_in_base64(self):
        self.assertTrue(ws_hub.key_ok("dGhlIHNhbXBsZSBub25jZQ=="))
        for bad in ("", "short", "dGhlIHNhbXBsZSBub25jZQ", "!" * 24, "QUJDREVGR0hJSktMTU5PUFE=",
                    "QUJD" * 6, None, 5):
            self.assertFalse(ws_hub.key_ok(bad), bad)

    def test_handshake_requirements(self):
        good = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
        self.assertIsNone(ws_hub.handshake_error("GET", good))
        self.assertIsNone(ws_hub.handshake_error("GET", dict(good, Upgrade="WebSocket", Connection="upgrade")))
        self.assertEqual(ws_hub.handshake_error("POST", good)[0], 400)
        for change in ({"Upgrade": "h2c"}, {"Upgrade": ""}, {"Connection": "keep-alive"},
                       {"Sec-WebSocket-Version": "8"}, {"Sec-WebSocket-Version": ""},
                       {"Sec-WebSocket-Key": "x"}, {"Sec-WebSocket-Key": ""}):
            with self.subTest(change=change):
                self.assertEqual(ws_hub.handshake_error("GET", dict(good, **change))[0], 400)


class FrameTests(unittest.TestCase):
    def test_server_frames_use_the_three_length_forms_and_are_unmasked(self):
        for size, head in ((0, b"\x82\x00"), (125, b"\x82\x7d"), (126, b"\x82\x7e\x00\x7e"),
                           (65535, b"\x82\x7e\xff\xff"), (65536, b"\x82\x7f" + struct.pack(">Q", 65536))):
            with self.subTest(size=size):
                frame = rb.encode_frame(rb.OP_BINARY, b"x" * size, mask=False)
                self.assertEqual(frame[:len(head)], head)
                self.assertEqual(frame[len(head):], b"x" * size)

    def test_masked_client_frames_decode_in_all_length_forms(self):
        for size in (0, 5, 125, 126, 4096):
            payload = bytes(range(256)) * (size // 256 + 1)
            payload = payload[:size]
            with self.subTest(size=size):
                fin, opcode, got = ws_hub.read_client_frame(reader_of(masked(rb.OP_BINARY, payload)), 4096)
                self.assertEqual((fin, opcode, got), (True, rb.OP_BINARY, payload))
        # the RFC 6455 5.7 example: a masked "Hello"
        data = bytes.fromhex("818537fa213d7f9f4d5158")
        self.assertEqual(ws_hub.read_client_frame(reader_of(data))[2], b"Hello")

    def test_the_64_bit_length_form_is_decoded(self):
        frame = b"\x82\xff" + struct.pack(">Q", 3) + b"\x00\x00\x00\x00abc"
        self.assertEqual(ws_hub.read_client_frame(reader_of(frame))[2], b"abc")

    def test_unmasked_client_frame_closes_with_1002(self):
        with self.assertRaises(ws_hub.ClientClose) as ctx:
            ws_hub.read_client_frame(reader_of(rb.encode_frame(rb.OP_TEXT, b"ack", mask=False)))
        self.assertEqual(ctx.exception.code, 1002)

    def test_oversized_frame_closes_with_1009_before_the_payload_is_read(self):
        data = b"\x82\xfe" + struct.pack(">H", 5000) + b"\x00\x00\x00\x00"       # only the header is there
        read = reader_of(data)
        with self.assertRaises(ws_hub.ClientClose) as ctx:
            ws_hub.read_client_frame(read, 4096)
        self.assertEqual(ctx.exception.code, 1009)
        self.assertEqual(read.state["pos"], 4)                      # 2 + 2 length bytes, nothing more
        huge = b"\x82\xff" + struct.pack(">Q", 1 << 40)
        with self.assertRaises(ws_hub.ClientClose) as ctx:
            ws_hub.read_client_frame(reader_of(huge), 4096)
        self.assertEqual(ctx.exception.code, 1009)

    def test_reserved_bits_unknown_opcodes_and_bad_control_frames_are_protocol_errors(self):
        cases = {
            "rsv1": bytes([0xC2, 0x80]) + b"\0\0\0\0",
            "opcode 3": bytes([0x83, 0x80]) + b"\0\0\0\0",
            "fragmented ping": bytes([0x09, 0x80]) + b"\0\0\0\0",
            "long ping": bytes([0x89, 0xFE]) + struct.pack(">H", 126) + b"\0\0\0\0" + b"x" * 126,
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(ws_hub.ClientClose) as ctx:
                    ws_hub.read_client_frame(reader_of(data))
                self.assertEqual(ctx.exception.code, 1002)


class Pair:
    """A LiveSocket on one end of a socketpair; `peer` is the phone."""

    def __init__(self, test, **kwargs):
        self.server_sock, self.peer = socket.socketpair()
        self.peer.settimeout(3)
        self.live = ws_hub.LiveSocket(self.server_sock, **kwargs)
        self.fp = self.peer.makefile("rb")
        test.addCleanup(self.close)

    def close(self):
        self.live.closed.set()
        for sock in (self.server_sock, self.peer):
            try:
                sock.close()
            except OSError:
                pass

    def recv_frame(self):
        return rb.read_frame(lambda n: self.fp.read(n) if n else b"")

    def expect_eof(self, timeout=3):
        self.peer.settimeout(timeout)
        while True:
            data = self.peer.recv(4096)
            if not data:
                return True


def close_code(payload):
    return struct.unpack(">H", payload[:2])[0]


class LiveSocketTests(unittest.TestCase):
    def test_binary_ping_and_text_frames_reach_the_phone_unmasked(self):
        pair = Pair(self)
        pair.live.send_binary(b"\xff\xd8\xff-jpeg")
        pair.live.send_ping()
        self.assertEqual(pair.recv_frame(), (True, rb.OP_BINARY, b"\xff\xd8\xff-jpeg"))
        self.assertEqual(pair.recv_frame()[1], rb.OP_PING)
        self.assertEqual(pair.live.frames_sent, 1)

    def test_phone_ping_gets_a_pong_ack_is_accepted_and_close_is_echoed(self):
        pair = Pair(self)
        pair.live.start_reader()
        pair.peer.sendall(masked(rb.OP_PING, b"hi") + masked(rb.OP_TEXT, b"ack") + masked(rb.OP_PONG, b""))
        self.assertEqual(pair.recv_frame(), (True, rb.OP_PONG, b"hi"))
        pair.peer.sendall(masked(rb.OP_CLOSE, struct.pack(">H", 1000)))
        fin, opcode, payload = pair.recv_frame()
        self.assertEqual((opcode, close_code(payload)), (rb.OP_CLOSE, 1000))
        pair.live.reader.join(3)
        self.assertFalse(pair.live.reader.is_alive())
        self.assertTrue(pair.live.closed.is_set())
        self.assertEqual(pair.live.acks, 1)
        with self.assertRaises(OSError):
            pair.live.send_binary(b"late")                       # nothing is sent after the close

    def test_a_phone_that_vanishes_ends_the_socket(self):
        pair = Pair(self)
        pair.live.start_reader()
        pair.fp.close()
        pair.peer.close()
        pair.live.reader.join(3)
        self.assertTrue(pair.live.closed.is_set())
        self.assertFalse(pair.live.alive())

    def test_misbehaving_clients_are_closed_with_the_right_code(self):
        cases = (
            ("unmasked", rb.encode_frame(rb.OP_TEXT, b"ack", mask=False), 1002),
            ("oversized", masked(rb.OP_BINARY, b"x" * 5000), 1009),
            ("fragment start", masked(rb.OP_TEXT, b"ac", fin=False), 1002),
            ("continuation", masked(rb.OP_CONT, b"k"), 1002),
            ("binary", masked(rb.OP_BINARY, b"\x00"), 1003),
            ("other text", masked(rb.OP_TEXT, b"pause"), 1008),
        )
        for name, data, code in cases:
            with self.subTest(name):
                pair = Pair(self)
                pair.live.start_reader()
                pair.peer.sendall(data)
                fin, opcode, payload = pair.recv_frame()
                self.assertEqual((opcode, close_code(payload)), (rb.OP_CLOSE, code))
                pair.live.reader.join(3)
                self.assertTrue(pair.live.closed.is_set())
                pair.close()

    def test_our_close_is_sent_once_and_finish_waits_for_the_echo(self):
        pair = Pair(self)
        pair.live.start_reader()
        done = threading.Thread(target=pair.live.finish, args=(1001, "bye"))
        done.start()
        fin, opcode, payload = pair.recv_frame()
        self.assertEqual((opcode, close_code(payload), payload[2:]), (rb.OP_CLOSE, 1001, b"bye"))
        pair.peer.sendall(masked(rb.OP_CLOSE, struct.pack(">H", 1001)))
        done.join(3)
        self.assertFalse(done.is_alive())
        self.assertTrue(pair.expect_eof())
        pair.live.send_close(1000)                                # a second close frame is never sent

    def test_a_viewer_that_does_not_read_stops_the_producer_with_an_error(self):
        # Backpressure: the kernel buffer fills, the send times out and the socket ends (the producer then skips to the
        # newest frame on a healthy socket; it never queues frames for a slow one).
        with mock.patch.object(ws_hub, "IO_TIMEOUT", 0.3), mock.patch.object(ws_hub, "SEND_BUFFER", 4096):
            pair = Pair(self)
        failed = []
        started = time.monotonic()

        def producer():
            try:
                for _ in range(10000):
                    pair.live.send_binary(b"x" * 60000)
            except OSError as error:
                failed.append(error)
        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        thread.join(10)
        self.assertEqual(len(failed), 1)
        self.assertTrue(pair.live.closed.is_set())
        self.assertLess(time.monotonic() - started, 10)
        self.assertLess(pair.live.frames_sent, 10000)

    def test_a_silent_phone_is_dropped_after_the_dead_peer_timeout(self):
        clock = [0.0]
        with mock.patch.object(ws_hub, "IO_TIMEOUT", 0.05), mock.patch.object(ws_hub, "DEAD_SECONDS", 5.0):
            pair = Pair(self, clock=lambda: clock[0])
            pair.live.start_reader()
            time.sleep(0.2)
            self.assertTrue(pair.live.alive())                    # quiet but within the limit
            clock[0] = 6.0
            pair.live.reader.join(3)
            self.assertFalse(pair.live.alive())


class RegistryTests(unittest.TestCase):
    class Fake:
        def __init__(self):
            self.evicted = False

        def evict(self):
            self.evicted = True

    def test_never_more_than_the_limit_and_the_oldest_makes_room(self):
        registry = ws_hub.Registry(4)
        sockets = [self.Fake() for _ in range(6)]
        for n, live in enumerate(sockets):
            registry.add(live)
            self.assertLessEqual(registry.count(), 4)
        self.assertEqual([s.evicted for s in sockets], [True, True, False, False, False, False])
        registry.remove(sockets[5])
        registry.remove(sockets[5])                               # twice is fine
        self.assertEqual(registry.count(), 3)
        self.assertEqual(ws_hub.MAX_SOCKETS, 4)

    def test_eviction_closes_a_real_socket_with_4001(self):
        pair = Pair(self)
        registry = ws_hub.Registry(1)
        registry.add(pair.live)
        registry.add(self.Fake())
        fin, opcode, payload = pair.recv_frame()
        self.assertEqual((opcode, close_code(payload)), (rb.OP_CLOSE, ws_hub.CLOSE_REPLACED))
        self.assertFalse(pair.live.alive())


if __name__ == "__main__":
    unittest.main()
