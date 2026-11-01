import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jd2_local as jl
import jd2_policy


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def row(local, state="0A", inode=1234):
    return ("   0: %s 00000000:0000 %s 00000000:00000000 00:00000000 00000000  1000        0 %d 1 0000000000000000 100 0 0 10 0\n"
            % (local, state, inode))


class FakeProc:
    def __init__(self, root, pid=4242, owned=(1234,)):
        self.root = Path(root)
        (self.root / "net").mkdir(parents=True)
        fd = self.root / str(pid) / "fd"
        fd.mkdir(parents=True)
        for i, inode in enumerate(owned):
            os.symlink("socket:[%d]" % inode, str(fd / str(10 + i)))
        os.symlink("/dev/null", str(fd / "0"))
        self.pid = pid

    def tcp(self, v4="", v6=None):
        (self.root / "net" / "tcp").write_text(HEADER + v4)
        if v6 is not None:
            (self.root / "net" / "tcp6").write_text(HEADER + v6)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = FakeProc(self.temp.name)

    def verify(self):
        return jl.verify_listener(3128, self.proc.pid, str(self.proc.root))

    def test_accepts_loopback_owned_by_pid(self):
        self.proc.tcp(row("0100007F:0C38"))
        self.assertTrue(self.verify())
        self.proc.tcp("", row("00000000000000000000000001000000:0C38"))
        self.assertTrue(self.verify())
        self.proc.tcp(row("0100007F:0C38"), row("0000000000000000FFFF00000100007F:0C38"))
        self.assertTrue(self.verify())

    def test_rejects_wildcard_and_lan_addresses(self):
        for v4, v6 in ((row("00000000:0C38"), ""),
                       (row("0100007F:0C38"), row("00000000000000000000000000000000:0C38")),
                       (row("0100A8C0:0C38"), ""),
                       (row("0100007F:0C38") + row("00000000:0C38", inode=1234), "")):
            with self.subTest(v4=v4, v6=v6):
                self.proc.tcp(v4, v6)
                with self.assertRaises(jl.ListenerError):
                    self.verify()

    def test_rejects_foreign_inode_and_missing_listener(self):
        self.proc.tcp(row("0100007F:0C38", inode=9999))
        with self.assertRaises(jl.ListenerError):
            self.verify()
        self.proc.tcp(row("0100007F:0C39"))
        with self.assertRaises(jl.ListenerError):
            self.verify()
        # Same port in non-LISTEN state is ignored.
        self.proc.tcp(row("0100007F:0C38", state="01"))
        with self.assertRaises(jl.ListenerError):
            self.verify()
        # One owned loopback listener plus one foreign: reject.
        self.proc.tcp(row("0100007F:0C38") + row("0100007F:0C38", inode=777))
        with self.assertRaises(jl.ListenerError):
            self.verify()

    def test_missing_pid_or_proc_files(self):
        self.proc.tcp(row("0100007F:0C38"))
        with self.assertRaises(jl.ListenerError):
            jl.verify_listener(3128, 99999, str(self.proc.root))
        (self.proc.root / "net" / "tcp").unlink()
        with self.assertRaises(jl.ListenerError):
            self.verify()


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        namespace = self.path.split("?")[0]
        if namespace == "/big/x":
            body = b"[" + b"1," * (jl.MAX_RESPONSE // 2) + b"1]"
        else:
            body = json.dumps(self.server.responses.get(namespace, {"data": None})).encode()
        status = self.server.status
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Engine:
    def __init__(self, port):
        self.port = port
        self.preset_applied = True
        self.running = True

    def connection_target(self):
        if not self.running:
            raise jl.LocalUnavailable("エンジン未起動/未導入")
        return self.port

    def is_ours_running(self):
        return self.running

    def note_preset_applied(self):
        pass

    def note_preset_stale(self):
        pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        self.server.paths = []
        self.server.responses = {}
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.engine = Engine(self.server.server_port)
        self.adapter = jl.LocalAdapter(self.engine)

    def test_unwraps_data_and_plain_values(self):
        self.server.responses["/jd/getCoreRevision"] = {"data": 12345}
        self.assertEqual(self.adapter.core_revision(), 12345)
        self.server.responses["/update/isUpdateAvailable"] = True
        self.assertTrue(self.adapter.is_update_available())
        self.server.responses["/update/isUpdateAvailable"] = {"data": False}
        self.assertFalse(self.adapter.is_update_available())

    def test_add_link_url_encoding_and_safe_flags(self):
        self.server.responses["/linkgrabberv2/addLinks"] = {"data": {"id": 1}}
        self.adapter.add_link("https://example.com/a?b=1&c=2", "/share/Download/日本語 dir")
        path = self.server.paths[-1]
        prefix, _, query = path.partition("?")
        self.assertEqual(prefix, "/linkgrabberv2/addLinks")
        self.assertNotIn(" ", path)
        self.assertNotIn("&", query)  # single argument; inner & is encoded
        sent = json.loads(urllib.parse.unquote(query))
        self.assertEqual(sent["links"], "https://example.com/a?b=1&c=2")
        self.assertEqual(sent["destinationFolder"], "/share/Download/日本語 dir")
        self.assertIs(sent["autostart"], False)
        self.assertIs(sent["overwritePackagizerRules"], False)

    def test_add_link_rejects_non_https_and_relative(self):
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.add_link("http://example.com/a", "/x")
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.add_link("https://example.com/a", "relative")
        self.assertEqual(self.server.paths, [])

    def test_multiple_args_and_preset_flow(self):
        entries = [
            {"interfaceName": "org.jdownloader.gui.views.linkgrabber.LinkgrabberSettings",
             "key": "LinkgrabberAutoConfirmEnabled", "storage": "cfg/x", "value": True},
            {"interfaceName": "org.jdownloader.gui.views.linkgrabber.LinkgrabberSettings",
             "key": "LinkgrabberAutoStartEnabled", "storage": None, "value": False},
        ]
        self.server.responses["/jd/getCoreRevision"] = {"data": 7}
        self.server.responses["/config/list"] = {"data": entries}
        self.server.responses["/config/get"] = {"data": True}
        info = self.adapter.inspect_preset()
        self.assertTrue(info["entries"]["linkgrabber.auto_confirm"]["supported"])
        self.assertFalse(info["entries"]["extraction.auto_extract"]["supported"])
        get_path = [p for p in self.server.paths if p.startswith("/config/get")][0]
        args = [json.loads(urllib.parse.unquote(a)) for a in get_path.split("?")[1].split("&")]
        self.assertEqual(args, ["org.jdownloader.gui.views.linkgrabber.LinkgrabberSettings",
                                "cfg/x", "LinkgrabberAutoConfirmEnabled"])
        preview = jd2_policy.build_preview(self.adapter)
        self.assertFalse(preview["applicable"])  # unknown keys stay unsupported
        self.server.responses["/config/set"] = {"data": True}
        self.adapter.update_preset("linkgrabber.auto_confirm", False)
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.update_preset("downloads.auto_start", False)

    def test_default_download_folder_found_and_missing(self):
        entries = [{"interfaceName": "org.jdownloader.settings.GeneralSettings",
                    "key": "DefaultDownloadFolder", "storage": "cfg/g", "value": "/old"}]
        self.server.responses["/config/list"] = {"data": entries}
        self.server.responses["/config/set"] = {"data": True}
        self.server.responses["/config/get"] = {"data": "/share/Download"}
        self.assertTrue(self.adapter.set_default_download_folder("/share/Download"))
        self.server.responses["/config/get"] = {"data": "/other"}
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.set_default_download_folder("/share/Download")
        self.server.responses["/config/list"] = {"data": []}
        self.assertFalse(self.adapter.set_default_download_folder("/share/Download"))

    def test_allowlist_and_limits(self):
        with self.assertRaises(jl.LocalUnavailable):
            jl.encode_call("system", "exit", ())
        with self.assertRaises(jl.LocalUnavailable):
            jl.encode_call("jd", "getCoreRevision/../x", ())
        self.server.status = 500
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.core_revision()
        self.server.status = 200
        self.server.responses["/jd/getCoreRevision"] = {"data": True}
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.core_revision()

    def test_oversize_response_rejected(self):
        original = jl.ALLOWED_CALLS
        jl.ALLOWED_CALLS = original | {("big", "x")}
        self.addCleanup(setattr, jl, "ALLOWED_CALLS", original)
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter._call("big", "x")

    def test_fail_closed_when_engine_not_running(self):
        self.engine.running = False
        with self.assertRaises(jl.LocalUnavailable) as ctx:
            self.adapter.core_revision()
        self.assertIn("エンジン未起動/未導入", str(ctx.exception))
        self.assertFalse(self.adapter.allow_update)
        self.assertFalse(self.adapter.can_send)
        self.assertEqual(self.server.paths, [])

    def test_send_options_follow_preset_state(self):
        self.assertTrue(self.adapter._options["allow_link_submit"])
        self.engine.preset_applied = False
        self.assertFalse(self.adapter._options["allow_link_submit"])


if __name__ == "__main__":
    unittest.main()
