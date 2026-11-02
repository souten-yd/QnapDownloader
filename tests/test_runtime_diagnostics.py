import tempfile
from pathlib import Path
import unittest
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

    def test_the_port_3128_preflight_is_gone(self):
        # v0.1.33: the Hub-managed native engine has its own checks.
        self.assertFalse(hasattr(diag, "local_jd2_preflight"))
        self.assertFalse(hasattr(diag, "socket"))

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
