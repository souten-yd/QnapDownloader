"""Remote browser: WebSocket framing, CDP manager with a fake transport. No docker, no network."""
import base64
import io
import json
import queue
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import container_runtime as cr
import remote_browser as rb
from fake_docker import FakeDocker


# ------------------------------------------------------------ websocket framing
def reader(data):
    stream = io.BytesIO(data)

    def read_exact(size):
        chunk = stream.read(size)
        if len(chunk) != size:
            raise rb.WebSocketError("eof")
        return chunk
    return read_exact


class FakeSock:
    def __init__(self, incoming=b""):
        self.sent = []
        self._incoming = incoming
        self.closed = False

    def makefile(self, mode, size):
        return io.BytesIO(self._incoming)

    def sendall(self, data):
        self.sent.append(data)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class FramingTests(unittest.TestCase):
    def test_client_frames_are_masked_and_roundtrip(self):
        key = b"\x01\x02\x03\x04"
        frame = rb.encode_frame(rb.OP_TEXT, b"hello", key=key)
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[1], 0x80 | 5)
        self.assertEqual(frame[2:6], key)
        self.assertEqual(frame[6:], bytes(b ^ key[i % 4] for i, b in enumerate(b"hello")))
        fin, op, payload = rb.read_frame(reader(frame), allow_masked=True)
        self.assertEqual((fin, op, payload), (True, rb.OP_TEXT, b"hello"))

    def test_random_mask_key_is_used_when_not_given(self):
        a = rb.encode_frame(rb.OP_TEXT, b"x" * 10)
        b = rb.encode_frame(rb.OP_TEXT, b"x" * 10)
        self.assertTrue(a[1] & 0x80)
        self.assertEqual(len(a), 2 + 4 + 10)
        self.assertNotEqual(a[2:6] + a[6:], b[2:6] + b[6:]) if a[2:6] != b[2:6] else None

    def test_length_encodings(self):
        for size, marker in ((125, None), (126, 126), (65535, 126), (65536, 127)):
            frame = rb.encode_frame(rb.OP_BINARY, b"a" * size, key=b"\0\0\0\0")
            if marker:
                self.assertEqual(frame[1] & 0x7F, marker)
            self.assertEqual(rb.read_frame(reader(frame), allow_masked=True)[2], b"a" * size)

    def test_server_frames_must_be_unmasked(self):
        masked = rb.encode_frame(rb.OP_TEXT, b"x", key=b"\x05\x06\x07\x08")
        with self.assertRaises(rb.WebSocketError):
            rb.read_frame(reader(masked))
        plain = rb.encode_frame(rb.OP_TEXT, b"x", mask=False)
        self.assertEqual(rb.read_frame(reader(plain))[2], b"x")

    def test_oversize_rejected_before_reading_payload(self):
        header = bytes([0x81, 127]) + struct.pack(">Q", 10 ** 9)
        with self.assertRaises(rb.WebSocketError):
            rb.read_frame(reader(header), 1024)
        with self.assertRaises(rb.WebSocketError):
            rb.read_frame(reader(bytes([0x81, 127]) + struct.pack(">Q", 1 << 63)), 1 << 70)

    def test_reserved_bits_unknown_opcode_and_bad_control_frames(self):
        for head in (bytes([0xC1, 0]), bytes([0x83, 0]), bytes([0x09, 0]),     # rsv1, opcode 3, fragmented ping
                     bytes([0x89, 126]) + struct.pack(">H", 200)):               # ping too long
            with self.subTest(head=head), self.assertRaises(rb.WebSocketError):
                rb.read_frame(reader(head + b"x" * 300))
        with self.assertRaises(rb.WebSocketError):
            rb.encode_frame(rb.OP_PING, b"x" * 126)
        with self.assertRaises(rb.WebSocketError):
            rb.encode_frame(rb.OP_CLOSE, b"", fin=False)

    def _ws(self, data, max_message=1024):
        sock = FakeSock(data)
        return rb.WebSocket(sock, max_message), sock

    def frames(self, *specs):
        return b"".join(rb.encode_frame(op, payload, fin=fin, mask=False) for op, payload, fin in specs)

    def test_fragmented_message_is_reassembled(self):
        ws, _ = self._ws(self.frames((rb.OP_TEXT, b"hel", False), (rb.OP_PING, b"p", True),
                                     (rb.OP_CONT, b"lo ", False), (rb.OP_CONT, "日本".encode(), True)))
        self.assertEqual(ws.recv(), "hello 日本")

    def test_ping_gets_a_masked_pong_and_close_is_echoed(self):
        ws, sock = self._ws(self.frames((rb.OP_PING, b"abc", True), (rb.OP_TEXT, b"x", True),
                                        (rb.OP_CLOSE, struct.pack(">H", 1000), True)))
        self.assertEqual(ws.recv(), "x")
        pong = sock.sent[0]
        self.assertEqual(pong[0], 0x80 | rb.OP_PONG)
        self.assertTrue(pong[1] & 0x80)
        self.assertEqual(rb.read_frame(reader(pong), allow_masked=True)[2], b"abc")
        self.assertIsNone(ws.recv())
        self.assertEqual(sock.sent[1][0] & 0x0F, rb.OP_CLOSE)

    def test_fragmented_and_oversize_messages_are_rejected(self):
        ws, _ = self._ws(self.frames((rb.OP_TEXT, b"a" * 600, False), (rb.OP_CONT, b"b" * 600, True)))
        with self.assertRaises(rb.WebSocketError):
            ws.recv()          # total above the message cap
        ws, _ = self._ws(self.frames((rb.OP_CONT, b"x", True)))
        with self.assertRaises(rb.WebSocketError):
            ws.recv()          # continuation without a start
        ws, _ = self._ws(self.frames((rb.OP_TEXT, b"a", False), (rb.OP_TEXT, b"b", True)))
        with self.assertRaises(rb.WebSocketError):
            ws.recv()          # new message inside a fragmented one
        ws, _ = self._ws(self.frames((rb.OP_BINARY, b"a", True)))
        with self.assertRaises(rb.WebSocketError):
            ws.recv()
        ws, _ = self._ws(self.frames((rb.OP_TEXT, b"\xff\xfe", True)))
        with self.assertRaises(rb.WebSocketError):
            ws.recv()

    def test_truncated_stream_raises(self):
        ws, _ = self._ws(rb.encode_frame(rb.OP_TEXT, b"hello", mask=False)[:4])
        with self.assertRaises(rb.WebSocketError):
            ws.recv()

    def test_handshake_validates_accept_key_and_path(self):
        import base64
        import hashlib
        key_bytes = bytes(range(16))
        key = base64.b64encode(key_bytes).decode()
        accept = base64.b64encode(hashlib.sha1((key + rb.WS_GUID).encode()).digest()).decode()
        good = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode()
        sock = FakeSock(good)
        sock.settimeout = lambda t: None
        ws = rb.WebSocket.connect("172.30.0.2", 9223, "/devtools/browser/abc-123",
                                  create=lambda addr, t: sock, rng=lambda n: key_bytes)
        request = sock.sent[0].decode()
        self.assertIn("GET /devtools/browser/abc-123 HTTP/1.1", request)
        self.assertIn("Sec-WebSocket-Key: " + key, request)
        self.assertNotIn("Origin", request)
        self.assertIsNotNone(ws)
        bad = FakeSock(good.replace(accept.encode(), b"wrong"))
        bad.settimeout = lambda t: None
        with self.assertRaises(rb.WebSocketError):
            rb.WebSocket.connect("172.30.0.2", 9223, "/devtools/browser/abc",
                                 create=lambda a, t: bad, rng=lambda n: key_bytes)
        self.assertTrue(bad.closed)
        for path in ("/json", "/devtools/page/abc", "/devtools/browser/../x", "devtools/browser/a"):
            with self.subTest(path=path), self.assertRaises(rb.WebSocketError):
                rb.WebSocket.connect("1.2.3.4", 9223, path, create=lambda a, t: FakeSock())


# --------------------------------------------------------------- fake CDP peer
class FakeWS:
    """Websocket-like peer that answers CDP commands and can emit events."""

    def __init__(self):
        self.q = queue.Queue()
        self.sent = []
        self.targets = 0
        self.sessions = 0
        self.closed = False
        self.fail = {}                 # method -> error text
        self.eval_value = []
        self.eval_map = []             # (expression substring, value) overrides for Runtime.evaluate
        self.hook = None               # called with every command before it is answered
        self.history = {"currentIndex": 1, "entries": [{"id": 5, "url": "https://a.example/1"}, {"id": 6, "url": "https://a.example/2"}, {"id": 7, "url": "https://a.example/3"}]}
        self.layout = {"cssContentSize": {"width": 390, "height": 4000.2}}
        self.shot_size = 100           # bytes of each captured tile
        self.results = {}              # method -> result dict (or callable(params) -> dict)

    def send_text(self, text):
        message = json.loads(text)
        self.sent.append(message)
        method = message["method"]
        if self.hook is not None:
            self.hook(message)
        if method in self.fail:
            self.q.put(json.dumps({"id": message["id"], "error": {"message": self.fail[method]}}))
            return
        result = {}
        if method == "Target.getTargets":
            result = {"targetInfos": [{"targetId": "ANCHOR00001", "type": "page"}]}
        elif method == "Target.createTarget":
            self.targets += 1
            result = {"targetId": "TARGET%05d" % self.targets}
        elif method == "Target.attachToTarget":
            self.sessions += 1
            result = {"sessionId": "SESSION%d" % self.sessions}
        elif method == "Runtime.evaluate":
            value = self.eval_value
            for needle, mapped in self.eval_map:
                if needle in message["params"]["expression"]:
                    value = mapped
                    break
            result = {"result": {"type": "object", "value": value}}
        elif method in self.results:
            result = self.results[method]
            if callable(result):
                result = result(message.get("params") or {})
        elif method == "Page.getNavigationHistory":
            result = self.history
        elif method == "Page.getLayoutMetrics":
            result = self.layout
        elif method == "Page.captureScreenshot":
            clip = message["params"]["clip"]
            seed = ("%d:" % clip["y"]).encode()
            result = {"data": base64.b64encode((seed + b"x" * self.shot_size)[:self.shot_size]).decode()}
        self.q.put(json.dumps({"id": message["id"], "result": result}))

    def recv(self):
        item = self.q.get()
        return item

    def close(self):
        self.closed = True
        self.q.put(None)

    def emit(self, method, params=None, session=None):
        message = {"method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.q.put(json.dumps(message))

    def methods(self):
        return [m["method"] for m in self.sent]

    def calls(self, method):
        return [m for m in self.sent if m["method"] == method]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class BrowserBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ctx = root / "ctx"
        self.ctx.mkdir()
        (self.ctx / "Dockerfile").write_text("FROM x\n")
        (self.ctx / "entrypoint.sh").write_text("#!/bin/sh\n")
        self.meminfo = root / "meminfo"
        self.set_mem(8 * 1024 * 1024)
        self.fake = FakeDocker()
        self.ws = FakeWS()
        self.clock = Clock()
        self.connects = []
        self.rt = cr.Runtime(self.fake, isfile=self.fake.isfile, access=lambda p, m: True,
                             meminfo_path=str(self.meminfo))
        self.tag = self.rt.browser_tag(str(self.ctx))
        self.fake.images.add(self.tag)
        self.rb = rb.RemoteBrowser(self.rt, self.ctx, meminfo_path=str(self.meminfo),
                                   connect=self.connect, clock=self.clock,
                                   sleep=lambda s: None, monitor=False, frame_interval=0.0,
                                   start_timeout=3.0)
        self.addCleanup(self.rb.shutdown)

    def connect(self, ip):
        self.connects.append(ip)
        return self.ws

    def set_mem(self, kib):
        self.meminfo.write_text("MemTotal: 16000000 kB\nMemAvailable: %d kB\n" % kib)

    def barrier(self):
        """A round trip: every event queued before it has been handled."""
        self.rb._call("Target.getTargets")

    def open(self, url="https://example.com/a"):
        return self.rb.open_tab(url)

    def feed(self, tab_session, data="QUJD", seq_ack=11, width=1280, height=2000):
        self.ws.emit("Page.screencastFrame", {
            "data": data, "sessionId": seq_ack,
            "metadata": {"deviceWidth": width, "deviceHeight": height}}, tab_session)
        self.barrier()


class LifecycleTests(BrowserBase):
    def test_first_tab_starts_container_connects_and_denies_downloads(self):
        info = self.open("https://example.com/a")
        self.assertEqual(info, {"id": "b1", "url": "https://example.com/a"})
        runs = [c for c in self.fake.docker_calls() if c[0] == "run"]
        self.assertEqual(runs, [self.rt.browser_run_args(self.tag)])
        self.assertEqual(self.connects, ["172.30.0.2"])
        self.assertEqual(self.ws.calls("Browser.setDownloadBehavior")[0]["params"],
                         {"behavior": "deny", "eventsEnabled": True})
        self.assertEqual(self.ws.calls("Target.setDiscoverTargets")[0]["params"], {"discover": True})
        create = self.ws.calls("Target.createTarget")[0]["params"]
        self.assertEqual(create["url"], "about:blank")
        navigate = self.ws.calls("Page.navigate")[0]
        self.assertEqual(navigate["params"], {"url": "https://example.com/a"})
        self.assertEqual(navigate["sessionId"], "SESSION1")
        self.assertLess(self.ws.methods().index("Target.createTarget"), self.ws.methods().index("Page.navigate"))
        status = self.rb.status()
        self.assertEqual(status["phase"], "running")
        self.assertEqual([t["id"] for t in status["tabs"]], ["b1"])

    def test_invalid_urls_never_reach_the_browser(self):
        for url in ("http://example.com/", "https://192.168.0.1/", "https://localhost/", "file:///etc/passwd",
                    "javascript:alert(1)", "https://user:pw@example.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.rb.open_tab(url)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])

    def test_max_three_tabs(self):
        for i in range(3):
            self.open("https://example.com/%d" % i)
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open("https://example.com/4")
        self.assertIn("最大3枚", str(ctx.exception))
        self.assertEqual(len(self.ws.calls("Target.createTarget")), 3)

    def test_at_the_cap_tabs_nobody_polled_are_closed_to_make_room(self):
        # v0.1.60: a tab whose opener lost the answer is nobody's; at the cap the idle ones go, oldest first
        for i in range(3):
            self.open("https://example.com/%d" % i)
            self.clock.now += 1
        self.clock.now += rb.ORPHAN_IDLE
        with self.rb._lock:
            self.rb._tabs["b3"].last_poll = self.clock.now      # b3 is in use
        self.open("https://example.com/4")
        ids = [t["id"] for t in self.rb.status()["tabs"]]
        self.assertNotIn("b1", ids)
        self.assertIn("b2", ids)
        self.assertIn("b3", ids)
        self.assertEqual(len(ids), 3)

    def test_at_the_cap_tabs_in_use_are_never_closed(self):
        for i in range(3):
            self.open("https://example.com/%d" % i)
        self.clock.now += rb.ORPHAN_IDLE - 1
        with self.assertRaises(rb.BrowserError):
            self.open("https://example.com/4")
        self.assertEqual(len(self.rb.status()["tabs"]), 3)

    def test_memory_guard_refuses_before_starting_anything(self):
        self.set_mem(2 * 1024 * 1024 - 1)
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("空きメモリが不足", str(ctx.exception))
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] == "run"], [])
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.set_mem(2 * 1024 * 1024)
        self.open()      # exactly 2 GiB is enough

    def test_memory_guard_unreadable_meminfo_refuses(self):
        self.meminfo.unlink()
        with self.assertRaises(rb.BrowserError):
            self.open()

    def test_container_usage_over_80_percent_refuses_new_tabs(self):
        self.open()
        self.fake.stats_out = "1700MiB / 2GiB|83.00%"
        self.clock.now += 10          # past the 5 s stats cache
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open("https://example.com/b")
        self.assertIn("80%", str(ctx.exception))
        self.assertEqual(len(self.ws.calls("Target.createTarget")), 1)
        self.fake.stats_out = "1000MiB / 2GiB|48.00%"
        self.clock.now += 10
        self.open("https://example.com/b")

    def test_unknown_container_usage_refuses_when_running(self):
        self.open()
        self.fake.stats_fail = True
        self.clock.now += 10
        with self.assertRaises(rb.BrowserError):
            self.open("https://example.com/b")

    def test_stats_are_cached_for_five_seconds_and_target_only_our_container(self):
        self.open()
        before = len([c for c in self.fake.docker_calls() if c[0] == "stats"])
        self.rb.memory_reason()
        self.rb.memory_reason()
        self.assertLessEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 1)
        self.clock.now += 6
        self.rb.memory_reason()                # the tab-open guard stays fresh (5 s)
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 2)
        # v0.1.46: status() (the /api/state path) never starts docker stats; the settings view's status()
        # reuses a result up to 30 s old
        self.clock.now += 6
        self.rb.status()
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 2)
        self.rb.status(fresh_memory=True)
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 2)
        self.clock.now += 31
        self.rb.status(fresh_memory=True)
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 3)
        for call in self.fake.docker_calls():
            if call[0] == "stats":
                self.assertEqual(call[-1], "qnapdl-browser")

    def test_image_missing_requires_setup(self):
        self.fake.images.clear()
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("初期設定", str(ctx.exception))

    def test_userns_failure_message_and_cleanup(self):
        self.fake.run_exits = cr.BROWSER_EXIT_NO_USERNS
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("ユーザー名前空間", str(ctx.exception))
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_chroot_failure_message_and_cleanup(self):
        self.fake.run_exits = cr.BROWSER_EXIT_NO_CHROOT
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("chroot", str(ctx.exception))
        self.assertNotIn("qnapdl-browser", self.fake.containers)

    def test_capability_guard_exit_is_reported_and_cleaned_up(self):
        self.fake.run_exits = cr.BROWSER_EXIT_CAPS
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("CapEff", str(ctx.exception))
        self.assertNotIn("qnapdl-browser", self.fake.containers)

    def test_early_exit_reads_logs_tail_and_surfaces_it(self):
        self.fake.run_exits = cr.BROWSER_EXIT_CHROMIUM
        self.fake.container_logs = ("qnapdl-browser: chromium exited\n" +
                                    "Failed to move to new namespace " * 30 + "TAILEND")
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        message = str(ctx.exception)
        self.assertIn("Chromiumが終了しました", message)
        self.assertIn("終了コード 72", message)
        self.assertIn("TAILEND", message)
        self.assertLessEqual(len(message.split(": ", 1)[1]), 300)
        lines = "\n".join(self.rb.log_lines())
        self.assertIn("qnapdl-browser: chromium exited", lines)
        self.assertIn("TAILEND", lines)
        calls = self.fake.docker_calls()
        logs = [i for i, c in enumerate(calls) if c[0] == "logs"]
        removes = [i for i, c in enumerate(calls) if c[:2] == ["rm", "-f"]]
        self.assertEqual([calls[i] for i in logs], [["logs", "--tail", "80", "qnapdl-browser"]])
        self.assertLess(logs[0], removes[0])
        self.assertNotIn("qnapdl-browser", self.fake.containers)

    def test_each_exit_code_has_its_message_and_missing_logs_are_tolerated(self):
        for code, word in ((70, "時間切れ"), (71, "Xvfb"), (72, "Chromium"), (73, "socat")):
            self.fake.run_exits = code
            self.fake.logs_fail = True
            with self.assertRaises(rb.BrowserError) as ctx:
                self.open()
            self.assertIn(word, str(ctx.exception))
            self.assertIn("終了コード %d" % code, str(ctx.exception))
        self.fake.run_exits = 5
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertIn("起動直後に終了しました（終了コード 5）", str(ctx.exception))

    def test_image_tag_mismatch_asks_for_setup_again(self):
        self.fake.images.clear()
        self.fake.images.add("qnapdl-browser:aaaaaaaaaaaa")   # older Hub-built image
        status = self.rb.status()
        self.assertFalse(status["image_ready"])
        self.assertTrue(status["update_needed"])
        self.assertEqual(status["reason"], "NASブラウザの更新が必要です（初期設定を再実行）")
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open()
        self.assertEqual(str(ctx.exception), "NASブラウザの更新が必要です（初期設定を再実行）")
        self.assertFalse([c for c in self.fake.docker_calls() if c[0] == "run"])

    def test_no_image_at_all_is_plain_setup_not_update(self):
        self.fake.images.clear()
        self.assertFalse(self.rb.status()["update_needed"])

    def test_connect_failure_stops_and_removes_container(self):
        def broken(ip):
            raise OSError("refused")
        self.rb._connect = broken
        ticks = iter(range(0, 1000))
        self.rb._clock = lambda: next(ticks)
        with self.assertRaises(rb.BrowserError):
            self.open()
        self.assertNotIn("qnapdl-browser", self.fake.containers)

    def test_download_deny_failure_aborts_start(self):
        self.ws.fail["Browser.setDownloadBehavior"] = "nope"
        with self.assertRaises(rb.BrowserError):
            self.open()
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_foreign_container_with_our_name_is_never_touched(self):
        self.fake.add_foreign("qnapdl-browser")
        with self.assertRaises(rb.BrowserError):
            self.open()
        self.assertIn("qnapdl-browser", self.fake.containers)
        self.assertEqual([c for c in self.fake.docker_calls() if c[0] in ("stop", "rm")], [])

    def test_leftover_managed_container_is_replaced(self):
        self.fake.containers["qnapdl-browser"] = {
            "labels": {cr.LABEL_KEY: "1"}, "running": True, "net": cr.BROWSER_NET,
            "ip": "172.30.0.9", "ports": {}, "extra_nets": []}
        self.open()
        self.assertEqual(self.fake.containers["qnapdl-browser"]["ip"], "172.30.0.2")

    def test_startup_cleanup_removes_a_surviving_managed_container(self):
        self.fake.containers["qnapdl-browser"] = {
            "labels": {cr.LABEL_KEY: "1"}, "running": True, "net": cr.BROWSER_NET,
            "ip": "172.30.0.9", "ports": {}, "extra_nets": []}
        self.assertTrue(self.rb.startup_cleanup())
        self.assertNotIn("qnapdl-browser", self.fake.containers)


class TabTests(BrowserBase):
    def test_close_tab_closes_target_and_frees_frame(self):
        self.open()
        self.feed("SESSION1")
        tab = self.rb._tabs["b1"]
        self.assertIsNotNone(tab.frame)
        self.rb.close_tab("b1")
        self.assertEqual(self.ws.calls("Target.closeTarget")[-1]["params"], {"targetId": "TARGET00001"})
        self.assertIsNone(tab.frame)
        self.assertEqual(self.rb._tabs, {})
        with self.assertRaises(rb.TabGone):
            self.rb.frame("b1", 0, wait=0)
        with self.assertRaises(rb.TabGone):
            self.rb.close_tab("b1")

    def test_tab_ids_are_validated(self):
        self.open()
        for bad in ("", "b", "../x", "b1;", None, 5, "TARGET00001"):
            with self.subTest(bad=bad), self.assertRaises(rb.BrowserError):
                self.rb.close_tab(bad)

    def test_destroyed_or_crashed_target_removes_the_tab(self):
        self.open()
        self.ws.emit("Target.targetDestroyed", {"targetId": "TARGET00001"})
        self.barrier()
        self.assertEqual(self.rb._tabs, {})

    def test_popups_opened_by_foreign_pages_are_closed_at_once(self):
        self.open()
        self.ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "POPUP000001", "type": "page", "openerId": "OTHER000001", "url": "https://ad.example/"}})
        self.barrier()
        self.assertIn({"targetId": "POPUP000001"}, [m["params"] for m in self.ws.calls("Target.closeTarget")])
        self.assertEqual([m for m in self.ws.calls("Page.navigate")], self.ws.calls("Page.navigate")[:1])
        self.ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "about:blank"}})
        self.barrier()
        self.assertEqual(len(self.ws.calls("Target.closeTarget")), 1)

    def test_non_https_navigation_is_sent_back_to_blank(self):
        self.open()
        for url in ("http://insecure.example/", "file:///etc/passwd", "chrome://downloads", "view-source:https://a.b/"):
            self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
                "targetId": "TARGET00001", "type": "page", "url": url, "title": "x"}})
            self.barrier()
        blanks = [m for m in self.ws.calls("Page.navigate") if m["params"] == {"url": "about:blank"}]
        self.assertEqual(len(blanks), 4)
        snapshot = self.rb.frame("b1", 0, wait=0)
        self.assertIn("ブロック", snapshot["blocked"])
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/ok", "title": "OK"}})
        self.barrier()
        self.assertEqual(self.rb._tabs["b1"].url, "https://example.com/ok")
        self.assertEqual(self.rb._tabs["b1"].title, "OK")

    def test_javascript_dialogs_are_dismissed(self):
        self.open()
        self.ws.emit("Page.javascriptDialogOpening", {"message": "x"}, "SESSION1")
        self.barrier()
        self.assertEqual(self.ws.calls("Page.handleJavaScriptDialog")[0]["params"], {"accept": False})

    def test_loading_state_tracks_frames(self):
        self.open()
        self.ws.emit("Page.frameStartedLoading", {"frameId": "F1"}, "SESSION1")
        self.ws.emit("Page.frameStartedLoading", {"frameId": "F2"}, "SESSION1")
        self.ws.emit("Page.frameStoppedLoading", {"frameId": "F2"}, "SESSION1")
        self.barrier()
        self.assertTrue(self.rb.frame("b1", 0, wait=0)["loading"])
        self.ws.emit("Page.frameStoppedLoading", {"frameId": "F1"}, "SESSION1")
        self.barrier()
        self.assertFalse(self.rb.frame("b1", 0, wait=0)["loading"])

    def test_navigate_validates_and_uses_the_tab_session(self):
        self.open()
        self.rb.navigate("b1", "https://example.org/x")
        self.assertEqual(self.ws.calls("Page.navigate")[-1]["params"], {"url": "https://example.org/x"})
        with self.assertRaises(ValueError):
            self.rb.navigate("b1", "http://example.org/")
        with self.assertRaises(rb.TabGone):
            self.rb.navigate("b9", "https://example.org/")


class ScreencastTests(BrowserBase):
    def test_poll_starts_screencast_with_fixed_parameters_and_returns_latest_frame_only(self):
        self.open()
        result = self.rb.frame("b1", 0, wait=0)
        self.assertIsNone(result["jpeg_base64"])
        self.assertEqual(self.ws.calls("Page.startScreencast")[0]["params"], {
            "format": "jpeg", "quality": 40, "maxWidth": 1280, "maxHeight": 2000, "everyNthFrame": 1})
        self.assertEqual(self.ws.calls("Target.activateTarget")[0]["params"], {"targetId": "TARGET00001"})
        for i, data in enumerate(("QUFB", "QkJC", "Q0ND"), 1):
            self.feed("SESSION1", data, seq_ack=i, width=1000, height=1800)
        result = self.rb.frame("b1", 0, wait=0)
        self.assertEqual(result["jpeg_base64"], "Q0ND")
        self.assertEqual((result["seq"], result["width"], result["height"]), (3, 1000, 1800))
        self.assertEqual(self.rb._tabs["b1"].frame, "Q0ND")
        again = self.rb.frame("b1", 3, wait=0)
        self.assertIsNone(again["jpeg_base64"])
        self.assertEqual(again["seq"], 3)
        self.assertEqual(len(self.ws.calls("Page.startScreencast")), 1)

    def test_frames_are_acked_immediately_without_any_poll(self):
        self.open()
        self.rb.frame("b1", 0, wait=0)
        before = len(self.ws.calls("Page.screencastFrameAck"))
        self.feed("SESSION1", "QUFB", seq_ack=41)
        self.feed("SESSION1", "QkJC", seq_ack=42)      # no poll in between
        acks = self.ws.calls("Page.screencastFrameAck")[before:]
        self.assertEqual([a["params"] for a in acks], [{"sessionId": 41}, {"sessionId": 42}])
        self.assertEqual(acks[0]["sessionId"], "SESSION1")
        self.rb.frame("b1", 2, wait=0)
        self.assertEqual(len(self.ws.calls("Page.screencastFrameAck")) - before, 2)   # each acked once
        self.assertEqual(self.rb._tabs["b1"].frame, "QkJC")      # only the latest frame is kept

    def test_bad_frame_data_is_dropped(self):
        self.open()
        self.rb.frame("b1", 0, wait=0)
        for bad in ("not base64!", "", None, 5, "A" * (rb.FRAME_MAX_CHARS + 1), "QUJD\"onerror="):
            self.feed("SESSION1", bad)
        self.assertEqual(self.rb._tabs["b1"].seq, 0)
        self.assertIsNone(self.rb._tabs["b1"].frame)

    def test_long_poll_wakes_when_a_frame_arrives(self):
        self.open()
        self.rb.frame("b1", 0, wait=0)
        got = []
        thread = threading.Thread(target=lambda: got.append(self.rb.frame("b1", 0, wait=5)))
        thread.start()
        import time
        time.sleep(0.2)
        self.feed("SESSION1", "QUFB")
        thread.join(5)
        self.assertEqual(got[0]["jpeg_base64"], "QUFB")

    def test_long_poll_is_released_when_the_tab_closes(self):
        self.open()
        self.rb.frame("b1", 0, wait=0)
        errors = []

        def poll():
            try:
                self.rb.frame("b1", 0, wait=5)
            except rb.TabGone as error:
                errors.append(error)
        thread = threading.Thread(target=poll)
        thread.start()
        import time
        time.sleep(0.2)
        self.rb.close_tab("b1")
        thread.join(5)
        self.assertEqual(len(errors), 1)

    def test_screencast_stops_five_seconds_after_the_last_poll(self):
        self.open()
        self.rb.frame("b1", 0, wait=0)
        self.clock.now += 4
        self.rb.tick()
        self.assertEqual(self.ws.calls("Page.stopScreencast"), [])
        self.clock.now += 2
        self.rb.tick()
        self.assertEqual(len(self.ws.calls("Page.stopScreencast")), 1)
        self.rb.tick()
        self.assertEqual(len(self.ws.calls("Page.stopScreencast")), 1)
        self.rb.frame("b1", 0, wait=0)       # a viewer returns: restarted
        self.assertEqual(len(self.ws.calls("Page.startScreencast")), 2)


class HistoryTargetTests(unittest.TestCase):
    """v0.1.52: back / forward step over redirect, Cloudflare check and duplicate entries in one gesture."""

    @staticmethod
    def entries(*urls):
        return [{"id": 10 + i, "url": u} for i, u in enumerate(urls)]

    def test_challenge_urls(self):
        for url in ("https://x.example/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1",
                    "https://x.example/page?__cf_chl_tk=abc", "https://x.example/page?a=1&__cf_chl_rt_tk=z"):
            self.assertTrue(rb.is_challenge_url(url), url)
        for url in ("https://x.example/page", "https://x.example/cdn-cgi/trace", "", None):
            self.assertFalse(rb.is_challenge_url(url), url)

    def test_back_skips_challenge_and_token_entries(self):
        e = self.entries("https://a.example/A", "https://a.example/cdn-cgi/challenge-platform/h/b/x",
                         "https://a.example/A?__cf_chl_tk=t", "https://a.example/B")
        self.assertEqual(rb.history_target(e, 3, -1), (10, 2))

    def test_duplicates_and_fragments_are_skipped(self):
        e = self.entries("https://a.example/A", "https://a.example/B", "https://a.example/B", "https://a.example/B#x")
        self.assertEqual(rb.history_target(e, 3, -1), (10, 2))
        self.assertEqual(rb.history_target(e, 0, 1), (11, 0))

    def test_about_blank_start_is_never_a_target(self):
        e = self.entries("about:blank", "https://a.example/A")
        self.assertIsNone(rb.history_target(e, 1, -1))
        e = self.entries("about:blank", "https://a.example/A", "https://a.example/B")
        self.assertEqual(rb.history_target(e, 2, -1), (11, 0))
        self.assertIsNone(rb.history_target(e, 1, -1))

    def test_forward_is_symmetric(self):
        e = self.entries("https://a.example/A", "https://a.example/cdn-cgi/challenge-platform/h/b/x",
                         "https://a.example/A?__cf_chl_tk=t", "https://a.example/B", "https://a.example/C")
        self.assertEqual(rb.history_target(e, 0, 1), (13, 2))
        self.assertEqual(rb.history_target(e, 3, 1), (14, 0))
        self.assertIsNone(rb.history_target(e, 4, 1))

    def test_only_challenge_entries_ahead_means_no_move(self):
        e = self.entries("https://a.example/A", "https://a.example/cdn-cgi/challenge-platform/h/b/x")
        self.assertIsNone(rb.history_target(e, 0, 1))

    def test_bad_input(self):
        for args in ((None, 0, -1), ([], 0, -1), ([{"id": 1, "url": "https://a/"}], 5, -1), ("x", 0, 1)):
            self.assertIsNone(rb.history_target(*args))
        self.assertIsNone(rb.history_target([{"id": True, "url": "https://a/x"}, {"url": "https://a/y"}], 1, -1))


class InputTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open()

    def mouse(self):
        return [m["params"] for m in self.ws.calls("Input.dispatchMouseEvent")]

    def test_tap_reports_whether_the_focused_element_is_editable(self):
        self.ws.eval_map = [("activeElement", True)]
        self.assertEqual(self.rb.send_input("b1", {"type": "tap", "x": 1, "y": 1}),
                         {"ok": True, "editable": True})
        evaluate = [m for m in self.ws.calls("Runtime.evaluate")
                    if "activeElement" in m["params"]["expression"]][-1]
        self.assertTrue(evaluate["params"]["returnByValue"])
        self.assertEqual(evaluate["sessionId"], "SESSION1")
        self.assertIn("isContentEditable", evaluate["params"]["expression"])
        self.assertIs(self.rb.frame("b1", 0, wait=0)["editable"], True)
        self.ws.eval_map = [("activeElement", False)]
        self.assertEqual(self.rb.send_input("b1", {"type": "tap", "x": 1, "y": 1})["editable"], False)
        self.ws.eval_map = [("activeElement", "yes")]       # anything but True is not editable
        self.assertEqual(self.rb.send_input("b1", {"type": "tap", "x": 1, "y": 1})["editable"], False)
        self.assertIs(self.rb.frame("b1", 0, wait=0)["editable"], False)

    def test_tap_is_move_press_release(self):
        self.rb.send_input("b1", {"type": "tap", "x": 120, "y": 340.5})
        events = self.mouse()
        self.assertEqual([e["type"] for e in events], ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertEqual((events[1]["x"], events[1]["y"], events[1]["button"], events[1]["clickCount"]),
                         (120.0, 340.5, "left", 1))
        self.assertEqual(self.ws.calls("Input.dispatchMouseEvent")[0]["sessionId"], "SESSION1")

    def test_scroll_is_a_mouse_wheel(self):
        self.rb.send_input("b1", {"type": "scroll", "x": 10, "y": 20, "dx": 0, "dy": -300})
        self.assertEqual(self.mouse()[0], {"type": "mouseWheel", "x": 10.0, "y": 20.0,
                                           "deltaX": 0.0, "deltaY": -300.0})

    def test_text_and_keys(self):
        self.rb.send_input("b1", {"type": "text", "text": "こんにちは abc"})
        self.assertEqual(self.ws.calls("Input.insertText")[0]["params"], {"text": "こんにちは abc"})
        self.rb.send_input("b1", {"type": "key", "key": "Enter"})
        keys = [m["params"] for m in self.ws.calls("Input.dispatchKeyEvent")]
        self.assertEqual(keys[0]["type"], "keyDown")
        self.assertEqual(keys[0]["text"], "\r")
        self.assertEqual(keys[0]["windowsVirtualKeyCode"], 13)
        self.assertEqual(keys[1]["type"], "keyUp")
        self.rb.send_input("b1", {"type": "key", "key": "ArrowLeft"})
        self.assertEqual([m["params"]["type"] for m in self.ws.calls("Input.dispatchKeyEvent")][2:],
                         ["rawKeyDown", "keyUp"])

    def test_history_and_reload(self):
        self.rb.send_input("b1", {"type": "back"})
        self.assertEqual(self.ws.calls("Page.navigateToHistoryEntry")[-1]["params"], {"entryId": 5})
        self.rb.send_input("b1", {"type": "forward"})
        self.assertEqual(self.ws.calls("Page.navigateToHistoryEntry")[-1]["params"], {"entryId": 7})
        self.ws.history = {"currentIndex": 0, "entries": [{"id": 5}]}
        self.assertEqual(self.rb.send_input("b1", {"type": "back"}), {"ok": True, "moved": False})
        # v0.1.50: the blank start page is not a page to go back to
        self.ws.history = {"currentIndex": 1, "entries": [{"id": 4, "url": "about:blank"}, {"id": 5, "url": "https://a.example/1"}]}
        self.assertEqual(self.rb.send_input("b1", {"type": "back"}), {"ok": True, "moved": False})
        self.rb.send_input("b1", {"type": "reload"})
        self.assertEqual(len(self.ws.calls("Page.reload")), 1)

    def test_send_input_walks_over_challenge_entries(self):
        self.ws.history = {"currentIndex": 3, "entries": [
            {"id": 1, "url": "https://a.example/A"}, {"id": 2, "url": "https://a.example/cdn-cgi/challenge-platform/h/b/x"},
            {"id": 3, "url": "https://a.example/A?__cf_chl_tk=t"}, {"id": 4, "url": "https://a.example/B"}]}
        result = self.rb.send_input("b1", {"type": "back"})
        self.assertEqual((result["moved"], result["skipped"]), (True, 2))
        self.assertEqual(self.ws.calls("Page.navigateToHistoryEntry")[-1]["params"], {"entryId": 1})
        self.ws.history = {"currentIndex": 0, "entries": self.ws.history["entries"][:1]}
        self.assertEqual(self.rb.send_input("b1", {"type": "back"}), {"ok": True, "moved": False})

    def test_validation_rejects_bad_types_and_ranges(self):
        bad = [
            {"type": "eval", "expression": "1"}, {"type": "Page.navigate"}, {"type": 5}, {}, None,
            {"type": "tap", "x": -1, "y": 0}, {"type": "tap", "x": "1", "y": 0},
            {"type": "tap", "x": True, "y": 0}, {"type": "tap", "x": float("nan"), "y": 0},
            {"type": "tap", "x": 1e9, "y": 0}, {"type": "tap", "x": 1},
            {"type": "scroll", "x": 1, "y": 1, "dy": 99999}, {"type": "scroll", "x": 1, "y": 1, "dx": "a"},
            {"type": "text", "text": ""}, {"type": "text", "text": "a" * 501}, {"type": "text", "text": 5},
            {"type": "text", "text": "a\x00b"}, {"type": "key", "key": "F12"}, {"type": "key"},
            {"type": "key", "key": "Control"},
        ]
        before = len(self.ws.sent)
        for event in bad:
            with self.subTest(event=event), self.assertRaises(rb.BrowserError):
                self.rb.send_input("b1", event)
        self.assertEqual(len(self.ws.sent), before)

    def test_only_fixed_cdp_methods_are_ever_sent(self):
        for event in ({"type": "tap", "x": 1, "y": 1}, {"type": "scroll", "x": 1, "y": 1, "dy": 5},
                      {"type": "text", "text": "a"}, {"type": "key", "key": "Tab"},
                      {"type": "back"}, {"type": "reload"}):
            self.rb.send_input("b1", event)
        self.rb.frame("b1", 0, wait=0)
        self.rb.links("b1")
        allowed = {"Target.setDiscoverTargets", "Browser.setDownloadBehavior", "Target.getTargets",
                   "Target.createTarget", "Target.attachToTarget", "Page.enable",
                   "Emulation.setFocusEmulationEnabled", "Page.navigate", "Input.dispatchMouseEvent",
                   "Input.dispatchKeyEvent", "Input.insertText", "Page.getNavigationHistory",
                   "Page.navigateToHistoryEntry", "Page.reload", "Target.activateTarget",
                   "Page.startScreencast", "Page.stopScreencast", "Page.screencastFrameAck",
                   "Runtime.evaluate", "Target.closeTarget", "Page.handleJavaScriptDialog",
                   "Fetch.enable", "Network.enable", "Network.setBlockedURLs",
                   "Emulation.setEmulatedMedia"}
        self.assertLessEqual(set(self.ws.methods()), allowed)


class LinkTests(BrowserBase):
    def test_links_extracted_from_hrefs_deduplicated(self):
        self.open()
        self.ws.eval_value = [
            "https://rapidgator.net/file/0123456789abcdef0123456789abcdef/a.rar.html",
            "https://rapidgator.net/file/0123456789abcdef0123456789abcdef?x=1",
            "https://rg.to/file/12345", "https://example.com/other", "http://rapidgator.net/file/9", 5, None]
        result = self.rb.links("b1")
        self.assertEqual(result["items"], ["https://rapidgator.net/file/0123456789abcdef0123456789abcdef",
                                           "https://rapidgator.net/file/12345"])
        evaluate = self.ws.calls("Runtime.evaluate")[0]["params"]
        self.assertTrue(evaluate["returnByValue"])
        self.assertIn("5000", evaluate["expression"])
        self.assertEqual(self.ws.calls("Runtime.evaluate")[0]["sessionId"], "SESSION1")

    def test_many_links_are_chunked_under_the_input_cap_and_capped_at_100(self):
        self.open()
        hrefs = ["https://rapidgator.net/file/%d/" % i + "x" * 100 for i in range(3000)]
        self.ws.eval_value = hrefs
        result = self.rb.links("b1")
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["scanned"], 3000)

    def test_non_list_result_is_empty(self):
        self.open()
        self.ws.eval_value = "oops"
        self.assertEqual(self.rb.links("b1")["items"], [])


class IdleTests(BrowserBase):
    def test_no_tabs_for_five_minutes_stops_and_removes_the_container(self):
        self.open()
        self.rb.close_tab("b1")
        self.clock.now += 299
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.clock.now += 2
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.assertTrue(self.ws.closed)
        self.assertIn(["stop", "-t", "5", "qnapdl-browser"], self.fake.docker_calls())

    def test_no_viewer_activity_for_ten_minutes_closes_targets_then_stops(self):
        self.open()
        self.open("https://example.com/b")
        self.clock.now += 599
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")
        self.clock.now += 2
        self.rb.tick()
        closed = [m["params"]["targetId"] for m in self.ws.calls("Target.closeTarget")]
        self.assertEqual(sorted(closed), ["TARGET00001", "TARGET00002"])
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.assertEqual(self.rb.status()["tabs"], [])

    def test_viewer_activity_keeps_it_alive(self):
        self.open()
        for _ in range(5):
            self.clock.now += 300
            self.rb.frame("b1", 0, wait=0)
            self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "running")

    def test_status_polling_does_not_count_as_activity(self):
        self.open()
        self.clock.now += 400
        self.rb.status()
        self.clock.now += 400
        self.rb.status()
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_lost_connection_is_cleaned_up(self):
        self.open()
        self.ws.close()
        import time
        for _ in range(50):
            if self.rb._conn.closed:
                break
            time.sleep(0.02)
        self.rb.tick()
        self.assertEqual(self.rb.status()["phase"], "stopped")
        self.assertNotIn("qnapdl-browser", self.fake.containers)

    def test_hub_shutdown_stops_the_container_and_is_idempotent(self):
        self.open()
        self.rb.shutdown()
        self.assertNotIn("qnapdl-browser", self.fake.containers)
        self.rb.shutdown()
        self.assertEqual(self.rb.status()["phase"], "stopped")

    def test_can_open_again_after_stop(self):
        self.open()
        self.rb.stop()
        self.ws2 = FakeWS()
        self.rb._connect = lambda ip: self.ws2
        self.rb.open_tab("https://example.com/again")
        self.assertEqual(self.rb.status()["phase"], "running")
        self.assertEqual(len(self.ws2.calls("Page.navigate")), 1)


class SetupTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.fake.images.clear()

    def test_setup_builds_checks_sandbox_and_reports_steps(self):
        self.rb.run_setup()
        status = self.rb.status()
        self.assertEqual({s["id"]: s["status"] for s in status["steps"]},
                         {"precheck": "done", "network": "done", "build": "done", "sandbox": "done", "done": "done"})
        self.assertIsNone(status["error"])
        self.assertTrue(status["image_ready"])
        calls = self.fake.docker_calls()
        build = [c for c in calls if c[0] == "build"][0]
        self.assertEqual(build, ["build", "--label", cr.LABEL, "--tag", self.tag, str(self.ctx)])
        self.assertIn(cr.BROWSER_NET, self.fake.networks)
        self.assertEqual([c for c in calls if c[0] == "run"][0][-1], "selftest")

    def test_build_failure_is_reported_on_its_step(self):
        self.fake.build_fail = True
        self.rb.run_setup()
        status = self.rb.status()
        steps = {s["id"]: s for s in status["steps"]}
        self.assertEqual(steps["build"]["status"], "failed")
        self.assertIn("ブラウザイメージを作成できません", status["error"])
        self.assertFalse(status["image_ready"])

    def test_userns_refusal_fails_setup_with_clear_message(self):
        self.fake.selftest_code = cr.BROWSER_EXIT_NO_USERNS
        self.rb.run_setup()
        status = self.rb.status()
        self.assertIn("ユーザー名前空間", status["error"])
        self.assertEqual({s["id"]: s["status"] for s in status["steps"]}["sandbox"], "failed")

    def test_no_container_station(self):
        fake = FakeDocker(has_cs=False)
        rt = cr.Runtime(fake, isfile=fake.isfile, access=lambda p, m: True, meminfo_path=str(self.meminfo))
        browser = rb.RemoteBrowser(rt, self.ctx, meminfo_path=str(self.meminfo), monitor=False)
        browser.run_setup()
        self.assertIn("Container Station", browser.status()["error"])
        self.assertFalse(browser.status()["container_station"]["available"])

    def test_concurrent_setup_is_refused(self):
        self.rb._setup_active = True
        with self.assertRaises(rb.SetupBusy):
            self.rb.start_setup()

    def test_old_images_are_pruned_after_build(self):
        self.fake.images.add("qnapdl-browser:aaaaaaaaaaaa")
        self.rb.run_setup()
        self.assertNotIn("qnapdl-browser:aaaaaaaaaaaa", self.fake.images)
        self.assertIn(self.tag, self.fake.images)

    def test_status_reason_when_memory_is_low(self):
        self.fake.images.add(self.tag)
        self.set_mem(1024 * 1024)
        status = self.rb.status()
        self.assertFalse(status["can_open"])
        self.assertIn("空きメモリ", status["reason"])


if __name__ == "__main__":
    unittest.main()


class ViewportTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open()

    def test_mobile_viewport_sets_metrics_touch_and_scaled_screencast(self):
        self.assertEqual(self.rb.set_viewport("b1", 390, 700, 3, "high"), {"ok": True, "changed": True})
        override = self.ws.calls("Emulation.setDeviceMetricsOverride")[0]
        self.assertEqual(override["params"], {"width": 390, "height": 700,
                                              "deviceScaleFactor": 2.0, "mobile": True})
        self.assertEqual(override["sessionId"], "SESSION1")
        self.assertEqual(self.ws.calls("Emulation.setTouchEmulationEnabled")[0]["params"], {"enabled": True})
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.ws.calls("Page.startScreencast")[-1]["params"], {
            "format": "jpeg", "quality": 70, "maxWidth": 780, "maxHeight": 1400, "everyNthFrame": 1})

    def test_desktop_viewport_quality_and_no_touch_emulation(self):
        self.rb.set_viewport("b1", 1200, 800, 1)
        self.assertEqual(self.ws.calls("Emulation.setDeviceMetricsOverride")[0]["params"], {
            "width": 1200, "height": 800, "deviceScaleFactor": 1.0, "mobile": False})
        self.assertEqual(self.ws.calls("Emulation.setTouchEmulationEnabled"), [])
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.ws.calls("Page.startScreencast")[-1]["params"], {
            "format": "jpeg", "quality": 40, "maxWidth": 1200, "maxHeight": 800, "everyNthFrame": 1})

    def test_boundary_at_760_is_desktop_and_759_is_mobile(self):
        self.rb.set_viewport("b1", 759, 600, 2)
        self.rb.set_viewport("b1", 760, 600, 2)
        flags = [m["params"]["mobile"] for m in self.ws.calls("Emulation.setDeviceMetricsOverride")]
        self.assertEqual(flags, [True, False])
        self.assertEqual([m["params"]["enabled"] for m in self.ws.calls("Emulation.setTouchEmulationEnabled")],
                         [True, False])

    def test_resize_restarts_a_running_screencast_and_same_size_is_a_noop(self):
        self.rb.set_viewport("b1", 390, 700, 2, "high")
        self.rb.frame("b1", 0, wait=0)
        starts = len(self.ws.calls("Page.startScreencast"))
        self.assertEqual(self.rb.set_viewport("b1", 390, 700, 2, "high")["changed"], False)
        self.assertEqual(len(self.ws.calls("Emulation.setDeviceMetricsOverride")), 1)
        self.rb.set_viewport("b1", 700, 390, 2, "high")
        self.assertEqual(len(self.ws.calls("Page.stopScreencast")), 1)
        self.assertEqual(len(self.ws.calls("Page.startScreencast")), starts + 1)
        self.assertEqual(self.ws.calls("Page.startScreencast")[-1]["params"]["maxWidth"], 1400)

    def test_validation(self):
        before = len(self.ws.sent)
        for width, height, dpr in ((319, 700, 2), (1601, 700, 2), (390, 319, 2), (390, 2401, 2),
                                   (390.5, 700, 2), ("390", 700, 2), (True, 700, 2), (390, 700, 0.9),
                                   (390, 700, 3.1), (390, 700, "2"), (390, 700, None), (390, 700, float("nan")),
                                   (None, 700, 2)):
            with self.subTest(v=(width, height, dpr)), self.assertRaises(rb.BrowserError):
                self.rb.set_viewport("b1", width, height, dpr)
        with self.assertRaises(rb.BrowserError):
            self.rb.set_viewport("../x", 390, 700, 2)
        with self.assertRaises(rb.TabGone):
            self.rb.set_viewport("b9", 390, 700, 2)
        self.assertEqual(len(self.ws.sent), before)
        for edge in ((320, 320, 1), (1600, 2400, 3)):
            self.rb.set_viewport("b1", *edge)

    def test_no_browser_identity_override_is_ever_sent(self):
        self.rb.set_viewport("b1", 390, 700, 3)
        self.assertFalse([m for m in self.ws.methods() if "Agent" in m or "Override" in m and "Metrics" not in m])


class FrameImageTests(BrowserBase):
    def test_binary_frame_none_when_nothing_newer_and_bytes_when_fresh(self):
        self.open()
        result = self.rb.frame_image("b1", 0, wait=0)
        self.assertIsNone(result["jpeg"])
        self.assertNotIn("jpeg_base64", result)
        self.feed("SESSION1", "QUJD", seq_ack=1, width=390, height=700)
        result = self.rb.frame_image("b1", 0, wait=0)
        self.assertEqual(result["jpeg"], b"ABC")
        self.assertEqual((result["seq"], result["width"], result["height"]), (1, 390, 700))
        self.assertIsNone(self.rb.frame_image("b1", 1, wait=0)["jpeg"])

    def test_poll_wakes_on_metadata_change_only_when_a_rev_is_given(self):
        self.open()
        first = self.rb.frame("b1", 0, wait=0)
        got = []
        thread = threading.Thread(target=lambda: got.append(self.rb.frame("b1", 0, wait=5, rev=first["rev"])))
        thread.start()
        import time
        time.sleep(0.2)
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/b", "title": "B"}})
        thread.join(5)
        self.assertEqual(got[0]["url"], "https://example.com/b")
        self.assertIsNone(got[0]["jpeg_base64"])
        self.assertNotEqual(got[0]["rev"], first["rev"])


class InterceptTests(BrowserBase):
    HOSTER = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.open()

    def paused(self, url, request_id="req1", resource="Document", session="SESSION1"):
        self.ws.emit("Fetch.requestPaused", {"requestId": request_id, "resourceType": resource,
                                             "request": {"url": url}}, session)
        self.barrier()

    def test_fetch_is_enabled_per_tab_with_exactly_the_hoster_document_patterns(self):
        calls = self.ws.calls("Fetch.enable")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sessionId"], "SESSION1")
        self.assertEqual(calls[0]["params"], {"patterns": [
            {"urlPattern": "*://rapidgator.net/file/*", "resourceType": "Document"},
            {"urlPattern": "*://*.rapidgator.net/file/*", "resourceType": "Document"},
            {"urlPattern": "*://rg.to/file/*", "resourceType": "Document"}]})
        self.assertLess(self.ws.methods().index("Fetch.enable"), self.ws.methods().index("Page.navigate"))

    def test_fetch_enable_failure_does_not_break_the_tab(self):
        self.ws.fail["Fetch.enable"] = "boom"
        self.assertEqual(self.open("https://example.com/2")["id"], "b2")
        self.assertTrue(any("Fetch.enable" in line for line in self.rb.log_lines()))

    def test_hoster_document_is_aborted_and_recorded_normalized(self):
        self.paused(self.HOSTER.upper().replace("HTTPS", "https") + "/name.rar?x=1", "req9")
        fail = self.ws.calls("Fetch.failRequest")
        self.assertEqual([m["params"] for m in fail], [{"requestId": "req9", "errorReason": "Aborted"}])
        self.assertEqual(fail[0]["sessionId"], "SESSION1")
        self.assertEqual(self.ws.calls("Fetch.continueRequest"), [])
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "hoster", "url": self.HOSTER}])
        result = self.rb.frame("b1", 0, wait=0)
        self.assertEqual(result["intercepts"], 1)

    def test_everything_else_is_continued_never_blocked(self):
        self.paused("https://rapidgator.net/file/not-an-id-at-all", "r1")
        self.paused(self.HOSTER, "r2", resource="Image")
        self.assertEqual(self.ws.calls("Fetch.failRequest"), [])
        self.assertEqual([m["params"] for m in self.ws.calls("Fetch.continueRequest")],
                         [{"requestId": "r1"}, {"requestId": "r2"}])
        self.assertEqual(self.rb.intercepts("b1"), [])

    def test_events_are_capped_at_20_with_monotonic_ids_and_deduplicated(self):
        for index in range(25):
            self.paused("https://rapidgator.net/file/%d" % (index + 1), "r%d" % index)
        items = self.rb.intercepts("b1")
        self.assertEqual(len(items), 20)
        self.assertEqual([i["id"] for i in items], list(range(6, 26)))
        self.assertEqual(self.rb.intercepts("b1", 24), [i for i in items if i["id"] == 25])
        self.paused("https://rapidgator.net/file/25", "dup")        # same URL again within 2 s
        self.assertEqual(self.rb.frame("b1", 0, wait=0)["intercepts"], 25)
        self.clock.now += 3
        self.paused("https://rapidgator.net/file/25", "again")
        self.assertEqual(self.rb.frame("b1", 0, wait=0)["intercepts"], 26)

    def test_open_intercept_bypasses_the_pattern_once_for_that_exact_url(self):
        self.paused(self.HOSTER)
        self.assertEqual(self.rb.open_intercept("b1", 1), {"id": "b1", "url": self.HOSTER})
        self.assertEqual(self.ws.calls("Page.navigate")[-1]["params"], {"url": self.HOSTER})
        self.paused("https://rapidgator.net/file/999", "other")      # a different URL is still caught
        self.assertEqual(len(self.ws.calls("Fetch.failRequest")), 2)
        self.paused(self.HOSTER, "pass1")
        self.assertEqual([m["params"] for m in self.ws.calls("Fetch.continueRequest")], [{"requestId": "pass1"}])
        self.paused(self.HOSTER, "pass2")                              # one time only
        self.assertEqual(len(self.ws.calls("Fetch.failRequest")), 3)

    def test_bypass_expires(self):
        self.paused(self.HOSTER)
        self.rb.open_intercept("b1", 1)
        self.clock.now += rb.BYPASS_TTL + 1
        self.paused(self.HOSTER, "late")
        self.assertEqual(self.ws.calls("Fetch.continueRequest"), [])

    def test_open_intercept_only_for_hosters_and_valid_ids(self):
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001", "guid": "g",
                                                   "url": "https://files.example/a.zip"})
        self.barrier()
        with self.assertRaises(rb.BrowserError):
            self.rb.open_intercept("b1", 1)
        for bad in (0, -1, "1", True, None, 99):
            with self.subTest(bad=bad), self.assertRaises(rb.BrowserError):
                self.rb.intercept("b1", bad)

    def test_download_will_begin_records_an_event_downloads_stay_denied(self):
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001", "guid": "g",
                                                   "url": "https://files.example/a.zip",
                                                   "suggestedFilename": "a.zip"})
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001", "url": "blob:https://x/1"})
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001", "url": "http://plain.example/x"})
        self.barrier()
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "download",
                                                     "url": "https://files.example/a.zip"}])
        self.assertEqual(self.ws.calls("Browser.setDownloadBehavior")[0]["params"]["behavior"], "deny")

    def test_download_from_unknown_frame_goes_to_the_most_recently_polled_tab(self):
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "SUBFRAME0001", "url": "https://files.example/b.zip"})
        self.barrier()
        self.assertEqual(self.rb.intercepts("b1")[0]["kind"], "download")

    def test_intercepts_vanish_with_the_tab(self):
        self.paused(self.HOSTER)
        self.rb.close_tab("b1")
        with self.assertRaises(rb.TabGone):
            self.rb.intercepts("b1")


class PopupTests(BrowserBase):
    HOSTER = "https://rapidgator.net/file/12345"

    def setUp(self):
        super().setUp()
        self.open()

    def popup(self, url="", target="POPUP000001"):
        self.ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": target, "type": "page", "openerId": "TARGET00001", "url": url}})
        self.barrier()

    def changed(self, url, target="POPUP000001"):
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": target, "type": "page", "openerId": "TARGET00001", "url": url}})
        self.barrier()

    def closed(self):
        return [m["params"]["targetId"] for m in self.ws.calls("Target.closeTarget")]

    def test_popup_with_https_url_is_closed_recorded_and_opener_not_navigated(self):
        self.popup()
        self.assertEqual(self.closed(), [])                   # still waiting for its URL
        self.changed("about:blank")
        self.assertEqual(self.closed(), [])
        self.changed("https://shop.example/item")
        self.assertEqual(self.closed(), ["POPUP000001"])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)       # only the initial navigation
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "popup", "url": "https://shop.example/item"}])
        self.changed("https://shop.example/other")             # handled once only
        self.assertEqual(len(self.rb.intercepts("b1")), 1)
        # the viewer opens it: the tab navigates, no Fetch bypass for a non-hoster
        self.assertEqual(self.rb.open_intercept("b1", 1), {"id": "b1", "url": "https://shop.example/item"})
        nav = self.ws.calls("Page.navigate")[-1]
        self.assertEqual((nav["params"], nav["sessionId"]), ({"url": "https://shop.example/item"}, "SESSION1"))
        self.assertEqual(self.rb._tabs["b1"].bypass, {})

    def test_popup_created_with_its_url_resolves_at_once(self):
        self.popup("https://shop.example/now")
        self.assertEqual(self.closed(), ["POPUP000001"])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)
        self.assertEqual(self.rb.intercepts("b1")[0]["kind"], "popup")

    def test_hoster_popup_becomes_an_intercept_not_a_navigation(self):
        self.popup()
        self.changed(self.HOSTER + "/x.html")
        self.assertEqual(self.closed(), ["POPUP000001"])
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "hoster", "url": self.HOSTER}])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)       # only the initial navigation

    def test_unsafe_popup_url_is_closed_and_noted_as_blocked(self):
        for url in ("http://insecure.example/", "https://192.168.0.1/", "chrome://downloads"):
            with self.subTest(url=url):
                self.popup(url, target="POPUP%06d" % abs(hash(url) % 10 ** 6))
        self.assertEqual(len(self.closed()), 3)
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)
        self.assertEqual(self.rb.intercepts("b1"), [])
        self.assertIn("ポップアップ", self.rb.frame("b1", 0, wait=0)["blocked"])

    def test_popup_without_a_url_is_closed_after_the_wait(self):
        import time
        old, rb.POPUP_WAIT = rb.POPUP_WAIT, 0.05
        self.addCleanup(setattr, rb, "POPUP_WAIT", old)
        self.popup()
        for _ in range(100):
            if self.closed():
                break
            time.sleep(0.02)
        self.assertEqual(self.closed(), ["POPUP000001"])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)
        self.assertIn("ポップアップ", self.rb.frame("b1", 0, wait=0)["blocked"])

    def test_popup_whose_opener_tab_is_gone_is_closed_without_effect(self):
        self.popup()
        self.rb.close_tab("b1")
        self.changed("https://shop.example/late")
        self.assertIn("POPUP000001", self.closed())
        with self.assertRaises(rb.TabGone):
            self.rb.intercepts("b1")


# ======================================================================= v0.1.24
import rb_blocklist
import time as _time


class FakeRules:
    """Stands in for storage.Store: settings() and site_rule_for(host)."""

    def __init__(self):
        self.options = {"block_ads": True}
        self.rules = {}

    def settings(self):
        return dict(self.options)

    def site_rule_for(self, host):
        labels = host.split(".")
        for start in range(len(labels) - 1):
            rule = self.rules.get(".".join(labels[start:]))
            if rule:
                return dict(rule)
        return None


def rule(host, **fields):
    base = {"host": host, "auto_send": False, "popup": "ask", "block_ads": True}
    base.update(fields)
    return base


class TickClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1.0
        return self.now


class QualityTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open()

    def params(self):
        return self.ws.calls("Page.startScreencast")[-1]["params"]

    def test_default_is_speed_one_x_and_jpeg_40(self):
        self.rb.set_viewport("b1", 390, 700, 3)
        self.assertEqual(self.ws.calls("Emulation.setDeviceMetricsOverride")[0]["params"]["deviceScaleFactor"], 1.0)
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.params(), {"format": "jpeg", "quality": 40, "maxWidth": 390,
                                         "maxHeight": 700, "everyNthFrame": 1})

    def test_each_mode_sets_scale_and_quality(self):
        for mode, scale, quality in (("speed", 1.0, 40), ("standard", 1.5, 55), ("high", 2.0, 70)):
            with self.subTest(mode=mode):
                self.rb.set_viewport("b1", 400, 800, 3, mode)
                override = self.ws.calls("Emulation.setDeviceMetricsOverride")[-1]["params"]
                self.assertEqual(override["deviceScaleFactor"], scale)
                self.rb.frame("b1", 0, wait=0)
                self.assertEqual(self.params()["quality"], quality)
                self.assertEqual(self.params()["maxWidth"], int(round(400 * scale)))
                self.assertEqual(self.params()["maxHeight"], int(round(800 * scale)))

    def test_scale_never_exceeds_the_device_pixel_ratio(self):
        self.rb.set_viewport("b1", 1200, 800, 1, "high")
        self.assertEqual(self.ws.calls("Emulation.setDeviceMetricsOverride")[-1]["params"]["deviceScaleFactor"], 1.0)

    def test_mode_is_remembered_per_tab_and_changing_it_restarts_the_screencast(self):
        self.rb.set_viewport("b1", 400, 800, 2, "high")
        self.rb.frame("b1", 0, wait=0)
        starts = len(self.ws.calls("Page.startScreencast"))
        self.rb.set_viewport("b1", 400, 800, 2)          # no quality given: keeps "high"
        self.assertEqual(len(self.ws.calls("Page.startScreencast")), starts)
        self.assertEqual(self.rb.set_viewport("b1", 400, 800, 2, "speed")["changed"], True)
        self.assertEqual(len(self.ws.calls("Page.startScreencast")), starts + 1)
        self.assertEqual(self.params()["quality"], 40)

    def test_unknown_quality_is_refused_before_anything_is_sent(self):
        before = len(self.ws.sent)
        for bad in ("ultra", "", 5, "SPEED", ["high"], {"a": 1}):
            with self.subTest(q=bad), self.assertRaises(rb.BrowserError):
                self.rb.set_viewport("b1", 400, 800, 2, bad)
        self.assertEqual(len(self.ws.sent), before)


class PerfSetupTests(BrowserBase):
    def test_reduced_motion_is_emulated_on_every_tab(self):
        self.open()
        call = self.ws.calls("Emulation.setEmulatedMedia")[0]
        self.assertEqual(call["params"], {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
        self.assertEqual(call["sessionId"], "SESSION1")

    def test_emulated_media_failure_does_not_break_the_tab(self):
        self.ws.fail["Emulation.setEmulatedMedia"] = "nope"
        self.assertEqual(self.open()["id"], "b1")


class AdBlockTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.rules = FakeRules()
        self.rb.rules = self.rules

    def blocked(self):
        return [m for m in self.ws.calls("Network.setBlockedURLs")]

    def test_blocked_by_default_with_the_hub_blocklist_patterns(self):
        self.open()
        methods = self.ws.methods()
        self.assertLess(methods.index("Network.enable"), methods.index("Network.setBlockedURLs"))
        self.assertLess(methods.index("Network.setBlockedURLs"), methods.index("Page.navigate"))
        call = self.blocked()[0]
        self.assertEqual(call["sessionId"], "SESSION1")
        self.assertEqual(call["params"]["urls"], list(rb_blocklist.patterns()))
        self.assertIn("*://doubleclick.net/*", call["params"]["urls"])
        self.assertIn("*://*.doubleclick.net/*", call["params"]["urls"])

    def test_global_switch_off_blocks_nothing(self):
        self.rules.options["block_ads"] = False
        self.open()
        self.assertEqual(self.ws.calls("Network.enable"), [])
        self.assertEqual(self.blocked(), [])

    def test_site_exception_turns_blocking_off_for_that_host_only(self):
        self.rules.rules["example.com"] = rule("example.com", block_ads=False)
        self.open("https://example.com/a")
        self.assertEqual(self.blocked(), [])
        self.open("https://other.example.org/")
        self.assertEqual(len(self.blocked()), 1)

    def test_navigating_to_a_host_with_an_exception_clears_the_patterns_and_back(self):
        self.rules.rules["free.example.net"] = rule("free.example.net", block_ads=False)
        self.open("https://example.com/a")
        self.assertEqual(len(self.blocked()), 1)
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://www.free.example.net/x", "title": "t"}})
        self.barrier()
        self.assertEqual(self.blocked()[-1]["params"], {"urls": []})
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/y", "title": "t"}})
        self.barrier()
        self.assertEqual(self.blocked()[-1]["params"]["urls"], list(rb_blocklist.patterns()))
        # the same host again changes nothing
        count = len(self.blocked())
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/z", "title": "t"}})
        self.barrier()
        self.assertEqual(len(self.blocked()), count)

    def test_blocking_failure_is_logged_not_fatal(self):
        self.ws.fail["Network.setBlockedURLs"] = "unsupported"
        self.assertEqual(self.open()["id"], "b1")
        self.assertTrue(any("ad blocking failed" in line for line in self.rb.log_lines()))

    def test_hoster_domains_are_never_in_the_patterns(self):
        urls = list(self.blocked() and self.blocked()[0]["params"]["urls"] or rb_blocklist.patterns())
        for pattern in urls:
            for hoster in ("rapidgator.net", "rg.to"):
                self.assertNotIn(hoster, pattern)
        # and the hoster Fetch patterns still pause only hoster documents
        self.open()
        self.assertEqual(self.ws.calls("Fetch.enable")[0]["params"]["patterns"],
                         [dict(p) for p in rb.FETCH_PATTERNS])


class AutoSendTests(BrowserBase):
    HOSTER = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.rules = FakeRules()
        self.rb.rules = self.rules
        self.rb._spawn = lambda fn: fn()
        self.sent = []
        self.outcome = "sent"
        self.rb.auto_send = self.fake_send
        self.open("https://blog.example.com/post")

    def fake_send(self, url):
        self.sent.append(url)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def paused(self, url=None):
        self.ws.emit("Fetch.requestPaused", {"requestId": "r%d" % (len(self.ws.sent)), "resourceType": "Document",
                                             "request": {"url": url or self.HOSTER + "/a.rar.html"}}, "SESSION1")
        self.barrier()

    def test_no_rule_means_the_sheet_and_no_send(self):
        self.paused()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "hoster", "url": self.HOSTER}])

    def test_auto_send_rule_sends_normalized_url_and_reports_auto_sent(self):
        self.rules.rules["example.com"] = rule("example.com", auto_send=True)      # parent domain matches
        self.paused()
        self.assertEqual(self.sent, [self.HOSTER])
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "auto_sent", "url": self.HOSTER, "dup": False}])
        # the page itself was still aborted, never rendered
        self.assertEqual(len(self.ws.calls("Fetch.failRequest")), 1)

    def test_already_sent_is_reported_as_dup(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        self.outcome = "dup"
        self.paused()
        self.assertEqual(self.rb.intercepts("b1")[0]["dup"], True)

    def test_engine_not_ready_falls_back_to_the_sheet_silently(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        self.outcome = None
        self.paused()
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "hoster", "url": self.HOSTER}])
        self.assertIsNone(self.rb.frame("b1", 0, wait=0)["blocked"])

    def test_failure_falls_back_to_the_sheet_and_tells_why(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        self.outcome = ValueError("保存先を選択してください")
        self.paused()
        self.assertEqual(self.rb.intercepts("b1")[0]["kind"], "hoster")
        self.assertIn("保存先を選択してください", self.rb.frame("b1", 0, wait=0)["blocked"])
        self.assertEqual(self.rb._auto_inflight, set())

    def test_unexpected_exception_text_is_not_leaked(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        self.outcome = OSError("secret path /share/x")
        self.paused()
        self.assertNotIn("secret", self.rb.frame("b1", 0, wait=0)["blocked"])

    def test_downloads_and_popups_never_auto_send(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        self.ws.emit("Browser.downloadWillBegin", {"frameId": "TARGET00001", "url": "https://files.example/a.zip"})
        self.barrier()
        self.assertEqual(self.sent, [])

    def test_a_rule_for_another_host_does_not_apply(self):
        self.rules.rules["elsewhere.org"] = rule("elsewhere.org", auto_send=True)
        self.paused()
        self.assertEqual(self.sent, [])

    def test_in_flight_send_is_not_started_twice(self):
        self.rules.rules["blog.example.com"] = rule("blog.example.com", auto_send=True)
        queued = []
        self.rb._spawn = queued.append
        self.paused()
        self.paused(self.HOSTER + "/other-name.html")
        self.assertEqual(len(queued), 1)
        queued[0]()
        self.assertEqual(self.sent, [self.HOSTER])


class PopupPolicyTests(PopupTests):
    def setUp(self):
        super().setUp()
        self.rules = FakeRules()
        self.rb.rules = self.rules

    def wait_events(self):
        for _ in range(200):
            if self.rb.intercepts("b1"):
                break
            _time.sleep(0.01)
        return self.rb.intercepts("b1")

    def test_ask_is_the_default_and_a_rule_for_ask_is_the_same(self):
        self.rules.rules["example.com"] = rule("example.com", popup="ask")
        self.popup("https://shop.example/item")
        self.assertEqual(self.rb.intercepts("b1")[0]["kind"], "popup")

    def test_block_closes_silently_and_counts(self):
        self.rules.rules["example.com"] = rule("example.com", popup="block")
        self.popup("https://shop.example/item")
        self.popup("https://shop.example/other", target="POPUP000002")
        self.assertEqual(self.closed(), ["POPUP000001", "POPUP000002"])
        self.assertEqual(self.rb.intercepts("b1"), [])
        frame = self.rb.frame("b1", 0, wait=0)
        self.assertEqual(frame["popups"], 2)
        self.assertIsNone(frame["blocked"])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)

    def test_block_counts_url_less_and_unsafe_popups_too(self):
        self.rules.rules["example.com"] = rule("example.com", popup="block")
        self.popup("http://insecure.example/", target="POPUP000001")
        self.assertEqual(self.rb.frame("b1", 0, wait=0)["popups"], 1)

    def test_hoster_popups_are_still_intercepted_under_block(self):
        self.rules.rules["example.com"] = rule("example.com", popup="block")
        self.popup(self.HOSTER + "/x.html")
        self.assertEqual(self.rb.intercepts("b1"), [{"id": 1, "kind": "hoster", "url": self.HOSTER}])
        self.assertEqual(self.rb.frame("b1", 0, wait=0)["popups"], 0)

    def test_allow_with_room_asks_the_viewer_to_open_a_new_tab(self):
        self.rules.rules["example.com"] = rule("example.com", popup="allow")
        self.popup("https://shop.example/item")
        self.assertEqual(self.closed(), ["POPUP000001"])
        self.assertEqual(self.wait_events(), [
            {"id": 1, "kind": "popup_opened", "url": "https://shop.example/item", "where": "new"}])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)       # the opener did not move
        # the viewer can still open it in the current tab when a new one is not possible
        self.rb.open_intercept("b1", 1)
        self.assertEqual(self.ws.calls("Page.navigate")[-1]["params"], {"url": "https://shop.example/item"})

    def test_allow_at_the_tab_limit_uses_the_current_tab(self):
        self.rules.rules["example.com"] = rule("example.com", popup="allow")
        self.open("https://example.com/2")
        self.open("https://example.com/3")
        self.popup("https://shop.example/item")
        events = self.wait_events()
        self.assertEqual([(e["kind"], e["where"]) for e in events], [("popup_opened", "current")])
        nav = self.ws.calls("Page.navigate")[-1]
        self.assertEqual((nav["params"], nav["sessionId"]), ({"url": "https://shop.example/item"}, "SESSION1"))

    def test_allow_under_memory_pressure_uses_the_current_tab(self):
        self.rules.rules["example.com"] = rule("example.com", popup="allow")
        self.set_mem(100 * 1024)
        self.popup("https://shop.example/item")
        self.assertEqual(self.wait_events()[0]["where"], "current")

    def test_allow_with_unsafe_url_is_never_opened(self):
        self.rules.rules["example.com"] = rule("example.com", popup="allow")
        self.popup("http://insecure.example/")
        _time.sleep(0.1)
        self.assertEqual(self.rb.intercepts("b1"), [])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 1)


class ProbeTests(BrowserBase):
    HOSTER = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
    START = "https://blog.example.org/go?id=7"

    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.ws.eval_map = [("elementFromPoint", self.START)]
        self.old = (rb.PROBE_SETTLE, rb.PROBE_TIMEOUT)
        rb.PROBE_SETTLE = 0
        self.addCleanup(lambda: (setattr(rb, "PROBE_SETTLE", self.old[0]),
                                 setattr(rb, "PROBE_TIMEOUT", self.old[1])))

    def probe_session(self):
        return "SESSION2"

    def script(self, on_navigate):
        """Run `on_navigate(emit)` when the probe page is told to navigate."""
        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") == "SESSION2":
                on_navigate(lambda method, params=None: self.ws.emit(method, params or {}, "SESSION2"))
        self.ws.hook = hook

    def closes(self):
        return [m["params"]["targetId"] for m in self.ws.calls("Target.closeTarget")]

    def test_redirect_chain_to_a_hoster_is_captured_and_the_target_is_closed(self):
        def navigate(emit):
            emit("Network.requestWillBeSent", {"type": "Document", "request": {"url": self.START}})
            emit("Network.requestWillBeSent", {"type": "Document", "redirectResponse": {"status": 302},
                                               "request": {"url": "https://hop.example.net/r"}})
            emit("Fetch.requestPaused", {"requestId": "rp1", "resourceType": "Document",
                                         "request": {"url": self.HOSTER + "/f.rar.html"}})
        self.script(navigate)
        result = self.rb.probe("b1", 120.4, 300.2)
        self.assertEqual(result["start_url"], self.START)
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertEqual(result["redirects"], 1)
        # the hoster document was aborted inside the probe, not rendered
        fail = self.ws.calls("Fetch.failRequest")[0]
        self.assertEqual((fail["params"]["requestId"], fail["sessionId"]), ("rp1", "SESSION2"))
        self.assertEqual(self.closes(), ["TARGET00002"])
        self.assertEqual(self.rb._probe_targets, {})
        self.assertEqual(self.rb._probe_sessions, {})
        self.assertEqual(self.rb._probes, 0)
        self.assertFalse(self.rb._tabs["b1"].probing)

    def test_probe_target_is_hidden_never_screencast_and_sets_up_hoster_and_ad_rules(self):
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        self.rb.probe("b1", 10, 10)
        create = self.ws.calls("Target.createTarget")[-1]["params"]
        self.assertEqual(create, {"url": "about:blank", "background": True})
        probe_calls = [m for m in self.ws.sent if m.get("sessionId") == "SESSION2"]
        methods = [m["method"] for m in probe_calls]
        self.assertNotIn("Page.startScreencast", methods)
        self.assertNotIn("Target.activateTarget", [m["method"] for m in self.ws.sent[-30:]
                                                   if m.get("params", {}).get("targetId") == "TARGET00002"])
        for needed in ("Page.enable", "Fetch.enable", "Network.enable", "Network.setBlockedURLs", "Page.navigate"):
            self.assertIn(needed, methods)
        fetch = [m for m in probe_calls if m["method"] == "Fetch.enable"][0]
        self.assertEqual(fetch["params"]["patterns"], [dict(p) for p in rb.FETCH_PATTERNS])
        self.assertEqual([m for m in probe_calls if m["method"] == "Page.navigate"][0]["params"],
                         {"url": self.START})
        # coordinates are CSS pixels of the tab and are formatted as numbers only
        expression = [m for m in self.ws.calls("Runtime.evaluate") if "elementFromPoint" in m["params"]["expression"]][0]
        self.assertEqual(expression["sessionId"], "SESSION1")
        self.assertTrue(expression["params"]["expression"].endswith("(10.0,10.0,28)"))
        self.assertIn("closest('a[href]')", expression["params"]["expression"])

    def test_selftest_probe_answers_the_virtual_site_from_ram(self):
        # v0.1.66: a probe started by the self-test tab pauses the virtual site's requests too (else the hidden page
        # asked the real network for selftest.example.com and found nothing on a NAS)
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        tab = self.rb._tabs["b1"]
        tab.fixture = lambda url: (200, "text/html", b"<p>x</p>")
        self.rb._run_probe(tab, rb.SELFTEST_ORIGIN + "/go/1")
        fetch = [m for m in self.ws.sent if m.get("sessionId") == "SESSION2" and m["method"] == "Fetch.enable"][0]
        self.assertIn({"urlPattern": rb.SELFTEST_ORIGIN + "/*"}, fetch["params"]["patterns"])
        for p in rb.FETCH_PATTERNS:
            self.assertIn(dict(p), fetch["params"]["patterns"])

    def test_ad_blocking_follows_the_origin_tab_site_exception(self):
        rules = FakeRules()
        rules.rules["example.com"] = rule("example.com", block_ads=False)
        self.rb.rules = rules
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        self.rb.probe("b1", 10, 10)
        probe_methods = [m["method"] for m in self.ws.sent if m.get("sessionId") == "SESSION2"]
        self.assertNotIn("Network.setBlockedURLs", probe_methods)

    def test_page_without_hoster_redirect_reports_anchors_and_final_url(self):
        self.ws.eval_map += [("location.href", "https://blog.example.org/final"),
                             ("querySelectorAll", ["https://x.example/a", self.HOSTER + "/a.html",
                                                   "https://rapidgator.net/file/ffffffffffffffffffffffffffffffff/b.html",
                                                   self.HOSTER + "/dupe.html"])]

        def navigate(emit):
            emit("Page.frameStartedLoading", {"frameId": "F1"})
            emit("Network.requestWillBeSent", {"type": "Document", "request": {"url": self.START}})
            emit("Page.frameStoppedLoading", {"frameId": "F1"})
            emit("Page.loadEventFired", {"timestamp": 1})
        self.script(navigate)
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [self.HOSTER, "https://rapidgator.net/file/ffffffffffffffffffffffffffffffff"])
        self.assertEqual(result["final_url"], "https://blog.example.org/final")
        self.assertEqual(result["redirects"], 0)
        self.assertEqual(self.closes(), ["TARGET00002"])

    def test_no_links_returns_the_final_url_for_opening_in_the_tab(self):
        self.ws.eval_map += [("location.href", "https://blog.example.org/landing")]

        def navigate(emit):
            emit("Page.frameStartedLoading", {"frameId": "F1"})
            emit("Page.frameStoppedLoading", {"frameId": "F1"})
            emit("Page.loadEventFired", {})
        self.script(navigate)
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual((result["links"], result["final_url"]), ([], "https://blog.example.org/landing"))

    def test_results_are_capped_at_50_and_deduplicated(self):
        hrefs = ["https://rapidgator.net/file/%032x/a" % i for i in range(1, 80)]
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", hrefs + hrefs)]
        self.script(lambda emit: (emit("Page.frameStartedLoading", {"frameId": "F1"}),
                                  emit("Page.frameStoppedLoading", {"frameId": "F1"}),
                                  emit("Page.loadEventFired", {})))
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(len(result["links"]), 50)
        self.assertEqual(len(set(result["links"])), 50)

    def test_timeout_still_reads_the_page_and_closes_the_target(self):
        rb.PROBE_TIMEOUT = 3
        self.rb._clock = TickClock()
        self.ws.eval_map += [("location.href", "https://blog.example.org/slow"), ("querySelectorAll", [])]
        started = _time.monotonic()
        result = self.rb.probe("b1", 5, 5)
        self.assertLess(_time.monotonic() - started, 10)
        self.assertEqual(result["links"], [])
        self.assertEqual(result["final_url"], "https://blog.example.org/slow")
        self.assertEqual(self.closes(), ["TARGET00002"])
        self.assertEqual(self.rb._probes, 0)

    def test_timeout_when_the_page_no_longer_answers_still_closes(self):
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.ws.fail["Runtime.evaluate"] = "target crashed"
        # the first evaluate (href) is allowed through, later ones fail
        calls = []

        def hook(message):
            if message["method"] == "Runtime.evaluate":
                calls.append(1)
                if len(calls) == 1:
                    self.ws.fail.pop("Runtime.evaluate", None)
                else:
                    self.ws.fail["Runtime.evaluate"] = "target crashed"
        self.ws.hook = hook
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [])
        self.assertEqual(result["final_url"], self.START)
        self.assertEqual(self.closes(), ["TARGET00002"])

    def test_navigation_error_raises_and_still_closes_the_target(self):
        self.ws.fail["Page.navigate"] = "net::ERR"
        # the tab's own initial navigation already happened; only the probe navigates now
        with self.assertRaises(rb.BrowserError):
            self.rb.probe("b1", 5, 5)
        self.assertEqual(self.closes(), ["TARGET00002"])
        self.assertEqual((self.rb._probes, self.rb._tabs["b1"].probing), (0, False))
        self.assertEqual(self.rb._probe_sessions, {})

    def test_attach_failure_closes_the_created_target(self):
        self.ws.fail["Target.attachToTarget"] = "gone"
        with self.assertRaises(rb.BrowserError):
            self.rb.probe("b1", 5, 5)
        self.assertEqual(self.closes(), ["TARGET00002"])
        self.assertEqual(self.rb._probe_targets, {})

    def test_close_failure_does_not_mask_the_result(self):
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        self.ws.fail["Target.closeTarget"] = "already closed"
        self.assertEqual(self.rb.probe("b1", 5, 5)["links"], [self.HOSTER])

    def test_direct_hoster_links_need_no_hidden_page(self):
        self.ws.eval_map = [("elementFromPoint", self.HOSTER + "/file.rar.html")]
        before = len(self.ws.calls("Target.createTarget"))
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before)

    def test_unsafe_or_missing_links_are_refused_without_creating_a_target(self):
        before = len(self.ws.calls("Target.createTarget"))
        for value in (None, "", "http://insecure.example/", "https://192.168.0.2/x", "javascript:alert(1)",
                      "[object SVGAnimatedString]", 5, ["x"]):
            with self.subTest(value=value):
                self.ws.eval_map = [("elementFromPoint", value)]
                with self.assertRaises(rb.BrowserError):
                    self.rb.probe("b1", 5, 5)
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before)
        self.assertEqual(self.rb._probes, 0)

    def test_hub_origin_check_can_refuse_the_start_url(self):
        def refuse(url):
            raise ValueError("hub")
        with self.assertRaises(rb.BrowserError):
            self.rb.probe("b1", 5, 5, url_check=refuse)
        self.assertEqual(len(self.ws.calls("Target.createTarget")), 1)     # only the tab

    def test_coordinates_and_tab_are_validated(self):
        for x, y in ((-1, 0), (0, -1), ("1", 1), (True, 1), (float("nan"), 1), (1e9, 1), (None, 1)):
            with self.subTest(x=x, y=y), self.assertRaises(rb.BrowserError):
                self.rb.probe("b1", x, y)
        with self.assertRaises(rb.BrowserError):
            self.rb.probe("../x", 1, 1)
        with self.assertRaises(rb.TabGone):
            self.rb.probe("b9", 1, 1)

    # ---- v0.1.27: parallel probes ------------------------------------------
    def hold_navigations(self):
        """Hold every probe at Page.navigate until released; returns (count_event_fn, release)."""
        release = threading.Event()
        seen = []
        lock = threading.Lock()

        def hook(message):
            if message["method"] == "Page.navigate" and str(message.get("sessionId", "")) != "SESSION1":
                with lock:
                    seen.append(message["sessionId"])
                release.wait(10)
        self.ws.hook = hook
        return seen, release

    def wait_for(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def run_probes(self, count, tab="b1"):
        out, errors = [], []

        def work(i):
            try:
                out.append(self.rb.probe(tab, 5 + i, 5))
            except rb.BrowserError as error:
                errors.append(error)
        threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(count)]
        for t in threads:
            t.start()
        return threads, out, errors

    def test_three_probes_run_in_parallel_with_distinct_targets_and_the_fourth_waits(self):
        seen, release = self.hold_navigations()
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        threads, out, errors = self.run_probes(4)
        self.assertTrue(self.wait_for(lambda: len(seen) == 3))
        time.sleep(0.4)
        self.assertEqual(len(seen), 3)                 # the 4th (same tab) is queued
        self.assertEqual(len(set(seen)), 3)            # three distinct probe sessions
        tab = self.rb._tabs["b1"]
        self.assertEqual((tab.probing, tab.probe_waiting, self.rb._probes), (3, 1, 3))
        self.assertEqual(len(self.rb._probe_targets), 3)
        release.set()
        for t in threads:
            t.join(10)
        self.assertEqual((len(out), errors), (4, []))
        self.assertEqual(len(seen), 4)                 # the queued one ran afterwards
        created = [m for m in self.ws.calls("Target.createTarget")][1:]
        ids = ["TARGET%05d" % n for n in range(2, 6)]
        self.assertEqual(len(created), 4)
        self.assertEqual(sorted(i for i in self.closes() if i in ids), ids)
        self.assertEqual((self.rb._probes, tab.probing, tab.probe_waiting), (0, 0, 0))
        self.assertEqual((self.rb._probe_targets, self.rb._probe_sessions, self.rb._probe_queue),
                         ({}, {}, []))

    def test_concurrent_probes_keep_their_own_redirect_capture_and_popups(self):
        # each probe's hoster URL and popup is attributed by its own target/session
        hosters = {}

        def hook(message):
            if message["method"] != "Page.navigate" or message.get("sessionId") == "SESSION1":
                return
            session = message["sessionId"]
            n = int(session[len("SESSION"):])
            hoster = "https://rapidgator.net/file/%032x" % n
            hosters[session] = hoster
            self.ws.emit("Fetch.requestPaused", {"requestId": "rp%d" % n, "resourceType": "Document",
                                                 "request": {"url": hoster}}, session)
        self.ws.hook = hook
        threads, out, errors = self.run_probes(3)
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        got = sorted(r["links"][0] for r in out)
        self.assertEqual(got, sorted(hosters.values()))
        self.assertEqual(len(set(got)), 3)
        self.assertEqual(self.rb._probe_targets, {})

    def test_overall_cap_is_four_across_tabs_and_waiters_run_later(self):
        for n in range(2):
            self.open("https://example.com/%d" % n)       # b2, b3
        seen, release = self.hold_navigations()
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        t1, o1, e1 = self.run_probes(3, "b1")
        t2, o2, e2 = self.run_probes(2, "b2")
        self.assertTrue(self.wait_for(lambda: len(seen) == 4))
        # the fifth probe queues behind the overall cap; which tab's probe waits depends on
        # thread start order, so the waiters are counted across tabs
        self.assertTrue(self.wait_for(
            lambda: sum(t.probe_waiting for t in list(self.rb._tabs.values())) == 1))
        self.assertEqual((len(seen), self.rb._probes), (4, 4))
        release.set()
        for t in t1 + t2:
            t.join(10)
        self.assertEqual((len(o1), len(o2), e1, e2), (3, 2, [], []))
        self.assertEqual(self.rb._probes, 0)

    def test_fifo_order_of_the_waiting_probes(self):
        seen, release = self.hold_navigations()
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        order = []
        threads = []

        def work(i):
            order.append(self.rb.probe("b1", i, i)["start_url"])
        for i in range(3):
            t = threading.Thread(target=work, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        self.assertTrue(self.wait_for(lambda: len(seen) == 3))
        waiter_tickets = []
        for i in (10, 11):
            t = threading.Thread(target=work, args=(i,), daemon=True)
            t.start()
            threads.append(t)
            self.assertTrue(self.wait_for(lambda: len(self.rb._probe_queue) == len(waiter_tickets) + 1))
            waiter_tickets.append(list(self.rb._probe_queue)[-1])
        self.assertEqual(self.rb._probe_queue, waiter_tickets)
        release.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(order), 5)
        self.assertEqual(self.rb._probe_queue, [])

    def test_queue_overflow_is_refused_with_the_429_error_class(self):
        seen, release = self.hold_navigations()
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        threads, out, errors = self.run_probes(3 + rb.PROBE_QUEUE_PER_TAB)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].probe_waiting == rb.PROBE_QUEUE_PER_TAB))
        with self.assertRaises(rb.ProbeQueueFull) as error:
            self.rb.probe("b1", 9, 9)
        self.assertIn("確認待ちが多すぎます", str(error.exception))
        self.assertEqual(self.rb._tabs["b1"].probe_waiting, rb.PROBE_QUEUE_PER_TAB)
        release.set()
        for t in threads:
            t.join(20)
        self.assertEqual((len(out), errors), (3 + rb.PROBE_QUEUE_PER_TAB, []))
        self.assertEqual((self.rb._probes, self.rb._tabs["b1"].probe_waiting), (0, 0))

    def test_memory_guard_waits_then_runs_when_memory_returns(self):
        self.set_mem(100 * 1024)
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        before = len(self.ws.calls("Target.createTarget"))
        out = []
        thread = threading.Thread(target=lambda: out.append(self.rb.probe("b1", 5, 5)), daemon=True)
        thread.start()
        time.sleep(0.4)
        self.assertEqual(out, [])                      # still waiting, not failed
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before)
        self.assertEqual(self.rb._probes, 1)
        self.set_mem(8 * 1024 * 1024)
        thread.join(10)
        self.assertEqual(out[0]["start_url"], self.START)
        self.assertEqual(self.rb._probes, 0)

    def test_memory_guard_fails_with_the_message_after_the_wait(self):
        self.set_mem(100 * 1024)                       # < 1 GiB
        old_wait, rb.PROBE_QUEUE_WAIT = rb.PROBE_QUEUE_WAIT, 2.5
        self.addCleanup(lambda: setattr(rb, "PROBE_QUEUE_WAIT", old_wait))
        self.rb._clock = TickClock()
        before = len(self.ws.calls("Target.createTarget"))
        with self.assertRaises(rb.BrowserError) as error:
            self.rb.probe("b1", 5, 5)
        self.assertIn("メモリ", str(error.exception))
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before)
        self.assertEqual((self.rb._probes, self.rb._tabs["b1"].probing), (0, 0))

    def test_probe_guard_thresholds_are_one_gib_and_eighty_five_percent(self):
        self.assertEqual((rb.PROBE_MIN_AVAILABLE_KIB, rb.PROBE_MEM_LIMIT_PERCENT), (1024 * 1024, 85.0))
        self.set_mem(1536 * 1024)                      # between 1 GiB and the 2 GiB tab rule
        self.assertIsNone(self.rb.probe_memory_reason())
        self.assertIsNotNone(self.rb.memory_reason())
        self.set_mem(900 * 1024)
        self.assertIsNotNone(self.rb.probe_memory_reason())

    def test_probe_popups_are_collected_and_closed(self):
        def navigate(emit):
            self.ws.emit("Target.targetCreated", {"targetInfo": {
                "targetId": "POPUPPROBE01", "type": "page", "openerId": "TARGET00002", "url": ""}})
            self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
                "targetId": "POPUPPROBE01", "type": "page", "openerId": "TARGET00002",
                "url": self.HOSTER + "/x.html"}})
        self.script(navigate)
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertIn("POPUPPROBE01", self.closes())
        self.assertIn("TARGET00002", self.closes())
        # nothing was recorded on the origin tab
        self.assertEqual(self.rb.intercepts("b1"), [])

    def test_probe_popups_never_become_events_or_tabs_even_with_popup_allow(self):
        class Rules:
            def site_rule_for(self, host):
                return {"popup": "allow", "auto_send": True}
        self.rb.rules = Rules()
        def navigate(emit):
            self.ws.emit("Target.targetCreated", {"targetInfo": {
                "targetId": "POPUPPROBE04", "type": "page", "openerId": "TARGET00002", "url": ""}})
            self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
                "targetId": "POPUPPROBE04", "type": "page", "openerId": "TARGET00002",
                "url": "https://ads.example/pop"}})
            # a popup of the popup (transitive)
            self.ws.emit("Target.targetCreated", {"targetInfo": {
                "targetId": "POPUPPROBE05", "type": "page", "openerId": "POPUPPROBE04",
                "url": self.HOSTER + "/z.html"}})
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", [])]
        self.script(navigate)
        tabs_before = set(self.rb._tabs)
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [self.HOSTER])
        closes = self.closes()
        for target in ("POPUPPROBE04", "POPUPPROBE05", "TARGET00002"):
            self.assertIn(target, closes)
        self.assertEqual(set(self.rb._tabs), tabs_before)
        self.assertEqual(self.rb.intercepts("b1"), [])
        self.assertEqual(len(self.ws.calls("Page.navigate")), 2)
        self.assertEqual(self.rb._probe_popups, {})

    def test_leftover_probe_targets_are_closed_after_the_probe(self):
        base = self.ws.send_text
        def listing(text):
            message = json.loads(text)
            if message["method"] == "Target.getTargets" and self.ws.calls("Target.closeTarget"):
                self.ws.sent.append(message)
                self.ws.q.put(json.dumps({"id": message["id"], "result": {"targetInfos": [
                    {"targetId": "ANCHOR00001", "type": "page"},
                    {"targetId": "TARGET00002", "type": "page"},
                    {"targetId": "STRAY0000001", "type": "page", "openerId": "TARGET00002"},
                    {"targetId": "STRAY0000002", "type": "page", "openerId": "STRAY0000001"}]}}))
                return
            base(text)
        self.ws.send_text = listing
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        self.rb.probe("b1", 5, 5)
        closes = self.closes()
        for target in ("TARGET00002", "STRAY0000001", "STRAY0000002"):
            self.assertIn(target, closes)
        self.assertNotIn("ANCHOR00001", closes)
        self.assertGreaterEqual(closes.count("TARGET00002"), 2)

    def test_hoster_anchors_of_a_popup_are_collected(self):
        self.script(lambda emit: self.ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "POPUPPROBE06", "type": "page", "openerId": "TARGET00002",
            "url": "https://ads.example/p"}}))
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.ws.eval_map += [("querySelectorAll", [self.HOSTER + "/a.rar.html"]),
                             ("location.href", self.START)]
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertIn("POPUPPROBE06", self.closes())

    def test_unrelated_popups_of_the_probe_are_closed_never_followed(self):
        def navigate(emit):
            self.ws.emit("Target.targetCreated", {"targetInfo": {
                "targetId": "POPUPPROBE02", "type": "page", "openerId": "TARGET00002",
                "url": "https://ads.example/pop"}})
            emit("Page.frameStartedLoading", {"frameId": "F1"})
            emit("Page.frameStoppedLoading", {"frameId": "F1"})
            emit("Page.loadEventFired", {})
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", [])]
        self.script(navigate)
        result = self.rb.probe("b1", 5, 5)
        self.assertEqual(result["links"], [])
        self.assertIn("POPUPPROBE02", self.closes())
        self.assertEqual(len(self.ws.calls("Page.navigate")), 2)      # tab + probe page only

    def test_popup_still_open_when_the_probe_ends_is_closed(self):
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.script(lambda emit: self.ws.emit("Target.targetCreated", {"targetInfo": {
            "targetId": "POPUPPROBE03", "type": "page", "openerId": "TARGET00002", "url": ""}}))
        self.rb.probe("b1", 5, 5)
        self.assertIn("POPUPPROBE03", self.closes())
        self.assertEqual(self.rb._probe_popups, {})

    def test_dialogs_on_the_probe_page_are_dismissed(self):
        self.script(lambda emit: emit("Page.javascriptDialogOpening", {"message": "x"}))
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", [])]
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.rb.probe("b1", 5, 5)
        handled = self.ws.calls("Page.handleJavaScriptDialog")
        self.assertEqual([(m["params"], m["sessionId"]) for m in handled], [({"accept": False}, "SESSION2")])

    def test_downloads_from_the_probe_never_reach_a_tab(self):
        self.script(lambda emit: self.ws.emit("Browser.downloadWillBegin", {
            "frameId": "TARGET00002", "url": "https://files.example/x.zip"}))
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", [])]
        self.rb.probe("b1", 5, 5)
        self.assertEqual(self.rb.intercepts("b1"), [])

    def test_non_document_hoster_requests_pass_in_the_probe(self):
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "img1", "resourceType": "Image", "request": {"url": self.HOSTER + "/x.png"}}))
        rb.PROBE_TIMEOUT = 2
        self.rb._clock = TickClock()
        self.ws.eval_map += [("location.href", self.START), ("querySelectorAll", [])]
        self.assertEqual(self.rb.probe("b1", 5, 5)["links"], [])
        self.assertEqual([m["params"]["requestId"] for m in self.ws.calls("Fetch.continueRequest")], ["img1"])

    def test_probe_only_sends_fixed_cdp_methods(self):
        self.script(lambda emit: emit("Fetch.requestPaused", {
            "requestId": "rp1", "resourceType": "Document", "request": {"url": self.HOSTER}}))
        self.rb.probe("b1", 5, 5)
        allowed = {"Target.setDiscoverTargets", "Browser.setDownloadBehavior", "Target.getTargets",
                   "Target.createTarget", "Target.attachToTarget", "Page.enable",
                   "Emulation.setFocusEmulationEnabled", "Page.navigate", "Runtime.evaluate",
                   "Target.closeTarget", "Fetch.enable", "Fetch.failRequest", "Fetch.continueRequest",
                   "Network.enable", "Network.setBlockedURLs", "Emulation.setEmulatedMedia"}
        self.assertLessEqual(set(self.ws.methods()), allowed)

    def test_stop_clears_probe_state(self):
        self.script(lambda emit: None)
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        self.rb.probe("b1", 5, 5)
        self.rb.stop()
        self.assertEqual((self.rb._probe_targets, self.rb._probe_sessions, self.rb._probe_popups), ({}, {}, {}))



class SnapshotTests(BrowserBase):
    """v0.1.28: fast full-page display (snapshot + lazily rendered tiles)."""

    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.rb.set_viewport("b1", 390, 700, 2.0, "standard")      # scale 1.5, quality 55
        self.old = (rb.TAP_NAV_WAIT, rb.TAP_SETTLE_MAX)
        rb.TAP_NAV_WAIT = 0
        self.addCleanup(lambda: (setattr(rb, "TAP_NAV_WAIT", self.old[0]),
                                 setattr(rb, "TAP_SETTLE_MAX", self.old[1])))

    def shots(self):
        return [m["params"] for m in self.ws.calls("Page.captureScreenshot")]

    def snap_eval(self, links=None, inputs=None):
        self.ws.eval_map = [("__qdlMut", {"links": links or [], "inputs": inputs or []})] + \
            [e for e in self.ws.eval_map if "__qdlMut" not in e[0]]

    # --- tile math
    def test_tile_clip_math_with_a_partial_last_tile(self):
        self.ws.layout = {"cssContentSize": {"width": 390, "height": 4000.2}}
        meta = self.rb.snapshot("b1")
        self.assertEqual((meta["width"], meta["height"], meta["tile_height"], meta["tiles"]),
                         (390, 4001, 1600, 3))
        self.assertEqual(meta["scale"], 1.5)
        for index in range(3):
            self.rb.tile("b1", meta["snap_id"], index)
        shots = self.shots()
        self.assertEqual([(s["clip"]["y"], s["clip"]["height"]) for s in shots],
                         [(0, 1600), (1600, 1600), (3200, 801)])
        for shot in shots:
            self.assertEqual((shot["clip"]["x"], shot["clip"]["width"]), (0, 390))
            self.assertEqual(shot["format"], "jpeg")
            self.assertEqual(shot["quality"], 55)
            self.assertIs(shot["captureBeyondViewport"], True)
            self.assertIs(shot["fromSurface"], True)
            # the emulated viewport already carries the device scale factor
            self.assertEqual(shot["clip"]["scale"], rb.SNAP_CLIP_SCALE)

    def test_exact_multiple_and_tiny_pages(self):
        self.ws.layout = {"cssContentSize": {"width": 390, "height": 3200}}
        self.assertEqual(self.rb.snapshot("b1")["tiles"], 2)
        self.ws.layout = {"cssContentSize": {"width": 390, "height": 0}}
        meta = self.rb.snapshot("b1", force=True)
        self.assertEqual((meta["height"], meta["tiles"]), (1, 1))

    def test_height_is_capped_at_30000_css_pixels(self):
        self.ws.layout = {"cssContentSize": {"width": 390, "height": 123456}}
        meta = self.rb.snapshot("b1")
        self.assertEqual((meta["height"], meta["tiles"]), (30000, 19))
        self.rb.tile("b1", meta["snap_id"], 18)
        self.assertEqual(self.shots()[-1]["clip"], {"x": 0, "y": 28800, "width": 390, "height": 1200,
                                                    "scale": rb.SNAP_CLIP_SCALE})
        with self.assertRaises(rb.BrowserError):
            self.rb.tile("b1", meta["snap_id"], 19)

    def test_quality_mode_picks_scale_and_jpeg_quality(self):
        self.rb.set_viewport("b1", 390, 700, 3.0, "high")
        meta = self.rb.snapshot("b1")
        self.assertEqual((meta["scale"], meta["quality"]), (2.0, "high"))
        self.rb.tile("b1", meta["snap_id"], 0)
        self.assertEqual(self.shots()[-1]["quality"], 70)

    def test_bad_layout_is_refused(self):
        for layout in ({}, {"cssContentSize": {"height": "x"}}, {"cssContentSize": {"height": -5}},
                       {"cssContentSize": {"height": True}}):
            with self.subTest(layout=layout):
                self.ws.layout = layout
                with self.assertRaises(rb.BrowserError):
                    self.rb.snapshot("b1", force=True)

    # --- lazy capture + LRU
    def test_tiles_are_rendered_on_demand_only_and_cached(self):
        meta = self.rb.snapshot("b1")
        self.assertEqual(self.shots(), [])
        first = self.rb.tile("b1", meta["snap_id"], 1)
        self.assertEqual(len(self.shots()), 1)
        self.assertEqual(self.rb.tile("b1", meta["snap_id"], 1), first)
        self.assertEqual(len(self.shots()), 1)            # served from RAM
        self.rb.tile("b1", meta["snap_id"], 0)
        self.assertEqual(len(self.shots()), 2)

    def test_tile_cache_is_capped_by_count_and_bytes_with_lru_order(self):
        self.ws.layout = {"cssContentSize": {"width": 390, "height": 30000}}
        meta = self.rb.snapshot("b1")
        tab = self.rb._tabs["b1"]
        for index in range(min(rb.SNAP_MAX_TILES + 5, meta["tiles"])):
            self.rb.tile("b1", meta["snap_id"], index)
        self.assertLessEqual(len(tab.tiles), rb.SNAP_MAX_TILES)
        old = (rb.SNAP_MAX_TILES, rb.SNAP_MAX_BYTES)
        self.addCleanup(lambda: (setattr(rb, "SNAP_MAX_TILES", old[0]),
                                 setattr(rb, "SNAP_MAX_BYTES", old[1])))
        rb.SNAP_MAX_TILES, rb.SNAP_MAX_BYTES = 3, 10 ** 9
        tab.tiles.clear()
        tab.tile_bytes = 0
        sid = meta["snap_id"]
        for index in (0, 1, 2):
            self.rb.tile("b1", sid, index)
        self.rb.tile("b1", sid, 0)                         # 0 becomes the most recent
        self.rb.tile("b1", sid, 3)                         # evicts 1 (least recent)
        self.assertEqual([k[1] for k in tab.tiles], [2, 0, 3])
        shots_before = len(self.shots())
        self.rb.tile("b1", sid, 0)
        self.assertEqual(len(self.shots()), shots_before)
        self.rb.tile("b1", sid, 1)                         # evicted: rendered again
        self.assertEqual(len(self.shots()), shots_before + 1)
        # byte cap
        rb.SNAP_MAX_TILES, rb.SNAP_MAX_BYTES = 40, 250
        tab.tiles.clear()
        tab.tile_bytes = 0
        for index in range(4):
            self.rb.tile("b1", sid, index)
        self.assertEqual(len(tab.tiles), 2)                # 2 x 100 bytes <= 250
        self.assertLessEqual(tab.tile_bytes, 250)

    def test_old_snapshot_is_kept_until_the_new_one_has_a_tile(self):
        first = self.rb.snapshot("b1")
        self.rb.tile("b1", first["snap_id"], 0)
        second = self.rb.snapshot("b1", force=True)
        tab = self.rb._tabs["b1"]
        self.assertEqual(list(tab.snaps), [first["snap_id"], second["snap_id"]])
        self.assertEqual(self.rb.tile("b1", first["snap_id"], 0)[:2], b"0:")      # still served
        self.rb.tile("b1", second["snap_id"], 0)
        self.assertEqual(list(tab.snaps), [second["snap_id"]])
        self.assertEqual({k[0] for k in tab.tiles}, {second["snap_id"]})
        with self.assertRaises(rb.SnapshotGone):
            self.rb.tile("b1", first["snap_id"], 0)
        self.assertIsInstance(rb.SnapshotGone("x"), rb.TabGone)                     # HTTP 404

    def test_at_most_two_snapshots_are_kept(self):
        ids = [self.rb.snapshot("b1", force=True)["snap_id"] for _ in range(4)]
        self.assertEqual(list(self.rb._tabs["b1"].snaps), ids[-2:])

    def test_release_close_viewport_change_and_stop_free_every_tile(self):
        meta = self.rb.snapshot("b1")
        self.rb.tile("b1", meta["snap_id"], 0)
        tab = self.rb._tabs["b1"]
        self.rb.snapshot_release("b1")
        self.assertEqual((tab.snaps, tab.tiles, tab.tile_bytes), ({}, {}, 0))
        meta = self.rb.snapshot("b1")
        self.rb.tile("b1", meta["snap_id"], 0)
        self.rb.set_viewport("b1", 400, 700, 2.0)
        self.assertEqual((tab.snaps, tab.tiles), ({}, {}))
        meta = self.rb.snapshot("b1")
        self.rb.tile("b1", meta["snap_id"], 0)
        self.rb.close_tab("b1")
        self.assertEqual((tab.snaps, tab.tiles, tab.tile_bytes), ({}, {}, 0))

    def test_tile_arguments_are_validated(self):
        meta = self.rb.snapshot("b1")
        for snap, index in (("../x", 0), ("s", 0), (None, 0), (meta["snap_id"], -1),
                            (meta["snap_id"], True), (meta["snap_id"], "0"), (meta["snap_id"], 99)):
            with self.subTest(snap=snap, index=index), self.assertRaises(rb.BrowserError):
                self.rb.tile("b1", snap, index)
        with self.assertRaises(rb.SnapshotGone):
            self.rb.tile("b1", "s999", 0)
        with self.assertRaises(rb.TabGone):
            self.rb.tile("b9", meta["snap_id"], 0)

    def test_bad_screenshot_data_is_refused(self):
        meta = self.rb.snapshot("b1")
        for data in ("", "not base64!", None, 5):
            self.ws.hook = None
            original = self.ws.send_text

            def fake(text, original=original, data=data):
                message = json.loads(text)
                if message["method"] == "Page.captureScreenshot":
                    self.ws.sent.append(message)
                    self.ws.q.put(json.dumps({"id": message["id"], "result": {"data": data}}))
                else:
                    original(text)
            with self.subTest(data=data):
                self.ws.send_text = fake
                with self.assertRaises(rb.BrowserError):
                    self.rb.tile("b1", meta["snap_id"], 0)
                self.ws.send_text = original
        self.assertEqual(self.rb._tabs["b1"].tiles, {})

    def test_memory_guard_blocks_a_snapshot(self):
        self.set_mem(100 * 1024)
        with self.assertRaises(rb.BrowserError):
            self.rb.snapshot("b1")
        self.assertEqual(self.ws.calls("Page.getLayoutMetrics"), [])

    # --- captures never overlap per tab
    def test_captures_of_one_tab_never_run_concurrently(self):
        meta = self.rb.snapshot("b1")
        active = []
        peak = [0]
        gate = threading.Lock()

        def hook(message):
            if message["method"] == "Page.captureScreenshot":
                with gate:
                    active.append(1)
                    peak[0] = max(peak[0], len(active))
                time.sleep(0.05)
                with gate:
                    active.pop()
        self.ws.hook = hook
        threads = [threading.Thread(target=self.rb.tile, args=("b1", meta["snap_id"], i))
                   for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(self.shots()), 3)

    # --- metadata
    def test_metadata_links_and_inputs_are_filtered_and_capped(self):
        good = {"x": 10, "y": 20, "w": 100, "h": 18, "href": "https://example.org/a"}
        self.snap_eval(links=[
            good,
            {"x": 1, "y": 1, "w": 5, "h": 5, "href": "http://plain.example/x"},
            {"x": 1, "y": 1, "w": 5, "h": 5, "href": "javascript:alert(1)"},
            {"x": 1, "y": 1, "w": 5, "h": 5, "href": "data:text/html,x"},
            {"x": 1, "y": 1, "w": 5, "h": 5, "href": "ftp://x/y"},
            {"x": 1, "y": 1, "w": 5, "h": 5, "href": "https://" + "a" * 2100},
            {"x": 1, "y": 1, "w": 5, "h": 5},
            {"x": "1", "y": 1, "w": 5, "h": 5, "href": "https://example.org/s"},
            {"x": float("nan"), "y": 1, "w": 5, "h": 5, "href": "https://example.org/n"},
            {"x": 1, "y": 1, "w": 0, "h": 5, "href": "https://example.org/z"},
            {"x": 1, "y": 99999, "w": 5, "h": 5, "href": "https://example.org/far"},
            {"x": 400, "y": 1, "w": 5, "h": 5, "href": "https://example.org/right"},
            {"x": 1.6, "y": 2.4, "w": 7.5, "h": 9, "href": "https://example.org/round"},
            "str", 5, None],
            inputs=[{"x": 5, "y": 6, "w": 200, "h": 30, "href": "https://leak.example/"},
                    {"x": 5, "y": 6, "w": 0, "h": 30}])
        meta = self.rb.snapshot("b1")
        self.assertEqual([l["href"] for l in meta["links"]],
                         ["https://example.org/a", "http://plain.example/x", "https://example.org/round"])
        self.assertEqual(meta["links"][2], {"x": 2, "y": 2, "w": 8, "h": 9,
                                            "href": "https://example.org/round"})
        # inputs carry geometry only
        self.assertEqual(meta["inputs"], [{"x": 5, "y": 6, "w": 200, "h": 30}])
        self.assertIsInstance(meta["url"], str)
        self.assertIn("title", meta)

    def test_link_and_input_caps(self):
        many = [{"x": 1, "y": i % 100, "w": 5, "h": 5, "href": "https://example.org/%d" % i}
                for i in range(rb.SNAP_LINK_CAP + 50)]
        boxes = [{"x": 1, "y": i % 100, "w": 5, "h": 5} for i in range(rb.SNAP_INPUT_CAP + 50)]
        self.snap_eval(links=many, inputs=boxes)
        meta = self.rb.snapshot("b1")
        self.assertEqual((len(meta["links"]), len(meta["inputs"])),
                         (rb.SNAP_LINK_CAP, rb.SNAP_INPUT_CAP))
        self.assertEqual((rb.SNAP_LINK_CAP, rb.SNAP_INPUT_CAP), (3000, 300))

    def test_collection_script_is_fixed_and_filters_in_the_page_too(self):
        self.rb.snapshot("b1")
        expression = [m for m in self.ws.calls("Runtime.evaluate")
                      if "__qdlMut" in m["params"]["expression"]][0]
        self.assertEqual(expression["sessionId"], "SESSION1")
        self.assertIn("/^https?:/i", expression["params"]["expression"])
        self.assertIn("getBoundingClientRect", expression["params"]["expression"])
        self.assertIn("W=390", expression["params"]["expression"])
        self.assertTrue(expression["params"]["returnByValue"])

    # --- reuse window
    def test_unchanged_snapshot_is_reused_for_two_seconds(self):
        first = self.rb.snapshot("b1")
        self.assertFalse(first["reused"])
        self.clock.now += 1.9
        again = self.rb.snapshot("b1")
        self.assertTrue(again["reused"])
        self.assertEqual(again["snap_id"], first["snap_id"])
        self.assertEqual(len(self.ws.calls("Page.getLayoutMetrics")), 1)
        self.clock.now += 0.2
        later = self.rb.snapshot("b1")
        self.assertNotEqual(later["snap_id"], first["snap_id"])
        self.assertFalse(later["reused"])

    def test_force_input_and_metadata_changes_defeat_the_reuse(self):
        first = self.rb.snapshot("b1")
        self.assertNotEqual(self.rb.snapshot("b1", force=True)["snap_id"], first["snap_id"])
        current = self.rb.snapshot("b1")
        self.assertTrue(current["reused"])
        self.rb.send_input("b1", {"type": "key", "key": "Enter"})
        after_key = self.rb.snapshot("b1")
        self.assertFalse(after_key["reused"])
        self.assertTrue(self.rb.snapshot("b1")["reused"])
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/b", "title": "B"}})
        self.barrier()
        after_nav = self.rb.snapshot("b1")
        self.assertFalse(after_nav["reused"])
        self.assertEqual((after_nav["url"], after_nav["title"]), ("https://example.com/b", "B"))

    def test_scrolling_does_not_defeat_the_reuse(self):
        self.rb.snapshot("b1")
        self.rb.send_input("b1", {"type": "scroll", "x": 1, "y": 1, "dx": 0, "dy": 50})
        self.assertTrue(self.rb.snapshot("b1")["reused"])

    def test_if_changed_asks_the_page_for_dom_changes_after_the_window(self):
        first = self.rb.snapshot("b1")
        self.clock.now += 11
        self.ws.eval_map = [("__qdlDirty===true", False)] + self.ws.eval_map
        quiet = self.rb.snapshot("b1", if_changed=True)
        self.assertTrue(quiet["reused"])
        self.assertEqual(quiet["snap_id"], first["snap_id"])
        self.ws.eval_map = [("__qdlDirty===true", True)] + self.ws.eval_map[1:]
        changed = self.rb.snapshot("b1", if_changed=True)
        self.assertFalse(changed["reused"])
        self.assertNotEqual(changed["snap_id"], first["snap_id"])

    # --- tap-page
    def test_tap_page_scrolls_then_clicks_at_viewport_coordinates(self):
        meta = self.rb.snapshot("b1")
        self.ws.eval_map = [("scrollTo", [0, 600, 390, 700])]
        result = self.rb.tap_page("b1", meta["snap_id"], 120.4, 1000)
        self.assertEqual(result, {"ok": True, "navigated": False, "editable": False,
                                  "snap_needed": True})
        scroll = [m for m in self.ws.calls("Runtime.evaluate") if "scrollTo" in m["params"]["expression"]][0]
        self.assertTrue(scroll["params"]["expression"].endswith("(1000.0)"))
        self.assertIn("behavior:'instant'", scroll["params"]["expression"])
        events = [m["params"] for m in self.ws.calls("Input.dispatchMouseEvent")]
        self.assertEqual([e["type"] for e in events], ["mouseMoved", "mousePressed", "mouseReleased"])
        for event in events:
            self.assertEqual((event["x"], event["y"]), (120.4, 400.0))     # 1000 - scrollY 600
        self.assertEqual(events[1]["button"], "left")

    def test_tap_page_with_horizontal_scroll_and_editable_result(self):
        meta = self.rb.snapshot("b1")
        self.ws.eval_map = [("scrollTo", [10, 0, 390, 700]), ("activeElement", True)]
        result = self.rb.tap_page("b1", meta["snap_id"], 50, 100)
        self.assertTrue(result["editable"])
        self.assertEqual(self.ws.calls("Input.dispatchMouseEvent")[1]["params"]["x"], 40.0)

    def test_tap_page_refuses_bad_input_and_unreachable_positions(self):
        meta = self.rb.snapshot("b1")
        sid = meta["snap_id"]
        self.ws.eval_map = [("scrollTo", [0, 0, 390, 700])]
        for snap, x, y in (("../x", 1, 1), (sid, -1, 1), (sid, 1, True), (sid, "1", 1),
                           (sid, float("nan"), 1), (sid, 391, 1), (sid, 1, 4002), (sid, None, 1)):
            with self.subTest(snap=snap, x=x, y=y), self.assertRaises(rb.BrowserError):
                self.rb.tap_page("b1", snap, x, y)
        with self.assertRaises(rb.SnapshotGone):
            self.rb.tap_page("b1", "s77", 1, 1)
        self.assertEqual(self.ws.calls("Input.dispatchMouseEvent"), [])
        # the scroll could not bring y into view: no click at a wrong place
        self.ws.eval_map = [("scrollTo", [0, 0, 390, 700])]
        with self.assertRaises(rb.BrowserError):
            self.rb.tap_page("b1", sid, 10, 3000)
        for state in ("x", [0, 0, 390], [0, "a", 390, 700], None):
            self.ws.eval_map = [("scrollTo", state)]
            with self.subTest(state=state), self.assertRaises(rb.BrowserError):
                self.rb.tap_page("b1", sid, 10, 10)
        self.assertEqual(self.ws.calls("Input.dispatchMouseEvent"), [])

    def test_tap_page_reports_a_navigation(self):
        meta = self.rb.snapshot("b1")
        self.ws.eval_map = [("scrollTo", [0, 0, 390, 700])]
        rb.TAP_NAV_WAIT = 2.0

        def hook(message):
            if message["method"] == "Input.dispatchMouseEvent" and message["params"]["type"] == "mouseReleased":
                self.ws.emit("Page.frameStartedLoading", {"frameId": "F1"}, "SESSION1")
                self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
                    "targetId": "TARGET00001", "type": "page", "url": "https://example.com/next", "title": "N"}})
                self.ws.emit("Page.frameStoppedLoading", {"frameId": "F1"}, "SESSION1")
        self.ws.hook = hook
        result = self.rb.tap_page("b1", meta["snap_id"], 10, 10)
        self.assertTrue(result["navigated"])
        self.assertTrue(result["snap_needed"])
        self.assertEqual(self.rb._tabs["b1"].url, "https://example.com/next")

    # --- probe with an href from the link map
    def test_probe_accepts_an_href_without_resolving_a_point(self):
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"
        result = self.rb.probe("b1", None, None, href=hoster)
        self.assertEqual(result["links"], [hoster])
        self.assertEqual([m for m in self.ws.calls("Runtime.evaluate")
                          if "elementFromPoint" in m["params"]["expression"]], [])

    def test_probe_href_is_validated(self):
        for bad in ("http://example.com/x", "https://127.0.0.1/x", "https://localhost/x",
                    "https://192.168.1.5/x", "https://nas/x", "javascript:alert(1)", "file:///etc/passwd",
                    "https://example.com:8443/x", "", None, 5, "https://" + "a" * 2100 + ".com/"):
            with self.subTest(href=bad), self.assertRaises(rb.BrowserError):
                self.rb.probe("b1", None, None, href=bad)
        self.assertEqual(self.ws.calls("Target.createTarget")[1:], [])
        with self.assertRaises(rb.BrowserError):          # the Hub's own origin
            def refuse(url):
                raise ValueError("hub")
            self.rb.probe("b1", None, None, url_check=refuse, href="https://example.org/x")

    def test_probe_href_follows_the_link_in_a_hidden_target(self):
        hoster = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") == "SESSION2":
                self.ws.emit("Fetch.requestPaused", {"requestId": "r1", "resourceType": "Document",
                                                     "request": {"url": hoster}}, "SESSION2")
        self.ws.hook = hook
        result = self.rb.probe("b1", None, None, href="https://blog.example.org/go?id=7")
        self.assertEqual(result["links"], [hoster])
        self.assertEqual([m["params"]["url"] for m in self.ws.calls("Page.navigate")
                          if m.get("sessionId") == "SESSION2"], ["https://blog.example.org/go?id=7"])
        self.assertEqual(self.rb._probe_targets, {})

    # --- Cloudflare detection and tab metadata
    def test_cloudflare_challenge_detection(self):
        yes = (("https://example.com/", "Just a moment..."), ("https://example.com/", "JUST A MOMENT"),
               ("https://example.com/", "しばらくお待ちください…"),
               ("https://example.com/cdn-cgi/challenge-platform/h/b", ""),
               ("https://example.com/x?__cf_chl_rt_tk=1/CDN-CGI/CHALLENGE", ""))
        no = (("https://example.com/", "Example"), ("", ""), (None, None),
              ("https://example.com/cdn-cgi/trace", "Moment"))
        for url, title in yes:
            with self.subTest(url=url, title=title):
                self.assertTrue(rb.looks_like_challenge(url, title))
        for url, title in no:
            with self.subTest(url=url, title=title):
                self.assertFalse(rb.looks_like_challenge(url, title))

    def test_meta_snapshot_and_frame_report_the_challenge_flag(self):
        self.assertFalse(self.rb.page_meta("b1", None)["challenge"])
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/", "title": "Just a moment..."}})
        self.barrier()
        self.assertTrue(self.rb.page_meta("b1", None)["challenge"])
        self.assertTrue(self.rb.snapshot("b1")["challenge"])
        self.assertTrue(self.rb.frame("b1", 0, wait=0)["challenge"])

    def test_page_side_challenge_check_catches_a_normal_title(self):
        # v0.1.29: a Cloudflare page that keeps its title is found through the DOM.
        for token in ("_cf_chl_opt", "#challenge-form", "#challenge-stage", ".cf-turnstile",
                      "challenges.cloudflare.com"):
            self.assertIn(token, rb.CHALLENGE_JS)
        self.assertFalse(self.rb.page_meta("b1", None)["challenge"])
        self.ws.eval_map = [("_cf_chl_opt", True)]
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/", "title": "Example"}})
        self.barrier()
        self.assertTrue(self.rb.page_meta("b1", None)["challenge"])
        self.assertTrue(self.rb.frame("b1", 0, wait=0)["challenge"])
        self.assertTrue(self.rb.snapshot("b1")["challenge"])
        self.ws.eval_map = [("_cf_chl_opt", False)]
        # Cached for one second, then re-checked; a navigation marks it stale at once.
        self.assertTrue(self.rb.page_meta("b1", None)["challenge"])
        self.ws.emit("Page.loadEventFired", {}, session="SESSION1")
        self.barrier()
        self.assertFalse(self.rb.page_meta("b1", None)["challenge"])

    def test_page_meta_long_poll_returns_on_a_new_revision_without_a_screencast(self):
        first = self.rb.page_meta("b1", None)
        self.assertIsNone(first["snap"])
        started = []

        def change():
            time.sleep(0.1)
            self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
                "targetId": "TARGET00001", "type": "page", "url": "https://example.com/z", "title": "Z"}})
        thread = threading.Thread(target=change)
        thread.start()
        result = self.rb.page_meta("b1", first["rev"], wait=5)
        thread.join()
        self.assertNotEqual(result["rev"], first["rev"])
        self.assertEqual(result["url"], "https://example.com/z")
        self.assertEqual(self.ws.calls("Page.startScreencast"), [])
        meta = self.rb.snapshot("b1")
        self.assertEqual(self.rb.page_meta("b1", None)["snap"], meta["snap_id"])
        with self.assertRaises(rb.TabGone):
            self.rb.page_meta("b9", None)

    def test_only_fixed_cdp_methods_are_used_for_the_snapshot_path(self):
        meta = self.rb.snapshot("b1")
        self.rb.tile("b1", meta["snap_id"], 0)
        self.ws.eval_map = [("scrollTo", [0, 0, 390, 700])]
        self.rb.tap_page("b1", meta["snap_id"], 5, 5)
        allowed = {"Target.setDiscoverTargets", "Browser.setDownloadBehavior", "Target.getTargets",
                   "Target.createTarget", "Target.attachToTarget", "Page.enable",
                   "Emulation.setFocusEmulationEnabled", "Page.navigate", "Runtime.evaluate",
                   "Fetch.enable", "Emulation.setEmulatedMedia", "Network.enable",
                   "Network.setBlockedURLs", "Emulation.setDeviceMetricsOverride",
                   "Emulation.setTouchEmulationEnabled", "Target.activateTarget",
                   "Page.getLayoutMetrics", "Page.captureScreenshot", "Input.dispatchMouseEvent"}
        self.assertLessEqual(set(self.ws.methods()), allowed)



# ------------------------------------------------------- v0.1.30: viewport link map
class ViewportLinkTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.rb.set_viewport("b1", 390, 700, 2.0)

    def vp_eval(self, links, **extra):
        value = {"scrollX": 0, "scrollY": 120, "width": 390, "height": 700, "links": links}
        value.update(extra)
        self.ws.eval_map = [("__qdlVp", value)] + [e for e in self.ws.eval_map if "__qdlVp" not in e[0]]

    def vp_calls(self):
        return [m for m in self.ws.calls("Runtime.evaluate") if "__qdlVp" in m["params"]["expression"]]

    def meta(self, **kwargs):
        return self.rb.page_meta("b1", None, wait=0, links=True, **kwargs)["viewport_links"]

    def box(self, i=0, **over):
        item = {"x": 10, "y": 20 + i, "w": 80, "h": 18, "href": "https://example.org/%d" % i}
        item.update(over)
        return item

    def test_map_has_rev_scroll_and_links(self):
        self.vp_eval([self.box(0), self.box(1)])
        result = self.meta()
        self.assertEqual(set(result), {"rev", "scrollX", "scrollY", "width", "height", "links"})
        self.assertEqual((result["scrollX"], result["scrollY"], result["width"], result["height"]),
                         (0, 120, 390, 700))
        self.assertEqual([l["href"] for l in result["links"]],
                         ["https://example.org/0", "https://example.org/1"])
        self.assertEqual(set(result["links"][0]), {"x", "y", "w", "h", "href"})
        self.assertIsInstance(result["rev"], int)

    def test_filter_http_only_finite_ints_and_on_screen(self):
        bad = [self.box(0, href="javascript:alert(1)"), self.box(1, href="ftp://example.org/x"),
               self.box(2, href="data:text/html,x"), self.box(3, href=5), self.box(4, href="https://" + "a" * 2100),
               self.box(5, w=0), self.box(6, h=-3), self.box(7, x="1"), self.box(8, y=float("nan")),
               self.box(9, x=True), self.box(10, x=5000), self.box(11, y=700), self.box(12, x=-200),
               self.box(13, y=-60), "text", None, {"href": "https://example.org/z"}]
        good = [self.box(20, x=-30, w=80), self.box(21, href="http://example.org/plain"),
                self.box(22, x=12.6, y=20.4, w=80.5, h=17.5)]
        self.vp_eval(bad + good)
        links = self.meta()["links"]
        self.assertEqual([l["href"] for l in links],
                         ["https://example.org/20", "http://example.org/plain", "https://example.org/22"])
        self.assertEqual((links[2]["x"], links[2]["y"]), (13, 20))
        for link in links:
            for name in ("x", "y", "w", "h"):
                self.assertIsInstance(link[name], int)

    def test_cap_is_600_and_the_script_filters_in_the_page_too(self):
        self.vp_eval([self.box(i % 100, href="https://example.org/%d" % i) for i in range(700)])
        self.assertEqual(len(self.meta()["links"]), 600)
        self.assertEqual(rb.VP_LINK_CAP, 600)
        script = self.vp_calls()[0]["params"]["expression"]
        self.assertTrue(script.endswith("(600,2048,true)"))
        for needle in ("/^https?:/i", "getClientRects", "MutationObserver", "'scroll'", "},200)",
                       "window.innerWidth", "window.__qdlVp"):
            self.assertIn(needle, script)
        self.assertTrue(self.vp_calls()[0]["params"]["returnByValue"])
        self.assertEqual(self.vp_calls()[0]["sessionId"], "SESSION1")

    def test_unchanged_rev_is_answered_from_the_cache_without_scanning(self):
        self.vp_eval([self.box(0)])
        first = self.meta()
        self.assertEqual(len(self.vp_calls()), 1)
        again = self.meta(vp=first["rev"])
        self.assertEqual(again, {"rev": first["rev"], "unchanged": True})
        full = self.meta()                                   # a viewer without a map still gets it
        self.assertEqual(full["links"], first["links"])
        self.assertEqual(len(self.vp_calls()), 1)

    def test_rev_follows_frames_and_a_clean_page_keeps_the_map(self):
        self.vp_eval([self.box(0)])
        first = self.meta()
        header = self.rb.frame("b1", 0, wait=0)["links_rev"]
        self.feed("SESSION1")
        self.assertGreater(self.rb.frame("b1", 0, wait=0)["links_rev"], header)
        # the page reports no change (null): the map stays, under the new revision
        self.ws.eval_map = [("__qdlVp", None)]
        second = self.meta(vp=first["rev"])
        self.assertGreater(second["rev"], first["rev"])
        self.assertEqual(second["links"], first["links"])
        self.assertTrue(self.vp_calls()[-1]["params"]["expression"].endswith(",false)"))
        # a changed page replaces it
        self.feed("SESSION1")
        self.vp_eval([self.box(5)])
        third = self.meta(vp=second["rev"])
        self.assertEqual([l["href"] for l in third["links"]], ["https://example.org/5"])

    def test_navigation_metadata_changes_the_rev(self):
        self.vp_eval([self.box(0)])
        first = self.meta()
        self.ws.emit("Page.frameStartedLoading", {"frameId": "F1"}, "SESSION1")
        self.barrier()
        self.assertGreater(self.meta()["rev"], first["rev"])

    def test_fresh_rescans_even_when_the_rev_is_unchanged(self):
        self.vp_eval([self.box(0)])
        self.meta()
        self.vp_eval([self.box(7)])
        result = self.meta(fresh=True)
        self.assertEqual(result["links"][0]["href"], "https://example.org/7")
        self.assertTrue(self.vp_calls()[-1]["params"]["expression"].endswith(",true)"))

    def test_nothing_is_evaluated_unless_the_map_is_requested(self):
        self.vp_eval([self.box(0)])
        result = self.rb.page_meta("b1", None, wait=0)
        self.assertNotIn("viewport_links", result)
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.vp_calls(), [])

    def test_unreadable_page_gives_none_and_bad_values_give_an_empty_map(self):
        self.ws.fail["Runtime.evaluate"] = "context destroyed"
        self.assertIsNone(self.meta())
        self.ws.fail.clear()
        self.vp_eval("garbage")
        self.assertEqual(self.meta()["links"], [])
        self.vp_eval([self.box(0)], width="x", height=None)
        self.assertEqual(self.meta(fresh=True)["links"], [])


class ProbeToleranceTests(BrowserBase):
    HOSTER = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.rb.set_viewport("b1", 390, 700, 1.0)

    def probe_calls(self):
        return [m for m in self.ws.calls("Runtime.evaluate") if "elementFromPoint" in m["params"]["expression"]]

    def test_resolver_script_is_tolerant_and_fixed(self):
        script = rb.PROBE_JS
        for needle in ("elementFromPoint", "closest('a[href]')", "getClientRects",
                       "Math.max(r.left-x,0,x-r.right)", "Math.max(r.top-y,0,y-r.bottom)",
                       "d>T", "d===bd&&ar<ba", "getComputedStyle"):
            self.assertIn(needle, script)
        self.assertEqual(rb.LINK_NEAR_PX, 28)

    def test_coordinate_probe_uses_the_tolerance(self):
        self.ws.eval_map = [("elementFromPoint", self.HOSTER)]
        result = self.rb.probe("b1", 12.5, 30, url_check=None)
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertTrue(self.probe_calls()[0]["params"]["expression"].endswith("(12.5,30.0,28)"))

    def test_nothing_near_means_no_link_found(self):
        self.ws.eval_map = [("elementFromPoint", None)]
        with self.assertRaises(rb.BrowserError) as caught:
            self.rb.probe("b1", 5, 5)
        self.assertIn("リンクが見つかりません", str(caught.exception))

    def test_fast_display_fallback_scrolls_then_resolves_in_viewport_coordinates(self):
        meta = self.rb.snapshot("b1")
        self.ws.eval_map = [("scrollTo", [0, 600, 390, 700]), ("elementFromPoint", self.HOSTER)]
        result = self.rb.probe("b1", 120, 1000, snap=meta["snap_id"])
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertTrue(self.probe_calls()[0]["params"]["expression"].endswith("(120.0,400.0,28)"))
        scroll = [m for m in self.ws.calls("Runtime.evaluate") if "scrollTo" in m["params"]["expression"]]
        self.assertTrue(scroll[0]["params"]["expression"].endswith("(1000.0)"))

    def test_fallback_with_a_stale_or_bad_snapshot_is_refused(self):
        with self.assertRaises(rb.SnapshotGone):
            self.rb.probe("b1", 1, 1, snap="s9")
        for bad in ("x", 5, "s1; drop"):
            with self.subTest(snap=bad), self.assertRaises(rb.BrowserError):
                self.rb.probe("b1", 1, 1, snap=bad)
        self.assertEqual(self.probe_calls(), [])


# ------------------------------------------------------------ v0.1.30: reader mode
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 20
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HOSTER_A = "https://rapidgator.net/file/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/a.rar.html"
HOSTER_B = "https://rg.to/file/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def b64(data):
    return base64.b64encode(data).decode()


class ReaderBase(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/a", "title": ""}})
        self.barrier()

    def extract(self, **kwargs):
        value = {"title": "T", "blocks": [], "images": [], "hosters": [], "truncated": False}
        value.update(kwargs)
        self.ws.eval_map = [("document.baseURI", value)] + [e for e in self.ws.eval_map if "document.baseURI" not in e[0]]
        return self.rb.reader("b1")

    @staticmethod
    def para(text="x", **run):
        return {"t": "p", "runs": [dict({"text": text}, **run)]}


class ReaderExtractorTests(ReaderBase):
    def test_fixed_extractor_runs_in_the_page_and_returns_plain_data(self):
        self.extract()
        call = [m for m in self.ws.calls("Runtime.evaluate") if "document.baseURI" in m["params"]["expression"]][0]
        self.assertEqual(call["params"]["expression"], rb.READER_JS)
        self.assertTrue(call["params"]["returnByValue"])
        self.assertEqual(call["sessionId"], "SESSION1")
        script = rb.READER_JS
        for needle in ("(^|[-_])(ad|ads|advert|banner|sponsor|promo|popup|cookie)([-_]|$)",
                       "NAV:1", "ASIDE:1", "'HEADER'", "'FOOTER'", "shadowRoot", "assignedNodes", "::before", "SCRIPT:1", "STYLE:1",
                       "aria-hidden", "node.hidden", "cs.display==='none'", "rapidgator", "rg\\.to",
                       "/^https?:/i"):
            self.assertIn(needle, script)
        for needle in ("__CAP__", "__TMAX__", "__BUDGET__", "__IMGCAP__", "__RUNCAP__"):
            self.assertNotIn(needle, script)
        # it only reads: no navigation, no network, no storage
        for forbidden in ("fetch(", "XMLHttpRequest", "location.href=", "location.assign", "document.cookie",
                          "localStorage", "eval(", "innerHTML", "window.open"):
            self.assertNotIn(forbidden, script)

    def test_output_types_and_fields_are_validated(self):
        data = self.extract(title="  Page title\x00 ", images=["https://cdn.example.org/a.jpg"], blocks=[
            {"t": "h", "level": 1, "text": " Heading "},
            {"t": "p", "runs": [{"text": "Hello "}, {"text": "world", "href": "https://example.org/x"},
                                {"text": "bad", "href": "javascript:alert(1)"}]},
            {"t": "li", "runs": [{"text": "item"}], "depth": 2},
            {"t": "img", "i": 0, "alt": "pic", "w": 640, "h": 480, "href": "http://example.org/big"},
            {"t": "pre", "text": "code\n  x"},
            {"t": "hr"},
        ])
        self.assertEqual(data["title"], "Page title")
        self.assertEqual(data["host"], "example.com")
        self.assertEqual(data["blocks"], [
            {"t": "h", "level": 1, "text": "Heading"},
            {"t": "p", "runs": [{"text": "Hello "}, {"text": "world", "href": "https://example.org/x"},
                                {"text": "bad"}]},
            {"t": "li", "runs": [{"text": "item"}], "depth": 2},
            {"t": "img", "i": 0, "alt": "pic", "w": 640, "h": 480, "href": "http://example.org/big"},
            {"t": "pre", "text": "code\n  x"},
            {"t": "hr"},
        ])
        self.assertEqual((data["count"], data["truncated"]), (6, False))

    def test_malformed_blocks_are_dropped(self):
        data = self.extract(images=["https://cdn.example.org/a.jpg", "javascript:1"], blocks=[
            "text", None, 5, [], {}, {"t": "script", "text": "x"}, {"t": "h", "level": 9, "text": "x"},
            {"t": "h", "level": True, "text": "x"}, {"t": "h", "level": 1, "text": 5},
            {"t": "h", "level": 1, "text": "   "}, {"t": "p"}, {"t": "p", "runs": "x"},
            {"t": "p", "runs": [{"text": 5}, {"href": "https://example.org/"}, "x"]},
            {"t": "p", "runs": [{"text": "   "}]}, {"t": "li", "runs": []}, {"t": "pre", "text": "  "},
            {"t": "pre", "text": 5},
            {"t": "img", "i": 5, "alt": "x", "w": 1, "h": 1},          # outside the table
            {"t": "img", "i": 1, "alt": "x", "w": 1, "h": 1},          # its URL was refused
            {"t": "img", "i": -1}, {"t": "img", "i": True}, {"t": "img", "i": "0"},
            {"t": "li", "runs": [{"text": "ok"}], "depth": 99},         # bad depth -> 0
            {"t": "img", "i": 0, "alt": 5, "w": "x", "h": -3, "href": "ftp://x/y"},
        ])
        self.assertEqual(data["blocks"], [
            {"t": "li", "runs": [{"text": "ok"}], "depth": 0},
            {"t": "img", "i": 0, "alt": "", "w": 0, "h": 0},
        ])

    def test_hrefs_are_http_or_https_only_and_bounded(self):
        bad = ["javascript:alert(1)", "data:text/html,x", "ftp://example.org/x", "file:///etc/passwd",
               "//example.org/x", "https://", "https://exa mple.org/", "https://example.org/\n",
               "https://example.org/" + "a" * 2100, "", None, 5]
        runs = [{"text": "t%d" % i, "href": h} for i, h in enumerate(bad)]
        runs += [{"text": "ok1", "href": "https://example.org/ok"}, {"text": "ok2", "href": "http://example.org/ok"}]
        data = self.extract(blocks=[{"t": "p", "runs": runs}])
        got = {r["text"]: r.get("href") for r in data["blocks"][0]["runs"]}
        self.assertEqual({k: v for k, v in got.items() if v}, {"ok1": "https://example.org/ok", "ok2": "http://example.org/ok"})

    def test_text_is_capped_and_control_characters_removed(self):
        data = self.extract(blocks=[
            {"t": "p", "runs": [{"text": "a\x00b\x07c\x1bd\ne\tf"}]},
            {"t": "p", "runs": [{"text": "x" * 30000}]},
            {"t": "pre", "text": "y" * 30000},
            {"t": "h", "level": 2, "text": "z" * 30000},
            {"t": "p", "runs": [{"text": "r" * 15000}, {"text": "s" * 15000}]},
        ])
        blocks = data["blocks"]
        self.assertEqual(blocks[0]["runs"][0]["text"], "abcd\ne\tf")
        self.assertEqual(len(blocks[1]["runs"][0]["text"]), 20000)
        self.assertEqual(len(blocks[2]["text"]), 20000)
        self.assertEqual(len(blocks[3]["text"]), 20000)
        self.assertEqual(sum(len(r["text"]) for r in blocks[4]["runs"]), 20000)

    def test_block_count_is_capped_at_5000(self):
        data = self.extract(blocks=[self.para("p%d" % i) for i in range(6000)])
        self.assertEqual((len(data["blocks"]), data["count"], data["truncated"]), (5000, 5000, True))
        self.assertEqual(rb.READER_BLOCK_CAP, 5000)

    def test_run_count_per_block_is_capped(self):
        data = self.extract(blocks=[{"t": "p", "runs": [{"text": "w"} for _ in range(1000)]}])
        self.assertEqual(len(data["blocks"][0]["runs"]), rb.READER_RUNS_CAP)

    def test_json_size_is_capped_at_2_mb(self):
        blocks = [{"t": "pre", "text": ("%04d" % i) * 5000} for i in range(150)]      # ~3 MB
        data = self.extract(blocks=blocks)
        self.assertTrue(data["truncated"])
        self.assertLess(len(json.dumps(data, ensure_ascii=False)), 2 * 1024 * 1024)
        self.assertGreater(len(data["blocks"]), 50)

    def test_hoster_links_are_appended_as_a_final_block_even_from_skipped_areas(self):
        data = self.extract(blocks=[self.para("see", href=HOSTER_B)], hosters=[
            HOSTER_A, HOSTER_B, HOSTER_A, "https://example.org/file/1", "https://evilrapidgator.net/file/1",
            "https://rapidgator.net.evil.example/file/1", "javascript:1", "http://rapidgator.net/file/ccccccccccccccccccccccccccccccc1"])
        self.assertEqual(data["blocks"][-2:], [
            {"t": "li", "runs": [{"text": HOSTER_A, "href": HOSTER_A}], "depth": 0},
            {"t": "li", "runs": [{"text": "http://rapidgator.net/file/ccccccccccccccccccccccccccccccc1",
                                  "href": "http://rapidgator.net/file/ccccccccccccccccccccccccccccccc1"}], "depth": 0}])
        self.assertEqual(data["blocks"][-3], {"t": "h", "level": 2, "text": "ダウンロードリンク"})
        self.assertEqual(data["hoster_links"], 2)           # HOSTER_B is already in the content
        # nothing to append: no extra block
        plain = self.extract(blocks=[self.para("x")], hosters=["https://example.org/x"])
        self.assertEqual(len(plain["blocks"]), 1)

    def test_hoster_links_survive_the_block_cap(self):
        hosters = ["https://rapidgator.net/file/%032x" % i for i in range(150)]
        data = self.extract(blocks=[self.para("p%d" % i) for i in range(6000)], hosters=hosters)
        self.assertLessEqual(len(data["blocks"]), 5000)
        self.assertEqual(data["blocks"][-(rb.READER_HOSTER_CAP + 1)]["text"], "ダウンロードリンク")
        self.assertEqual(data["hoster_links"], 100)
        self.assertEqual(data["blocks"][-1]["runs"][0]["href"], hosters[99])

    def test_image_urls_never_reach_the_client(self):
        secret = "https://cdn.example.org/private/token-123.jpg"
        data = self.extract(images=[secret], blocks=[{"t": "img", "i": 0, "alt": "a", "w": 1, "h": 1}])
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn("images", data)
        self.assertEqual(self.rb._tabs["b1"].reader_images, [secret])

    def test_image_table_is_capped_and_invalid_urls_are_blanked(self):
        urls = ["https://cdn.example.org/%d.jpg" % i for i in range(400)]
        urls[1] = "javascript:1"
        data = self.extract(images=urls, blocks=[
            {"t": "img", "i": 299, "alt": "", "w": 1, "h": 1}, {"t": "img", "i": 300, "alt": "", "w": 1, "h": 1},
            {"t": "img", "i": 1, "alt": "", "w": 1, "h": 1}])
        table = self.rb._tabs["b1"].reader_images
        self.assertEqual((len(table), table[1]), (300, None))
        self.assertEqual([b["i"] for b in data["blocks"]], [299])

    def test_non_object_result_is_refused(self):
        for value in (None, [], "x", 5):
            with self.subTest(value=value):
                self.ws.eval_map = [("document.baseURI", value)]
                with self.assertRaises(rb.BrowserError):
                    self.rb.reader("b1")

    def test_evaluation_timeout_has_a_clear_japanese_message(self):
        def slow(*args, **kwargs):
            raise rb.BrowserTimeout("ブラウザの操作に失敗しました（Runtime.evaluate: timeout）")
        self.rb._call = slow
        with self.assertRaises(rb.BrowserError) as ctx:
            self.rb.reader("b1")
        self.assertIn("ページの本文取得がタイムアウトしました", str(ctx.exception))
        self.assertNotIn("Runtime.evaluate", str(ctx.exception))

    def test_cdp_timeout_becomes_browser_timeout(self):
        class Conn:
            closed = False
            def call(self, *args):
                raise rb.CDPError("timeout")
        self.rb._conn = Conn()
        with self.assertRaises(rb.BrowserTimeout):
            self.rb._call("Runtime.evaluate")

    def test_unknown_tab(self):
        with self.assertRaises(rb.TabGone):
            self.rb.reader("b9")
        with self.assertRaises(rb.BrowserError):
            self.rb.reader("../x")

    def test_title_falls_back_to_the_tab_title(self):
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/a", "title": "Tab title"}})
        self.barrier()
        self.assertEqual(self.extract(title="")["title"], "Tab title")


class ReaderImageTests(ReaderBase):
    URL = "https://cdn.example.org/pics/a.png"

    def setUp(self):
        super().setUp()
        self.extract(images=[self.URL], blocks=[{"t": "img", "i": 0, "alt": "", "w": 1, "h": 1}])

    def tree(self, url=None, type_="Image", child=False):
        resource = {"url": url or self.URL, "type": type_, "mimeType": "image/png"}
        frame = {"frame": {"id": "FRAME1"}, "resources": [] if child else [resource]}
        if child:
            frame["childFrames"] = [{"frame": {"id": "CHILD1"}, "resources": [resource]}]
        return {"frameTree": frame}

    def content(self, data, encoded=True):
        calls = []

        def answer(params):
            calls.append(params)
            return {"content": b64(data) if encoded else data.decode("latin-1"), "base64Encoded": encoded}
        self.ws.results["Page.getResourceContent"] = answer
        return calls

    def fetch_calls(self):
        return [m for m in self.ws.calls("Runtime.evaluate") if "credentials:'include'" in m["params"]["expression"]]

    def test_loaded_image_comes_from_the_resource_tree(self):
        self.ws.results["Page.getResourceTree"] = self.tree()
        calls = self.content(PNG)
        self.assertEqual(self.rb.reader_image("b1", 0), (PNG, "image/png"))
        self.assertEqual(calls, [{"frameId": "FRAME1", "url": self.URL}])
        self.assertEqual(self.fetch_calls(), [])
        self.assertEqual(self.ws.calls("Page.getResourceTree")[0]["sessionId"], "SESSION1")

    def test_resource_in_a_child_frame_uses_that_frame_id(self):
        self.ws.results["Page.getResourceTree"] = self.tree(child=True)
        calls = self.content(JPEG)
        self.assertEqual(self.rb.reader_image("b1", 0), (JPEG, "image/jpeg"))
        self.assertEqual(calls[0]["frameId"], "CHILD1")

    def test_only_image_resources_with_the_exact_url_count(self):
        for tree in (self.tree(type_="Script"), self.tree(url=self.URL + "?x=1")):
            with self.subTest(tree=tree):
                self.ws.results["Page.getResourceTree"] = tree
                self.ws.eval_map = [("credentials:'include'", {"d": b64(PNG)})]
                self.assertEqual(self.rb.reader_image("b1", 0), (PNG, "image/png"))
                self.assertEqual(self.ws.calls("Page.getResourceContent"), [])

    def test_fetch_inside_the_page_is_the_fallback(self):
        self.ws.results["Page.getResourceTree"] = {"frameTree": {"frame": {"id": "F"}, "resources": []}}
        self.ws.eval_map = [("credentials:'include'", {"d": b64(JPEG)})]
        self.assertEqual(self.rb.reader_image("b1", 0), (JPEG, "image/jpeg"))
        call = self.fetch_calls()[0]
        self.assertTrue(call["params"]["awaitPromise"])
        self.assertTrue(call["params"]["returnByValue"])
        self.assertIn(json.dumps(self.URL), call["params"]["expression"])
        self.assertIn(",%d)" % rb.IMAGE_MAX_BYTES, call["params"]["expression"])
        self.assertIn("arrayBuffer", call["params"]["expression"])

    def test_fetch_is_used_when_the_tree_call_fails(self):
        self.ws.fail["Page.getResourceTree"] = "not supported"
        self.ws.eval_map = [("credentials:'include'", {"d": b64(GIF)})]
        self.assertEqual(self.rb.reader_image("b1", 0), (GIF, "image/gif"))

    def test_both_paths_failing_is_unavailable(self):
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.ws.fail["Page.getResourceContent"] = "No resource"
        for value in ({"err": "fetch"}, {"err": "status"}, {"err": "type"}, None, "x", {}):
            with self.subTest(value=value):
                self.ws.eval_map = [("credentials:'include'", value)]
                with self.assertRaises(rb.ImageUnavailable):
                    self.rb.reader_image("b1", 0)

    def test_size_cap_is_4_mb_on_both_paths(self):
        big = JPEG + b"\x00" * (rb.IMAGE_MAX_BYTES + 1)
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.content(big)
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.content(JPEG + b"\x00" * (rb.IMAGE_MAX_BYTES - len(JPEG)))      # exactly 4 MB is fine
        self.assertEqual(self.rb.reader_image("b1", 0)[1], "image/jpeg")
        self.ws.results["Page.getResourceTree"] = self.tree(type_="Other")
        self.ws.eval_map = [("credentials:'include'", {"d": b64(big)})]
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.ws.eval_map = [("credentials:'include'", {"err": "size"})]
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.ws.eval_map = [("credentials:'include'", {"d": "not base64 !!"})]
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)

    def test_svg_is_refused_everywhere(self):
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.content(SVG, encoded=False)                       # text content of a loaded SVG
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.content(SVG)                                      # even if it were delivered as base64
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.ws.results["Page.getResourceTree"] = self.tree(type_="Other")
        self.ws.eval_map = [("credentials:'include'", {"d": b64(SVG)})]
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        self.assertIn("image/svg", "image/svg+xml")
        self.assertNotIn("svg", json.dumps(sorted(set(rb.RemoteBrowser.sniff_image(d) or "" for d in (PNG, JPEG, GIF, WEBP, AVIF)))))

    def test_type_is_sniffed_from_the_bytes_not_trusted(self):
        cases = ((JPEG, "image/jpeg"), (PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp"),
                 (AVIF, "image/avif"), (b"GIF87a" + b"\x00" * 10, "image/gif"))
        for data, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(rb.RemoteBrowser.sniff_image(data), kind)
        for data in (b"", b"<html>", SVG, b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x00" * 40, b"BM" + b"\x00" * 20,
                     b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 20):
            with self.subTest(data=data[:12]):
                self.assertIsNone(rb.RemoteBrowser.sniff_image(data))
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.content(b"<html><body>not an image</body></html>")
        with self.assertRaises(rb.ImageRefused):
            self.rb.reader_image("b1", 0)
        # labelled as a PNG resource, but the bytes are a JPEG: the sniffed type wins
        self.content(JPEG)
        self.assertEqual(self.rb.reader_image("b1", 0)[1], "image/jpeg")

    def test_unknown_or_refused_index(self):
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.content(PNG)
        for index in (1, 299, 10 ** 6):
            with self.subTest(index=index), self.assertRaises(rb.ReaderImageGone):
                self.rb.reader_image("b1", index)
        self.assertTrue(issubclass(rb.ReaderImageGone, rb.TabGone))          # HTTP 404
        for index in (-1, True, "0", None, 1.5):
            with self.subTest(index=index), self.assertRaises(rb.BrowserError):
                self.rb.reader_image("b1", index)
        with self.assertRaises(rb.TabGone):
            self.rb.reader_image("b9", 0)
        # a new reader run replaces the table: the old index may be gone
        self.extract(images=[], blocks=[])
        with self.assertRaises(rb.ReaderImageGone):
            self.rb.reader_image("b1", 0)

    def test_refused_url_in_the_table_is_not_fetched(self):
        self.extract(images=["javascript:1"], blocks=[])
        with self.assertRaises(rb.ReaderImageGone):
            self.rb.reader_image("b1", 0)
        self.assertEqual(self.fetch_calls(), [])

    def test_only_fixed_cdp_methods_are_used_by_the_reader(self):
        self.ws.results["Page.getResourceTree"] = self.tree()
        self.content(PNG)
        self.rb.reader_image("b1", 0)
        self.ws.eval_map += [("credentials:'include'", {"d": b64(PNG)})]
        self.ws.results["Page.getResourceTree"] = self.tree(type_="Other")
        self.rb.reader_image("b1", 0)
        allowed = {"Target.setDiscoverTargets", "Browser.setDownloadBehavior", "Target.getTargets",
                   "Target.createTarget", "Target.attachToTarget", "Page.enable",
                   "Emulation.setFocusEmulationEnabled", "Page.navigate", "Runtime.evaluate",
                   "Fetch.enable", "Emulation.setEmulatedMedia", "Network.enable",
                   "Network.setBlockedURLs", "Page.getResourceTree", "Page.getResourceContent"}
        self.assertLessEqual(set(self.ws.methods()), allowed)


# ------------------------------------------------------------ v0.1.30: MJPEG stream
class StreamBase(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.parts = []
        self.lock = threading.Lock()
        self.threads = []
        self.addCleanup(self.finish)

    def finish(self):
        for tab in list(self.rb._tabs):
            try:
                self.rb.close_tab(tab)
            except rb.BrowserError:
                pass
        for t in self.threads:
            t.join(timeout=3)

    def run_stream(self, ident="b1", write=None, **kwargs):
        out = []
        errors = []

        def writer(part):
            with self.lock:
                out.append(part)
            if write:
                write(part)

        def target():
            try:
                self.rb.stream(ident, writer, **kwargs)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.threads.append(thread)
        return out, errors, thread

    def wait_for(self, condition, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def jpeg(self, data):
        return base64.b64encode(data).decode()

    @staticmethod
    def split(part):
        head, _, rest = part.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        length = int([l for l in lines if l.startswith(b"Content-Length:")][0].split(b":")[1])
        return lines, rest[:length], rest[length:]



class StreamTests(StreamBase):
    def test_parts_have_boundary_type_and_exact_lengths(self):
        out, errors, thread = self.run_stream()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        for data in (b"\xff\xd8\xff-one", b"\xff\xd8\xff-second-frame"):
            before = len(out)
            self.feed("SESSION1", data=self.jpeg(data))
            self.assertTrue(self.wait_for(lambda: len(out) > before))
            lines, body, tail = self.split(out[-1])
            self.assertEqual(lines[0], b"--qdlframe")
            self.assertIn(b"Content-Type: image/jpeg", lines)
            self.assertIn(b"Content-Length: %d" % len(data), lines)
            self.assertEqual((body, tail), (data, b"\r\n"))
        self.rb.close_tab("b1")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_a_slow_writer_gets_only_the_newest_frame(self):
        gate = threading.Event()
        first = []

        def slow_first(part):
            if not first:
                first.append(1)
                gate.wait(3)
        out, errors, thread = self.run_stream(write=slow_first)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-A"))
        self.assertTrue(self.wait_for(lambda: len(out) == 1))
        for name in (b"B", b"C", b"D"):    # arrive while the writer is blocked
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-" + name))
        gate.set()
        self.assertTrue(self.wait_for(lambda: len(out) >= 2))
        self.assertEqual(self.split(out[1])[1], b"\xff\xd8\xff-D")
        time.sleep(0.2)
        self.assertEqual(len(out), 2)      # B and C were skipped, nothing is repeated early

    def test_heartbeat_resends_the_last_frame(self):
        out, errors, thread = self.run_stream(heartbeat=0.2)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        time.sleep(0.5)
        self.assertEqual(out, [])          # no frame yet: nothing to repeat
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-HB"))
        self.assertTrue(self.wait_for(lambda: len(out) >= 3, 3.0))
        self.assertEqual({self.split(p)[1] for p in out[:3]}, {b"\xff\xd8\xff-HB"})

    def test_limits_per_tab_and_overall(self):
        outs = [self.run_stream("b1"), self.run_stream("b1")]
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 2))
        # v0.1.46: a third stream of the same tab ends the oldest one (a viewer behind a buffering proxy
        # never shows that its old stream is gone) instead of being refused
        outs.append(self.run_stream("b1"))
        outs[0][2].join(3)
        self.assertFalse(outs[0][2].is_alive())
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 2))
        self.assertTrue(issubclass(rb.StreamLimit, rb.ProbeQueueFull))        # HTTP 429
        self.open("https://example.com/b")
        self.open("https://example.com/c")
        outs += [self.run_stream("b2"), self.run_stream("b2")]
        self.assertTrue(self.wait_for(lambda: self.rb._streams == 4))
        with self.assertRaises(rb.StreamLimit) as caught:
            self.rb.stream("b3", lambda part: None)
        self.assertIn("同時接続", str(caught.exception))
        self.assertEqual(self.rb._tabs["b3"].streams, 0)
        self.rb.close_tab("b2")
        self.assertTrue(self.wait_for(lambda: self.rb._streams == 2))
        self.rb.close_tab("b1")
        self.assertTrue(self.wait_for(lambda: self.rb._streams == 0))
        self.assertEqual([e for _, e, _ in outs], [[], [], [], [], []])

    def test_ends_after_max_seconds_on_write_error_and_on_container_stop(self):
        out, errors, thread = self.run_stream(max_seconds=0.3)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual((errors, self.rb._streams, self.rb._tabs["b1"].streams), ([], 0, 0))

        def broken(part):
            raise OSError("peer left")
        out, errors, thread = self.run_stream(write=broken)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-x"))
        thread.join(3)
        self.assertIsInstance(errors[0], OSError)
        self.assertEqual((self.rb._streams, self.rb._tabs["b1"].streams, self.rb._tabs["b1"].pollers), (0, 0, 0))

        out, errors, thread = self.run_stream()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.rb.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_unknown_tab_and_the_stream_counts_as_viewer_activity(self):
        with self.assertRaises(rb.TabGone):
            self.rb.stream("b9", lambda part: None)
        with self.assertRaises(rb.BrowserError):
            self.rb.stream("../x", lambda part: None)
        out, errors, thread = self.run_stream()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        tab = self.rb._tabs["b1"]
        self.assertTrue(self.wait_for(lambda: self.ws.calls("Page.startScreencast")))
        self.assertEqual((tab.screencast, tab.pollers), (True, 1))     # idle-stop sees a viewer
        self.clock.now += 100
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-act"))
        self.assertTrue(self.wait_for(lambda: len(out) == 1))
        self.assertEqual(self.rb._last_activity, self.clock.now)
        self.clock.now += rb.VIEWER_STOP_AFTER + 1
        self.rb.tick()
        self.assertTrue(tab.screencast)       # not stopped while a stream is attached

    def test_meta_reports_stream_fps_and_the_links_rev(self):
        out, errors, thread = self.run_stream()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        for i in range(6):
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-%d" % i))
            self.assertTrue(self.wait_for(lambda: len(out) >= 1))
            time.sleep(0.02)
        meta = self.rb.page_meta("b1", None, wait=0)
        self.assertGreater(meta["fps"], 0)
        self.assertEqual((meta["streams"], meta["width"], meta["height"]), (1, 1280, 2000))
        self.assertGreater(meta["frame_bytes"], 0)
        self.assertIn("links_rev", meta)
        self.rb.close_tab("b1")
        thread.join(3)
        self.assertEqual(self.rb.page_meta.__name__, "page_meta")


# ------------------------------------------------------------ v0.1.37: reader look (srcset, quotes, fallbacks)
class ReaderLookTests(ReaderBase):
    def pick_script(self, body):
        script = rb.READER_JS
        piece = script[script.index("/*PICK*/"):script.index("/*ENDPICK*/")]
        return piece + body

    def run_node(self, body):
        import shutil
        import subprocess
        if shutil.which("node") is None:
            self.skipTest("node missing")
        result = subprocess.run(["node", "-e", self.pick_script(body)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_srcset_chooser_picks_the_smallest_candidate_of_1280_or_more(self):
        self.run_node("""
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(m + ": " + JSON.stringify(a)); process.exit(1); } };
const pick = (s, base) => { const r = pickBest(parseSrcset(s, base), 1280); return r && r.u; };
eq(pick("a 640w, b 1280w, c 2560w", 600), "b", "exact 1280w");
eq(pick("a 640w, b 1600w, c 2560w", 600), "b", "smallest at or above target");
eq(pick("c 2560w, a 640w, b 1600w", 600), "b", "order does not matter");
eq(pick("a 400w, b 800w", 600), "b", "largest when none reaches the target");
eq(pick("a 1x, b 2x, c 3x", 500), "c", "x descriptors scale by the rendered width");
eq(pick("a 1x, b 2x, c 3x", 700), "b", "2x of 700 is 1400");
eq(pick("a, b 2x", 700), "b", "no descriptor counts as 1x");
eq(pick("https://h/w=100,h=50/a.jpg 100w, https://h/w=2000,h=900/b.jpg 2000w", 0), "https://h/w=2000,h=900/b.jpg", "commas inside URLs");
eq(pick("a 1x, b 2x", 0), "b", "x without a base width: the last one");
eq(pick("", 100), null, "empty");
eq(pickBest([{u: "", w: 5}], 1280), null, "empty URL ignored");
eq(parseSrcset("a 640w , b 1280w", 0).map(c => c.w), [640, 1280], "spaces around commas");
eq(pickBest([{u: "s", w: 0}, {u: "t", w: 900}], 1280).u, "t", "unknown widths lose to known ones");
""")

    def test_extractor_uses_the_chooser_and_keeps_lazy_fallbacks(self):
        script = rb.READER_JS
        for needle in ("pickBest(cands,1280)", "data-srcset", "'PICTURE'", "'SOURCE'", "data-lazy-src", "data-original",
                       "BLOCKQUOTE", "pb.q=true", "out.fallbacks", "matchMedia"):
            self.assertIn(needle, script)
        self.assertNotIn("__TARGET__", script)
        self.assertEqual(rb.READER_IMAGE_TARGET, 1280)
        self.assertEqual(rb.IMAGE_MAX_BYTES, 4 * 1024 * 1024)

    def test_quote_flag_is_a_boolean_only(self):
        data = self.extract(blocks=[
            {"t": "p", "runs": [{"text": "a"}], "q": True},
            {"t": "p", "runs": [{"text": "b"}], "q": "yes"},
            {"t": "p", "runs": [{"text": "c"}], "q": 1},
            {"t": "li", "runs": [{"text": "d"}], "depth": 0, "q": True},
            {"t": "p", "runs": [{"text": "e"}], "q": False},
        ])
        self.assertEqual(data["blocks"], [
            {"t": "p", "runs": [{"text": "a"}], "q": True},
            {"t": "p", "runs": [{"text": "b"}]},
            {"t": "p", "runs": [{"text": "c"}]},
            {"t": "li", "runs": [{"text": "d"}], "depth": 0},
            {"t": "p", "runs": [{"text": "e"}]},
        ])

    def test_fallback_urls_stay_on_the_hub_and_are_validated(self):
        big, small = "https://cdn.example.org/big.jpg", "https://cdn.example.org/small.jpg"
        data = self.extract(images=[big, big + "2"], fallbacks=[small, "javascript:1"],
                            blocks=[{"t": "img", "i": 0, "alt": "", "w": 1, "h": 1}])
        self.assertNotIn(small, json.dumps(data))
        self.assertEqual(self.rb._tabs["b1"].reader_fallbacks, [small, None])
        self.extract(images=[big], blocks=[], fallbacks="x")
        self.assertEqual(self.rb._tabs["b1"].reader_fallbacks, [None])

    def test_smaller_candidate_is_used_when_the_large_one_fails_or_is_too_big(self):
        big, small = "https://cdn.example.org/big.jpg", "https://cdn.example.org/small.jpg"
        self.extract(images=[big], fallbacks=[small], blocks=[{"t": "img", "i": 0, "alt": "", "w": 1, "h": 1}])
        asked = []

        def fake(tab, url):
            asked.append(url)
            if url == big:
                raise rb.ImageRefused("画像が大きすぎます")
            return JPEG, "image/jpeg"
        self.rb._fetch_reader_image = fake
        self.assertEqual(self.rb.reader_image("b1", 0), (JPEG, "image/jpeg"))
        self.assertEqual(asked, [big, small])
        # both fail: the last error is reported, nothing else is tried
        asked.clear()

        def failing(tab, url):
            asked.append(url)
            raise rb.ImageUnavailable("画像を取得できません")
        self.rb._fetch_reader_image = failing
        with self.assertRaises(rb.ImageUnavailable):
            self.rb.reader_image("b1", 0)
        self.assertEqual(asked, [big, small])
        # no fallback: only the one URL
        self.extract(images=[big], blocks=[{"t": "img", "i": 0, "alt": "", "w": 1, "h": 1}])
        asked.clear()
        with self.assertRaises(rb.ImageUnavailable):
            self.rb.reader_image("b1", 0)
        self.assertEqual(asked, [big])


# ------------------------------------------------------------ v0.1.46: SSE live stream
class SseStreamTests(StreamBase):
    """The event-stream form of the live stream: same pacing and limits as the MJPEG stream."""

    def run_sse(self, ident="b1", write=None, **kwargs):
        return self.run_stream(ident, write, sse=True, **kwargs)

    @staticmethod
    def events(out):
        """[(event name, data)] of the writes (comments are skipped)."""
        found = []
        for part in out:
            text = part.decode("ascii")
            if text.startswith(":"):
                continue
            lines = text.split("\n")
            found.append((lines[0][len("event: "):], lines[1][len("data: "):]))
            assert text.endswith("\n\n") and len(lines) == 4, text
        return found

    def test_starts_with_2k_padding_then_base64_frames_in_the_event_format(self):
        out, errors, thread = self.run_sse()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.assertTrue(self.wait_for(lambda: len(out) == 1))
        self.assertTrue(out[0].startswith(b":") and out[0].endswith(b"\n\n"))
        self.assertGreaterEqual(len(out[0]), 2048)
        self.assertEqual(out[0].count(b"\n"), 2)             # one comment line, nothing a parser would deliver
        for data in (b"\xff\xd8\xff-one", b"\xff\xd8\xff-second-frame"):
            before = len(out)
            self.feed("SESSION1", data=self.jpeg(data))
            self.assertTrue(self.wait_for(lambda: len(out) > before))
            self.assertEqual(out[-1], b"event: frame\ndata: " + self.jpeg(data).encode() + b"\n\n")
            self.assertEqual(base64.b64decode(self.events(out)[-1][1]), data)
        self.rb.close_tab("b1")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_a_slow_writer_gets_only_the_newest_frame(self):
        gate = threading.Event()
        first = []

        def slow_first(part):
            if part.startswith(b"event:") and not first:
                first.append(1)
                gate.wait(3)
        out, errors, thread = self.run_sse(write=slow_first)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-A"))
        self.assertTrue(self.wait_for(lambda: len(self.events(out)) == 1))
        for name in (b"B", b"C", b"D"):
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-" + name))
        gate.set()
        self.assertTrue(self.wait_for(lambda: len(self.events(out)) >= 2))
        self.assertEqual(base64.b64decode(self.events(out)[1][1]), b"\xff\xd8\xff-D")
        time.sleep(0.2)
        self.assertEqual(len(self.events(out)), 2)

    def test_idle_stream_sends_pings_and_the_heartbeat_resends_the_last_frame(self):
        with mock.patch.object(rb, "SSE_PING_SECONDS", 0.15):
            out, errors, thread = self.run_sse(heartbeat=0.5)
            self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
            self.assertTrue(self.wait_for(lambda: out.count(b": ping\n\n") >= 2, 3.0))
            self.assertEqual(self.events(out), [])                 # no frame yet: nothing to repeat
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-HB"))
            self.assertTrue(self.wait_for(lambda: len(self.events(out)) >= 2, 3.0))
            self.assertEqual({d for _, d in self.events(out)[:2]}, {self.jpeg(b"\xff\xd8\xff-HB")})

    def test_ends_on_a_write_error_after_max_seconds_and_when_the_tab_closes(self):
        out, errors, thread = self.run_sse(max_seconds=0.3)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual((errors, self.rb._streams, self.rb._tabs["b1"].streams), ([], 0, 0))

        def broken(part):
            if part.startswith(b"event:"):
                raise OSError("peer left")
        out, errors, thread = self.run_sse(write=broken)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-x"))
        thread.join(3)
        self.assertIsInstance(errors[0], OSError)
        self.assertEqual((self.rb._streams, self.rb._tabs["b1"].streams, self.rb._tabs["b1"].pollers), (0, 0, 0))

        def broken_at_once(part):
            raise OSError("peer left at the padding")
        out, errors, thread = self.run_sse(write=broken_at_once)
        thread.join(3)
        self.assertIsInstance(errors[0], OSError)
        self.assertEqual((self.rb._streams, self.rb._tabs["b1"].streams), (0, 0))

    def test_a_third_stream_of_a_tab_replaces_the_oldest_instead_of_failing(self):
        # A viewer behind a buffering proxy never shows us that its old stream is gone.
        first = self.run_sse()
        second = self.run_stream()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 2))
        third = self.run_sse()
        first[2].join(3)
        self.assertFalse(first[2].is_alive())
        self.assertTrue(second[2].is_alive() and third[2].is_alive())
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 2))
        self.assertEqual((first[1], second[1], third[1]), ([], [], []))

    def test_mjpeg_and_sse_streams_share_the_overall_limit(self):
        self.open("https://example.com/b")
        self.open("https://example.com/c")
        keep = [self.run_sse("b1"), self.run_stream("b1"), self.run_sse("b2"), self.run_stream("b2")]
        self.assertTrue(self.wait_for(lambda: self.rb._streams == 4))
        with self.assertRaises(rb.StreamLimit):
            self.rb.stream("b3", lambda part: None, sse=True)

    def test_meta_reports_the_fps_of_an_sse_stream(self):
        out, errors, thread = self.run_sse()
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        for i in range(6):
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-%d" % i))
            time.sleep(0.02)
        self.assertTrue(self.wait_for(lambda: len(self.events(out)) >= 1))
        meta = self.rb.page_meta("b1", None, wait=0)
        self.assertGreater(meta["fps"], 0)
        self.assertGreater(meta["frame_bytes"], 0)


# ------------------------------------------------------------ v0.1.46: no docker stats on the /api/state path
# ------------------------------------------------------------ v0.1.68: WebSocket live stream (producer side)
class WsStreamTests(StreamBase):
    """stream(ws=True): raw JPEG bytes per write, newest frame only, pings, alive() and on_start()."""

    def run_ws(self, ident="b1", write=None, **kwargs):
        return self.run_stream(ident, write, ws=True, **kwargs)

    def test_each_write_is_the_raw_jpeg_and_on_start_runs_before_the_first_frame(self):
        started = []
        out, errors, thread = self.run_ws(on_start=lambda: started.append(True))
        self.assertTrue(self.wait_for(lambda: started))
        self.assertEqual((started, out), ([True], []))              # no padding, no frame yet
        for data in (b"\xff\xd8\xff-one", b"\xff\xd8\xff-second"):
            before = len(out)
            self.feed("SESSION1", data=self.jpeg(data))
            self.assertTrue(self.wait_for(lambda: len(out) > before))
            self.assertEqual(out[-1], data)
        self.rb.close_tab("b1")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_a_slow_writer_gets_only_the_newest_frame(self):
        gate = threading.Event()
        first = []

        def slow_first(part):
            if not first:
                first.append(1)
                gate.wait(3)
        out, errors, thread = self.run_ws(write=slow_first)
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-A"))
        self.assertTrue(self.wait_for(lambda: len(out) == 1))
        for name in (b"B", b"C", b"D"):                  # arrive while the writer is blocked (a slow viewer)
            self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-" + name))
        gate.set()
        self.assertTrue(self.wait_for(lambda: len(out) >= 2))
        self.assertEqual(out[1], b"\xff\xd8\xff-D")
        time.sleep(0.2)
        self.assertEqual(len(out), 2)                    # B and C were never queued

    def test_idle_stream_pings_and_a_busy_one_does_not(self):
        pings = []
        with mock.patch.object(rb, "WS_PING_SECONDS", 0.15):
            out, errors, thread = self.run_ws(ping=lambda: pings.append(time.monotonic()), heartbeat=30)
            self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
            self.assertTrue(self.wait_for(lambda: len(pings) >= 2, 3.0))
            self.assertEqual(out, [])
            count = len(pings)
            for n in range(6):                           # frames faster than the ping interval: no ping needed
                self.feed("SESSION1", data=self.jpeg(b"\xff\xd8\xff-%d" % n))
                time.sleep(0.05)
            self.assertLessEqual(len(pings) - count, 1)

    def test_alive_false_ends_the_stream_and_the_slot_is_freed(self):
        alive = [True]
        out, errors, thread = self.run_ws(alive=lambda: alive[0])
        self.assertTrue(self.wait_for(lambda: self.rb._tabs["b1"].streams == 1))
        alive[0] = False
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual((errors, self.rb._tabs["b1"].streams, self.rb._streams), ([], 0, 0))

    def test_a_missing_tab_fails_before_on_start(self):
        started = []
        with self.assertRaises(rb.TabGone):
            self.rb.stream("b9", lambda part: None, ws=True, on_start=lambda: started.append(1))
        self.assertEqual(started, [])


class StatusWithoutDockerStatsTests(BrowserBase):
    def stats_calls(self):
        return [c for c in self.fake.docker_calls() if c[0] == "stats"]

    def test_status_never_starts_docker_stats_by_default(self):
        self.open()
        self.assertEqual(self.stats_calls(), [])
        for _ in range(6):
            self.clock.now += 3
            status = self.rb.status()
            self.rb.resident_status()
        self.assertEqual(self.stats_calls(), [])
        self.assertIsNone(status["memory"])
        self.assertTrue(status["can_open"])

    def test_status_uses_the_cgroup_when_there_is_one(self):
        self.open()
        scope = Path(self.temp.name) / "cg" / "system.slice" / ("docker-" + "ab" * 32 + ".scope")
        scope.mkdir(parents=True)
        (scope / "memory.current").write_text("%d\n" % (300 * 1024 * 1024))
        (scope / "memory.stat").write_text("anon 1\nfile 1\n")
        self.rb._cgroup_root = str(Path(self.temp.name) / "cg")
        self.rb._proc_root = str(Path(self.temp.name) / "noproc")
        self.fake.containers[cr.BROWSER_NAME]["id"] = "ab" * 32
        self.clock.now += 30
        status = self.rb.status()
        self.assertEqual(self.stats_calls(), [])
        self.assertEqual(status["memory"], {"used_mib": 300, "limit_mib": None, "percent": None})

    def test_status_shows_a_cached_result_up_to_120_seconds_old_but_never_refreshes_it(self):
        self.open()
        self.rb.status(fresh_memory=True)                       # the settings view: one docker stats
        self.assertEqual(len(self.stats_calls()), 1)
        self.clock.now += 100
        status = self.rb.status()
        self.assertEqual(status["memory"], {"used_mib": 512, "limit_mib": 2048, "percent": 25.0})
        self.clock.now += 30                                    # now 130 s old: not shown, still not refreshed
        self.assertIsNone(self.rb.status()["memory"])
        self.assertEqual(len(self.stats_calls()), 1)

    def test_fresh_status_caches_docker_stats_for_30_seconds(self):
        self.open()
        for _ in range(4):
            self.rb.status(fresh_memory=True)
        self.assertEqual(len(self.stats_calls()), 1)
        self.clock.now += 31
        self.rb.status(fresh_memory=True)
        self.assertEqual(len(self.stats_calls()), 2)

    def test_the_tab_open_guard_stays_fresh(self):
        self.open()
        before = len(self.stats_calls())
        self.clock.now += 6
        self.assertIsNone(self.rb.memory_reason())
        self.assertEqual(len(self.stats_calls()), before + 1)
        self.fake.stats_out = "1900MiB / 2GiB|92.00%"
        self.clock.now += 6
        self.assertIn("上限", self.rb.memory_reason())

    def test_cached_high_usage_still_blocks_the_open_button_without_a_new_stats_call(self):
        self.open()
        self.fake.stats_out = "1900MiB / 2GiB|92.00%"
        self.clock.now += 6
        self.rb.memory_reason()
        calls = len(self.stats_calls())
        self.clock.now += 10
        status = self.rb.status()
        self.assertFalse(status["can_open"])
        self.assertEqual(len(self.stats_calls()), calls)


# ------------------------------------------------------------ v0.1.46: self-test helpers of the browser
class SelftestHelperTests(BrowserBase):
    def open_selftest(self):
        info = self.rb.open_tab("https://example.com/a", selftest={})
        return info["id"]

    def test_dom_content_loaded_is_counted_for_selftest_tabs_only_and_not_for_about_blank(self):
        tab = self.open_selftest()
        other = self.open("https://example.com/b")["id"]
        self.assertEqual(self.rb.selftest_info(tab)["dcl"], 0)
        self.ws.emit("Page.domContentEventFired", {"timestamp": 0.5}, "SESSION1")     # the initial about:blank
        self.barrier()
        self.assertEqual(self.rb.selftest_info(tab)["dcl"], 0)
        self.ws.emit("Target.targetInfoChanged", {"targetInfo": {
            "targetId": "TARGET00001", "type": "page", "url": "https://example.com/a", "title": "x"}})
        self.barrier()
        self.ws.emit("Page.domContentEventFired", {"timestamp": 1.0}, "SESSION1")
        self.barrier()
        self.assertEqual(self.rb.selftest_info(tab)["dcl"], 1)
        self.ws.emit("Page.domContentEventFired", {"timestamp": 2.0}, "SESSION2")
        self.barrier()
        self.assertEqual(self.rb._tabs[other].dcl_n, 0)
        self.rb._tabs[tab].url = "about:blank"
        self.ws.emit("Page.domContentEventFired", {"timestamp": 3.0}, "SESSION1")
        self.barrier()
        self.assertEqual(self.rb.selftest_info(tab)["dcl"], 1)

    def test_keep_survives_the_idle_stop_and_starts_the_screencast(self):
        tab = self.open_selftest()
        self.rb.selftest_keep(tab, 40)
        t = self.rb._tabs[tab]
        self.assertTrue(t.screencast)
        self.clock.now += rb.VIEWER_STOP_AFTER + 5
        self.rb.tick()
        self.assertTrue(t.screencast)
        self.clock.now += 60
        self.rb.tick()
        self.assertFalse(t.screencast)
        user = self.open("https://example.com/b")["id"]
        with self.assertRaises(rb.BrowserError):
            self.rb.selftest_keep(user, 5)

    def test_scroll_runs_in_an_isolated_world_and_reuses_the_context(self):
        tab = self.open_selftest()
        self.ws.results["Page.getFrameTree"] = {"frameTree": {"frame": {"id": "FRAME1"}}}
        self.ws.results["Page.createIsolatedWorld"] = {"executionContextId": 77}
        self.ws.eval_value = 200
        self.assertEqual(self.rb.selftest_scroll(tab, 200), 200)
        self.assertEqual(self.rb.selftest_scroll(tab, 200), 200)
        self.assertEqual(len(self.ws.calls("Page.createIsolatedWorld")), 1)
        evals = [m for m in self.ws.calls("Runtime.evaluate") if m["params"].get("contextId") == 77]
        self.assertEqual(len(evals), 2)
        self.assertIn("scrollTo", evals[0]["params"]["expression"])
        self.assertIn("200", evals[0]["params"]["expression"])
        user = self.open("https://example.com/b")["id"]
        with self.assertRaises(rb.BrowserError):
            self.rb.selftest_scroll(user, 200)

    def test_scroll_recreates_a_lost_context(self):
        tab = self.open_selftest()
        self.ws.results["Page.getFrameTree"] = {"frameTree": {"frame": {"id": "FRAME1"}}}
        self.ws.results["Page.createIsolatedWorld"] = {"executionContextId": 5}
        self.ws.eval_value = 100
        self.rb.selftest_scroll(tab, 100)
        self.ws.fail["Runtime.evaluate"] = "Cannot find context with specified id"
        self.assertIsNone(self.rb.selftest_scroll(tab, 100))
        self.assertIsNone(self.rb._tabs[tab].iso_ctx)
        del self.ws.fail["Runtime.evaluate"]
        self.assertEqual(self.rb.selftest_scroll(tab, 100), 100)
        self.assertEqual(len(self.ws.calls("Page.createIsolatedWorld")), 3)    # 1 first, 1 retry (failed), 1 after


class FakeOptionRules:
    """The part of storage.Store the browser reads: settings() and site_rule_for()."""

    def __init__(self, **options):
        self.options = dict(options)

    def settings(self):
        return dict(self.options)

    def site_rule_for(self, host):
        return None


class TabLimitTests(BrowserBase):
    """v0.1.65: the tab limit is the option rb_max_tabs (1..20, default 3), not a constant."""

    def limit(self, value):
        self.rb.rules = FakeOptionRules(rb_max_tabs=value, rb_spare_tab=False)
        self.rb.forget_settings_cache()

    def test_default_is_three_without_a_setting_or_with_a_bad_one(self):
        self.assertEqual(self.rb.max_tabs(), 3)
        for bad in (0, 21, -1, "5", 5.0, True, None):
            with self.subTest(value=bad):
                self.limit(bad)
                self.assertEqual(self.rb.max_tabs(), 3)

    def test_the_setting_is_the_ceiling_and_the_refusal_names_it(self):
        self.limit(5)
        for i in range(5):
            self.open("https://example.com/%d" % i)
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open("https://example.com/x")
        self.assertEqual(str(ctx.exception), "NASブラウザのタブは最大5枚です（設定で変更できます）。不要なタブを閉じてください")
        self.assertEqual(len(self.ws.calls("Target.createTarget")), 5)
        self.assertEqual(self.rb.status()["max_tabs"], 5)

    def test_twenty_is_allowed_and_one_works(self):
        self.limit(20)
        self.assertEqual(self.rb.max_tabs(), 20)
        self.limit(1)
        self.open("https://example.com/a")
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open("https://example.com/b")
        self.assertIn("最大1枚", str(ctx.exception))

    def test_a_changed_setting_applies_to_the_next_open_without_a_restart(self):
        self.limit(2)
        self.open("https://example.com/1")
        self.open("https://example.com/2")
        with self.assertRaises(rb.BrowserError):
            self.open("https://example.com/3")
        self.limit(3)
        self.open("https://example.com/3")
        self.assertEqual(len(self.rb.status()["tabs"]), 3)

    def test_the_setting_is_read_at_most_every_two_seconds(self):
        rules = FakeOptionRules(rb_max_tabs=4)
        self.rb.rules = rules
        self.assertEqual(self.rb.max_tabs(), 4)
        rules.options["rb_max_tabs"] = 6
        self.assertEqual(self.rb.max_tabs(), 4)                  # remembered
        self.clock.now += rb.MAX_TABS_TTL + 0.1
        self.assertEqual(self.rb.max_tabs(), 6)

    def test_orphan_eviction_uses_the_setting(self):
        self.limit(5)
        for i in range(5):
            self.open("https://example.com/%d" % i)
            self.clock.now += 1
        self.clock.now += rb.ORPHAN_IDLE
        with self.rb._lock:
            self.rb._tabs["b5"].last_poll = self.clock.now
        self.open("https://example.com/new")
        ids = [t["id"] for t in self.rb.status()["tabs"]]
        self.assertEqual(len(ids), 5)
        self.assertNotIn("b1", ids)                              # the oldest idle one made room
        self.assertIn("b5", ids)
        # below the ceiling nothing is evicted
        self.limit(8)
        self.clock.now += rb.ORPHAN_IDLE + 5
        self.open("https://example.com/more")
        self.assertEqual(len(self.rb.status()["tabs"]), 6)

    def test_self_test_tabs_do_not_count_and_the_capacity_run_may_keep_several(self):
        self.limit(1)
        self.open("https://example.com/a")
        fixture = lambda url: (200, "text/html", b"<title>x</title>")
        selftest = {"fixture": fixture, "capacity": True}
        for i in range(4):
            self.rb.open_tab(rb.SELFTEST_ORIGIN + "/article?k=%d" % i, selftest=selftest)
        self.assertEqual(len(self.rb.selftest_tabs()), 4)
        with self.assertRaises(rb.BrowserError):
            self.rb.open_tab(rb.SELFTEST_ORIGIN + "/article", selftest={"fixture": fixture})    # one at a time otherwise
        self.assertEqual(len(self.rb.status()["tabs"]), 1)

    def test_status_reason_and_the_spare_tab_follow_the_setting(self):
        self.limit(2)
        self.open("https://example.com/1")
        self.assertIsNone(self.rb.status()["reason"])
        self.open("https://example.com/2")
        self.assertEqual(self.rb.status()["reason"], "NASブラウザのタブは最大2枚です（設定で変更できます）。不要なタブを閉じてください")
        self.assertEqual(self.rb._spare_reason(), "タブが上限です")
        self.limit(4)
        self.assertNotEqual(self.rb._spare_reason(), "タブが上限です")

    def test_the_memory_guard_still_refuses_below_the_ceiling(self):
        self.limit(20)
        self.open("https://example.com/1")
        self.set_mem(2 * 1024 * 1024 - 1)
        with self.assertRaises(rb.BrowserError) as ctx:
            self.open("https://example.com/2")
        self.assertIn("空きメモリが不足", str(ctx.exception))        # unchanged message


class ProbeQueueRbTests(BrowserBase):
    """v0.1.65: a long press queues a job; the browser analyses the links one at a time."""
    HOSTER = "https://rapidgator.net/file/0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.open("https://example.com/a")
        self.old = (rb.PROBE_SETTLE, rb.PROBE_TIMEOUT)
        rb.PROBE_SETTLE = 0
        self.addCleanup(lambda: (setattr(rb, "PROBE_SETTLE", self.old[0]), setattr(rb, "PROBE_TIMEOUT", self.old[1])))
        self.active = []
        self.max_active = 0
        self.visited = []

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") != "SESSION1":
                url = message["params"]["url"]
                self.visited.append(url)
                self.max_active = max(self.max_active, len(self.rb._probe_targets))
                session = message["sessionId"]
                self.ws.emit("Fetch.requestPaused", {"requestId": "r", "resourceType": "Document",
                                                    "request": {"url": self.HOSTER}}, session)
        self.ws.hook = hook

    def press(self, n, **kw):
        return self.rb.probe_enqueue("b1", None, None, href="https://blog.example.org/go/%d" % n, **kw)

    def test_enqueue_answers_at_once_and_the_worker_finishes_every_job_in_order(self):
        answers = [self.press(n, label="リンク%d" % n) for n in range(1, 13)]
        self.assertEqual([a["position"] for a in answers[:3]], [1, 2, 3])
        self.assertTrue(all(a["state"] == "queued" or a["state"] == "running" for a in answers))
        self.assertTrue(self.rb.probes.wait_idle(20))
        jobs = self.rb.probe_jobs("b1")["jobs"]
        self.assertEqual([j["state"] for j in jobs], ["done"] * 12)
        self.assertEqual([j["links"] for j in jobs], [[self.HOSTER]] * 12)
        self.assertEqual(self.visited, ["https://blog.example.org/go/%d" % n for n in range(1, 13)])
        self.assertEqual(self.max_active, 1)                   # never two hidden pages at once
        self.assertEqual((self.rb._probes, self.rb._probe_targets, self.rb._probe_sessions), (0, {}, {}))
        self.assertEqual(self.rb.probe_jobs("b1")["summary"]["done"], 12)

    def test_the_same_link_is_not_queued_twice(self):
        first = self.press(1)
        again = self.press(1)
        self.assertEqual((first["existing"], again["existing"], again["job"]), (False, True, first["job"]))

    def test_a_rapidgator_link_needs_no_job(self):
        out = self.rb.probe_enqueue("b1", None, None, href=self.HOSTER + "/x.rar.html")
        self.assertEqual(out["direct"]["links"], [self.HOSTER])
        self.assertEqual(self.rb.probe_jobs("b1")["jobs"], [])
        self.assertEqual(self.visited, [])

    def test_bad_input_is_refused_before_anything_is_queued(self):
        for href in ("http://example.com/", "https://192.168.0.1/", "javascript:alert(1)", "", 5):
            with self.subTest(href=href), self.assertRaises(rb.BrowserError):
                self.rb.probe_enqueue("b1", None, None, href=href)
        with self.assertRaises(rb.BrowserError):
            self.rb.probe_enqueue("b1", None, None, href="https://hub.example/", url_check=lambda u: (_ for _ in ()).throw(ValueError("hub")))
        with self.assertRaises(rb.TabGone):
            self.rb.probe_enqueue("b9", None, None, href="https://example.org/")
        self.assertEqual(self.rb.probe_jobs("b1")["jobs"], [])

    def test_the_hit_test_still_resolves_a_point_at_press_time(self):
        self.ws.eval_map = [("elementFromPoint", "https://blog.example.org/go/9")]
        out = self.rb.probe_enqueue("b1", 10, 20)
        self.assertEqual(out["host"], "blog.example.org")
        self.assertTrue(self.rb.probes.wait_idle(10))
        self.assertEqual(self.visited, ["https://blog.example.org/go/9"])

    def test_a_job_that_hits_an_error_fails_with_the_reason_and_retry_runs_it_again(self):
        self.ws.fail["Fetch.enable"] = "Target closed"
        self.press(1)
        self.assertTrue(self.rb.probes.wait_idle(10))
        job = self.rb.probe_jobs("b1")["jobs"][0]
        self.assertEqual(job["state"], "failed")
        self.assertIn("Target closed", job["error"])
        self.assertEqual((self.rb._probes, self.rb._probe_targets), (0, {}))
        del self.ws.fail["Fetch.enable"]
        self.assertEqual(self.rb.probe_retry("b1"), {"retried": 1})
        self.assertTrue(self.rb.probes.wait_idle(10))
        self.assertEqual(self.rb.probe_jobs("b1")["jobs"][0]["state"], "done")

    def test_closing_the_tab_cancels_the_waiting_jobs(self):
        gate = threading.Event()
        inner = self.ws.hook

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") != "SESSION1":
                gate.wait(10)
            inner(message)
        self.ws.hook = hook
        for n in range(1, 5):
            self.press(n)
        deadline = time.time() + 5
        while not self.rb._probe_targets and time.time() < deadline:
            time.sleep(0.01)
        self.rb.close_tab("b1")
        gate.set()
        self.assertTrue(self.rb.probes.wait_idle(10))
        self.assertEqual(self.visited, ["https://blog.example.org/go/1"])     # the other three never ran
        with self.assertRaises(rb.TabGone):
            self.rb.probe_jobs("b1")
        self.assertEqual(self.rb.probes.jobs("b1"), [])

    def test_the_memory_guard_makes_jobs_wait_not_fail(self):
        self.rb.probes._memory_reason = lambda: "NASブラウザのメモリ使用量が上限の85%に達しています（90%）"
        self.press(1)
        deadline = time.time() + 5
        while self.rb.probe_jobs("b1")["jobs"][0]["wait"] != "memory" and time.time() < deadline:
            time.sleep(0.02)
        job = self.rb.probe_jobs("b1")["jobs"][0]
        self.assertEqual((job["state"], job["wait"]), ("queued", "memory"))
        self.assertIn("85%", job["note"])
        self.assertEqual(self.visited, [])
        self.rb.probes._memory_reason = lambda: None
        self.assertTrue(self.rb.probes.wait_idle(10))
        self.assertEqual(self.rb.probe_jobs("b1")["jobs"][0]["state"], "done")

    def test_meta_carries_the_counts_and_a_change_wakes_the_long_poll(self):
        meta = self.rb.page_meta("b1", wait=0)
        self.assertEqual(meta["probes"]["total"], 0)
        rev, pr = meta["rev"], meta["probes"]["rev"]
        gate = threading.Event()
        inner = self.ws.hook
        self.ws.hook = lambda message: (gate.wait(10) if message["method"] == "Page.navigate" and message.get("sessionId") != "SESSION1" else None, inner(message))
        woke = []

        def poll():
            started = time.time()
            woke.append((self.rb.page_meta("b1", rev=rev, wait=5, pr=pr), time.time() - started))
        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(woke, [])                              # nothing changed: it keeps waiting
        self.press(1)
        thread.join(5)
        self.assertTrue(woke and woke[0][1] < 3, woke)
        self.assertGreater(woke[0][0]["probes"]["rev"], pr)
        self.assertEqual(woke[0][0]["rev"], rev)                # the page itself did not change
        gate.set()
        self.assertTrue(self.rb.probes.wait_idle(10))

    def test_the_synchronous_probe_still_works_for_tools(self):
        result = self.rb.probe("b1", None, None, href="https://blog.example.org/go/1")
        self.assertEqual(result["links"], [self.HOSTER])
        self.assertNotIn("ended", result)
