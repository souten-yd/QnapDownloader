import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import runtime_diagnostics as diag


class RuntimeDiagnosticTests(unittest.TestCase):
    def test_procfs_read_only_physical_io_counters(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "io")
            path.write_text("rchar: 10000\nread_bytes: 4096\n"
                            "write_bytes: 8192\ncancelled_write_bytes: 512\n")
            result = diag.process_io(str(path))
            self.assertTrue(result["available"])
            self.assertEqual(result["read_bytes"], 4096)
            self.assertEqual(result["write_bytes"], 8192)
            self.assertEqual(result["cancelled_write_bytes"], 512)
            path.write_text("bad_data")
            self.assertFalse(diag.process_io(str(path))["available"])
            path.unlink()
            self.assertFalse(diag.process_io(str(path))["available"])

    def test_fixed_loopback_port_preflight_without_any_http_command(self):
        connection = Mock()
        connect = Mock(return_value=connection)
        result = diag.local_jd2_preflight(connect)
        connect.assert_called_once_with(("127.0.0.1", 3128), timeout=0.35)
        connection.close.assert_called_once_with()
        self.assertTrue(result["tcp_reachable"])
        self.assertFalse(result["operations_enabled"])
        self.assertFalse(result["network_isolation_verified"])
        self.assertFalse(result["api_authenticated"])
        self.assertFalse(result["jd2_identity_verified"])
        connection.sendall.assert_not_called()
        connection.send.assert_not_called()

    def test_connection_failure_never_falls_back_to_lan(self):
        connect = Mock(side_effect=ConnectionRefusedError("refused"))
        result = diag.local_jd2_preflight(connect)
        self.assertFalse(result["tcp_reachable"])
        self.assertFalse(result["operations_enabled"])
        self.assertEqual(connect.call_count, 1)

    def test_log_ring_is_bounded_and_only_nonsecret_enums(self):
        before = len(diag.events())
        for i in range(110):
            diag.event("web_started")
        items = diag.events()
        self.assertEqual(len(items), 64)
        self.assertEqual(set(items[0]), {"at", "type"})
        self.assertTrue(all(x["type"] == "web_started" for x in items))
        with self.assertRaises(ValueError):
            diag.event("password=hunter2")
        self.assertEqual(len(diag.events()), 64)


if __name__ == "__main__":
    unittest.main()
