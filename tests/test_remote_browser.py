"""Remote browser: WebSocket framing, CDP manager with a fake transport. No docker, no network."""
import io
import json
import queue
import struct
import tempfile
import threading
import unittest
from pathlib import Path

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
        self.history = {"currentIndex": 1, "entries": [{"id": 5}, {"id": 6}, {"id": 7}]}

    def send_text(self, text):
        message = json.loads(text)
        self.sent.append(message)
        method = message["method"]
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
            result = {"result": {"type": "object", "value": self.eval_value}}
        elif method == "Page.getNavigationHistory":
            result = self.history
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
        self.rb.status()
        self.rb.status()
        self.rb.memory_reason()
        self.assertLessEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 1)
        self.clock.now += 6
        self.rb.status()
        self.assertEqual(len([c for c in self.fake.docker_calls() if c[0] == "stats"]) - before, 2)
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
            "format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 2000, "everyNthFrame": 1})
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


class InputTests(BrowserBase):
    def setUp(self):
        super().setUp()
        self.open()

    def mouse(self):
        return [m["params"] for m in self.ws.calls("Input.dispatchMouseEvent")]

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
        self.rb.send_input("b1", {"type": "reload"})
        self.assertEqual(len(self.ws.calls("Page.reload")), 1)

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
                   "Fetch.enable"}
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
        self.assertEqual(self.rb.set_viewport("b1", 390, 700, 3), {"ok": True, "changed": True})
        override = self.ws.calls("Emulation.setDeviceMetricsOverride")[0]
        self.assertEqual(override["params"], {"width": 390, "height": 700,
                                              "deviceScaleFactor": 2.0, "mobile": True})
        self.assertEqual(override["sessionId"], "SESSION1")
        self.assertEqual(self.ws.calls("Emulation.setTouchEmulationEnabled")[0]["params"], {"enabled": True})
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.ws.calls("Page.startScreencast")[-1]["params"], {
            "format": "jpeg", "quality": 50, "maxWidth": 780, "maxHeight": 1400, "everyNthFrame": 1})

    def test_desktop_viewport_quality_and_no_touch_emulation(self):
        self.rb.set_viewport("b1", 1200, 800, 1)
        self.assertEqual(self.ws.calls("Emulation.setDeviceMetricsOverride")[0]["params"], {
            "width": 1200, "height": 800, "deviceScaleFactor": 1.0, "mobile": False})
        self.assertEqual(self.ws.calls("Emulation.setTouchEmulationEnabled"), [])
        self.rb.frame("b1", 0, wait=0)
        self.assertEqual(self.ws.calls("Page.startScreencast")[-1]["params"], {
            "format": "jpeg", "quality": 60, "maxWidth": 1200, "maxHeight": 800, "everyNthFrame": 1})

    def test_boundary_at_760_is_desktop_and_759_is_mobile(self):
        self.rb.set_viewport("b1", 759, 600, 2)
        self.rb.set_viewport("b1", 760, 600, 2)
        flags = [m["params"]["mobile"] for m in self.ws.calls("Emulation.setDeviceMetricsOverride")]
        self.assertEqual(flags, [True, False])
        self.assertEqual([m["params"]["enabled"] for m in self.ws.calls("Emulation.setTouchEmulationEnabled")],
                         [True, False])

    def test_resize_restarts_a_running_screencast_and_same_size_is_a_noop(self):
        self.rb.set_viewport("b1", 390, 700, 2)
        self.rb.frame("b1", 0, wait=0)
        starts = len(self.ws.calls("Page.startScreencast"))
        self.assertEqual(self.rb.set_viewport("b1", 390, 700, 2)["changed"], False)
        self.assertEqual(len(self.ws.calls("Emulation.setDeviceMetricsOverride")), 1)
        self.rb.set_viewport("b1", 700, 390, 2)
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
