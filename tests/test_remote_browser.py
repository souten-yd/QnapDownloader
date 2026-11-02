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
        self.eval_map = []             # (expression substring, value) overrides for Runtime.evaluate
        self.hook = None               # called with every command before it is answered
        self.history = {"currentIndex": 1, "entries": [{"id": 5}, {"id": 6}, {"id": 7}]}

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
        self.assertIn("(10.0,10.0)", expression["params"]["expression"])
        self.assertIn("closest('a')", expression["params"]["expression"])

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

    def test_one_probe_per_tab_at_a_time_and_flag_is_released(self):
        gate = threading.Event()
        release = threading.Event()

        def hook(message):
            if message["method"] == "Page.navigate" and message.get("sessionId") == "SESSION2":
                gate.set()
                release.wait(5)
        self.ws.hook = hook
        rb.PROBE_TIMEOUT = 1
        self.rb._clock = TickClock()
        out = []
        thread = threading.Thread(target=lambda: out.append(self.rb.probe("b1", 5, 5)))
        thread.start()
        self.assertTrue(gate.wait(5))
        with self.assertRaises(rb.BrowserError) as error:
            self.rb.probe("b1", 6, 6)
        self.assertIn("確認中", str(error.exception))
        release.set()
        thread.join(10)
        self.assertEqual(len(out), 1)
        self.assertFalse(self.rb._tabs["b1"].probing)
        self.ws.hook = None
        self.assertEqual(self.rb.probe("b1", 7, 7)["start_url"], self.START)

    def test_global_probe_cap_counts_against_all_tabs(self):
        self.open("https://example.com/b")
        with self.rb._lock:
            self.rb._probes = rb.MAX_PROBES
        with self.assertRaises(rb.BrowserError):
            self.rb.probe("b2", 5, 5)
        self.assertFalse(self.rb._tabs["b2"].probing)
        self.assertEqual(self.rb._probes, rb.MAX_PROBES)

    def test_memory_guard_applies_before_a_target_is_created(self):
        self.set_mem(100 * 1024)
        before = len(self.ws.calls("Target.createTarget"))
        with self.assertRaises(rb.BrowserError) as error:
            self.rb.probe("b1", 5, 5)
        self.assertIn("メモリ", str(error.exception))
        self.assertEqual(len(self.ws.calls("Target.createTarget")), before)
        self.assertEqual(self.rb._probes, 0)

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

