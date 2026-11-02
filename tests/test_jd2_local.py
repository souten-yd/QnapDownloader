import json
import os
import tempfile
import threading
import unittest
import unittest.mock
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
        if getattr(self.server, "fail_with_args", False) and "?" in self.path:
            status = 500
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
        self.assertIs(sent["overwritePackagizerRules"], True)   # Hub-owned engine: flat layout wins

    def test_add_link_rejects_non_https_and_relative(self):
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.add_link("http://example.com/a", "/x")
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.add_link("https://example.com/a", "relative")
        self.assertEqual(self.server.paths, [])

    def test_multiple_args_and_preset_flow(self):
        lg = "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings"
        entries = [
            {"interfaceName": lg, "key": "LinkgrabberAutoConfirmEnabled", "storage": "cfg/x", "value": True},
            {"interfaceName": lg, "key": "LinkgrabberAutoStartEnabled", "storage": None, "value": False},
            # same key name under a look-alike interface must NOT match
            {"interfaceName": "org.jdownloader.gui.views.linkgrabber.LinkgrabberSettings",
             "key": "AutoExtractionEnabled", "storage": None, "value": True},
        ]
        self.server.responses["/jd/getCoreRevision"] = {"data": 7}
        self.server.responses["/config/list"] = {"data": entries}
        self.server.responses["/config/get"] = {"data": True}
        info = self.adapter.inspect_preset()
        self.assertTrue(info["entries"]["linkgrabber.auto_confirm"]["supported"])
        self.assertFalse(info["entries"]["extraction.auto_extract"]["supported"])
        self.assertFalse(info["entries"]["downloads.auto_start"]["supported"])
        get_path = [p for p in self.server.paths if p.startswith("/config/get")][0]
        args = [json.loads(urllib.parse.unquote(a)) for a in get_path.split("?")[1].split("&")]
        self.assertEqual(args, [lg, "cfg/x", "LinkgrabberAutoConfirmEnabled"])
        preview = jd2_policy.build_preview(self.adapter)
        self.assertFalse(preview["applicable"])  # unknown keys stay unsupported
        self.server.responses["/config/set"] = {"data": True}
        self.adapter.update_preset("linkgrabber.auto_confirm", False)
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.update_preset("downloads.auto_start", False)   # enum, not bool
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.update_preset("packagizer.package_subfolders", False)

    def test_enum_entry_is_inspected_and_written_as_a_string(self):
        gs = "org.jdownloader.settings.GeneralSettings"
        entries = [{"interfaceName": gs, "key": "AutoStartDownloadOption", "storage": None,
                    "value": "ALWAYS"}]
        self.server.responses["/jd/getCoreRevision"] = {"data": 7}
        self.server.responses["/config/list"] = {"data": entries}
        self.server.responses["/config/get"] = {"data": "ALWAYS"}
        info = self.adapter.inspect_preset()
        self.assertEqual(info["entries"]["downloads.auto_start"]["current"], "ALWAYS")
        self.assertTrue(info["entries"]["downloads.auto_start"]["supported"])
        self.server.responses["/config/set"] = {"data": True}
        self.adapter.update_preset("downloads.auto_start", "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS")
        path = [p for p in self.server.paths if p.startswith("/config/set")][-1]
        args = [json.loads(urllib.parse.unquote(a)) for a in path.split("?")[1].split("&")]
        self.assertEqual(args, [gs, None, "AutoStartDownloadOption", "ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS"])
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.update_preset("downloads.auto_start", "SOMETIMES")

    def test_config_list_uses_five_arg_form_then_falls_back(self):
        self.server.responses["/config/list"] = {"data": []}
        self.adapter._config_entries()
        path = self.server.paths[-1]
        args = [json.loads(urllib.parse.unquote(a)) for a in path.split("?")[1].split("&")]
        self.assertEqual(args, [".*", False, True, False, True])
        self.server.paths.clear()
        self.server.fail_with_args = True
        self.adapter._config_entries()
        self.assertEqual(len(self.server.paths), 2)
        self.assertNotIn("?", self.server.paths[1])

    def test_config_keys_filter(self):
        entries = [
            {"interfaceName": "a.Linkgrabber", "key": "AutoConfirm", "type": "BOOLEAN", "value": True},
            {"interfaceName": "a.General", "key": "DefaultDownloadFolder", "type": "STRING", "value": "/x"},
            {"interfaceName": "a.Account", "key": "ExtractX", "value": 1},
            {"interfaceName": "a.Other", "key": "Unrelated", "value": 1},
        ]
        out = jl.filter_config_keys(entries)
        self.assertEqual([e["key"] for e in out], ["AutoConfirm", "DefaultDownloadFolder"])
        self.assertIsNone(out[1]["value"])

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


SECRET = "Pw:s3cr3t&=?/ \u00e9\u65e5\u672c"


class AccountAdapterTests(unittest.TestCase):
    """v0.1.23 accountsV2 calls: allowlist, GET encoding and secret redaction."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        self.server.paths = []
        self.server.responses = {}
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.engine = Engine(self.server.server_port)
        self.adapter = jl.LocalAdapter(self.engine)

    def decoded_args(self):
        path = self.server.paths[-1]
        prefix, _, query = path.partition("?")
        return prefix, [json.loads(urllib.parse.unquote(a)) for a in query.split("&")] if query else []

    def test_account_calls_are_allowlisted_and_nothing_else_in_the_namespace(self):
        names = sorted(m for ns, m in jl.ALLOWED_CALLS if ns == "accountsV2")
        self.assertEqual(names, sorted([
            "listAccounts", "listPremiumHoster", "addAccount", "refreshAccounts",
            "enableAccounts", "disableAccounts", "setUserNameAndPassword", "removeAccounts"]))
        with self.assertRaises(jl.LocalUnavailable):
            jl.encode_call("accountsV2", "getAccountCookies", ())
        with self.assertRaises(jl.LocalUnavailable):
            jl.encode_call("accounts", "listAccounts", ())

    def test_list_accounts_uses_the_documented_query_and_get_encoding(self):
        self.server.responses["/accountsV2/listAccounts"] = {"data": [{"UUID": 1}, "junk"]}
        self.assertEqual(self.adapter.list_accounts(), [{"UUID": 1}])
        prefix, args = self.decoded_args()
        self.assertEqual(prefix, "/accountsV2/listAccounts")
        self.assertEqual(args, [{"userName": True, "validUntil": True, "trafficLeft": True,
                                 "trafficMax": True, "enabled": True, "valid": True,
                                 "error": True, "maxResults": -1, "startAt": 0}])
        self.server.responses["/accountsV2/listPremiumHoster"] = {"data": ["rapidgator.net"]}
        self.assertEqual(self.adapter.list_premium_hosters(), ["rapidgator.net"])
        self.assertEqual(self.decoded_args(), ("/accountsV2/listPremiumHoster", []))

    def test_write_calls_encode_one_json_argument_each(self):
        self.adapter.add_account("rapidgator.net", "user@example.com", SECRET)
        prefix, args = self.decoded_args()
        self.assertEqual((prefix, args), ("/accountsV2/addAccount",
                                          ["rapidgator.net", "user@example.com", SECRET]))
        self.assertNotIn(" ", self.server.paths[-1])
        self.assertEqual(self.server.paths[-1].count("&"), 2)
        self.adapter.set_credentials(123, "u", SECRET)
        self.assertEqual(self.decoded_args(), ("/accountsV2/setUserNameAndPassword", [123, "u", SECRET]))
        for method, name in ((self.adapter.refresh_accounts, "refreshAccounts"),
                             (self.adapter.enable_accounts, "enableAccounts"),
                             (self.adapter.disable_accounts, "disableAccounts"),
                             (self.adapter.remove_accounts, "removeAccounts")):
            method([5, 6])
            self.assertEqual(self.decoded_args(), ("/accountsV2/" + name, [[5, 6]]))
        for bad in ([], ["1"], [True], [-1], [1] * 51, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(jl.LocalUnavailable):
                    self.adapter.remove_accounts(bad)

    def test_false_results_are_refusals(self):
        self.server.responses["/accountsV2/addAccount"] = {"data": False}
        with self.assertRaises(jl.LocalUnavailable) as ctx:
            self.adapter.add_account("rapidgator.net", "u", SECRET)
        self.assertNotIn(SECRET, str(ctx.exception))

    def assert_redacted(self, error):
        text = repr(error) + str(error) + repr(error.args)
        for needle in (SECRET, urllib.parse.quote(SECRET, safe=""), "s3cr3t", "user%40", "user@example.com",
                       "/accountsV2/addAccount?", "?%22"):
            self.assertNotIn(needle, text)
        self.assertIn("addAccount", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_http_error_does_not_echo_request(self):
        self.server.status = 500
        with self.assertRaises(jl.LocalUnavailable) as ctx:
            self.adapter.add_account("rapidgator.net", "user@example.com", SECRET)
        self.assert_redacted(ctx.exception)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_transport_exception_that_carries_the_path_is_redacted(self):
        def leaky(self_, method, url, **kwargs):
            raise OSError("cannot send " + url)
        import http.client
        with unittest.mock.patch.object(http.client.HTTPConnection, "request", leaky):
            with self.assertRaises(jl.LocalUnavailable) as ctx:
                self.adapter.add_account("rapidgator.net", "user@example.com", SECRET)
        self.assert_redacted(ctx.exception)
        with unittest.mock.patch.object(http.client.HTTPConnection, "request", leaky):
            with self.assertRaises(jl.LocalUnavailable) as ctx:
                self.adapter.set_credentials(9, "user@example.com", SECRET)
        self.assertNotIn(SECRET, repr(ctx.exception) + str(ctx.exception.__context__))

    def test_unparsable_and_oversize_responses_are_redacted(self):
        with unittest.mock.patch.object(jl.json, "loads", side_effect=ValueError("bad " + SECRET)):
            self.server.responses["/accountsV2/addAccount"] = {"data": True}
            with self.assertRaises(jl.LocalUnavailable) as ctx:
                self.adapter.add_account("rapidgator.net", "user@example.com", SECRET)
        self.assert_redacted(ctx.exception)

    def test_closed_port_failure_is_redacted_and_private_calls_never_log(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertLogs(level="DEBUG") as logs, self.assertRaises(jl.LocalUnavailable) as ctx:
            import logging
            logging.getLogger().debug("probe")
            self.adapter.add_account("rapidgator.net", "user@example.com", SECRET)
        self.assert_redacted(ctx.exception)
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_engine_not_running_fails_before_any_request(self):
        self.engine.running = False
        with self.assertRaises(jl.LocalUnavailable):
            self.adapter.add_account("rapidgator.net", "u", SECRET)
        self.assertEqual(self.server.paths, [])
        self.assertFalse(self.adapter.engine_running)


class ControllerCallTests(unittest.TestCase):
    """v0.1.24: download controller and LinkGrabber calls."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        self.server.paths = []
        self.server.responses = {}
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.adapter = jl.LocalAdapter(Engine(self.server.server_port))

    def decoded(self):
        prefix, _, query = self.server.paths[-1].partition("?")
        return prefix, [json.loads(urllib.parse.unquote(a)) for a in query.split("&")] if query else []

    def test_new_calls_are_allowlisted_and_nothing_wider(self):
        for call in (("downloadcontroller", "start"), ("downloadcontroller", "stop"),
                     ("linkgrabberv2", "queryLinks"), ("linkgrabberv2", "moveToDownloadlist")):
            self.assertIn(call, jl.ALLOWED_CALLS)
        for call in (("downloadcontroller", "pause"), ("downloadcontroller", "forceDownload"),
                     ("linkgrabberv2", "removeLinks"), ("linkgrabberv2", "clearList"),
                     ("downloadsV2", "removeLinks")):
            self.assertNotIn(call, jl.ALLOWED_CALLS)
            with self.assertRaises(jl.LocalUnavailable):
                jl.encode_call(call[0], call[1], ())

    def test_cleanup_finished_args_are_exact_and_never_delete_files(self):
        self.assertIn(("downloadsV2", "cleanup"), jl.ALLOWED_CALLS)
        self.adapter.cleanup_finished()
        self.assertEqual(self.decoded(), ("/downloadsV2/cleanup",
                         [[], [], "DELETE_FINISHED", "REMOVE_LINKS_ONLY", "ALL"]))
        self.assertEqual(jl.CLEANUP_MODE, "REMOVE_LINKS_ONLY")
        source = open(jl.__file__, encoding="utf-8").read()
        self.assertNotIn("DELETE_ALL", source)
        self.assertNotIn("DELETE_DISABLED", source)
        self.assertNotIn("REMOVE_LINKS_AND_DELETE_FILES", source)
        self.assertNotIn("REMOVE_LINKS_AND_RECYCLE_FILES", source)

    def test_start_and_stop_take_no_arguments(self):
        self.adapter.start_downloads()
        self.assertEqual(self.decoded(), ("/downloadcontroller/start", []))
        self.adapter.stop_downloads()
        self.assertEqual(self.decoded(), ("/downloadcontroller/stop", []))

    def test_add_link_autostart_is_a_per_call_flag(self):
        self.adapter.add_link("https://example.com/a", "/downloads", autostart=True)
        self.assertIs(self.decoded()[1][0]["autostart"], True)
        self.adapter.add_link("https://example.com/a", "/downloads")
        self.assertIs(self.decoded()[1][0]["autostart"], False)
        self.adapter.add_link("https://example.com/a", "/downloads", autostart="yes")
        self.assertIs(self.decoded()[1][0]["autostart"], False)

    def test_linkgrabber_query_requests_only_minimal_fields_and_returns_names_and_ids(self):
        self.server.responses["/linkgrabberv2/queryLinks"] = {"data": [
            {"name": "a.rar", "host": "rapidgator.net", "bytesTotal": 100, "uuid": 11, "packageUUID": 5,
             "url": "https://rapidgator.net/file/secret", "saveTo": "/share/x", "password": "pw"}]}
        out = self.adapter.list_linkgrabber()
        prefix, args = self.decoded()
        self.assertEqual(prefix, "/linkgrabberv2/queryLinks")
        self.assertEqual(args, [{"name": True, "host": True, "bytesTotal": True, "uuid": True,
                                 "packageUUID": True, "maxResults": 50, "startAt": 0}])
        self.assertEqual(out["items"], [{"id": "11", "package_id": "5", "name": "a.rar",
                                         "host": "rapidgator.net", "bytes_total": 100}])
        self.assertNotIn("secret", str(out))
        self.assertNotIn("/share/x", str(out))

    def test_move_to_downloadlist_sends_link_ids_then_package_ids(self):
        self.adapter.move_to_downloadlist([11, 12], [5])
        self.assertEqual(self.decoded(), ("/linkgrabberv2/moveToDownloadlist", [[11, 12], [5]]))

    def test_move_to_downloadlist_validates_ids_before_any_request(self):
        before = len(self.server.paths)
        for links, packages in (("1", []), ([True], []), ([-1], []), ([2 ** 63], []), ([], [1.5]),
                                (list(range(51)), []), ([], list(range(51))), (None, [])):
            with self.subTest(links=links, packages=packages), self.assertRaises(jl.LocalUnavailable):
                self.adapter.move_to_downloadlist(links, packages)
        self.assertEqual(len(self.server.paths), before)


if __name__ == "__main__":
    unittest.main()
