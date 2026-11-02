import tempfile
from pathlib import Path
import unittest

import jd2_transfer
import storage


class FakeJD2:
    def __init__(self, good=True, fail=False):
        self._options = {"allow_link_submit": True, "allow_unverified_packagizer": True}
        self.records = []
        self.good = good
        self.fail = fail

    def inspect_preset(self):
        return {"entries": {
            "linkgrabber.auto_confirm": {"supported": True, "current": False},
            "linkgrabber.auto_start": {"supported": True,
                                      "current": False if self.good else True}
        }}

    def add_link(self, url, dest):
        self.records.append((url, dest))
        if self.fail:
            raise OSError("network lost")
        return True


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "Download"
        self.base.mkdir()
        self.target = self.base / "Manga"
        self.target.mkdir()
        self.api = FakeJD2()
        self.api._options.update(nas_root=str(self.base), jd2_root="/downloads")

    def test_explicit_transfer_without_auto_start(self):
        out = jd2_transfer.transfer(self.api, "https://rapidgator.net/file/123",
                                    str(self.target))
        self.assertEqual(out["state"], "sent")
        self.assertEqual(self.api.records, [
            ("https://rapidgator.net/file/123", "/downloads/Manga")])

    def test_unknown_packagizer_and_missing_consent_block(self):
        self.api._options["allow_unverified_packagizer"] = False
        with self.assertRaises(jd2_transfer.TransferBlocked):
            jd2_transfer.transfer(self.api, "https://example.com", str(self.target))
        self.api._options["allow_unverified_packagizer"] = True
        self.api._options["allow_link_submit"] = False
        with self.assertRaises(jd2_transfer.TransferBlocked):
            jd2_transfer.transfer(self.api, "https://example.com", str(self.target))
        self.assertEqual(self.api.records, [])

    def test_active_auto_start_blocks(self):
        self.api.good = False
        with self.assertRaises(jd2_transfer.TransferBlocked):
            jd2_transfer.transfer(self.api, "https://example.com", str(self.target))
        self.assertEqual(self.api.records, [])

    def test_outside_path_symlink_and_invalid_mapping_block(self):
        outside = self.root / "Private"
        outside.mkdir()
        (self.base / "escape").symlink_to(outside, target_is_directory=True)
        for value in (str(outside), str(self.base / "escape")):
            with self.subTest(path=value), self.assertRaises(jd2_transfer.TransferBlocked):
                jd2_transfer.prepare_transfer(self.api, value)
        self.api._options["jd2_root"] = "/downloads/../unsafe"
        with self.assertRaises(jd2_transfer.TransferBlocked):
            jd2_transfer.prepare_transfer(self.api, str(self.target))

    def test_durable_state_prevents_duplicate_after_uncertain_result(self):
        store = storage.Store(self.root)
        self.addCleanup(store.close)
        record = store.add_link("https://rapidgator.net/file/123")
        url = store.begin_send(record["id"])
        self.assertEqual(url, "https://rapidgator.net/file/123")
        with self.assertRaises(ValueError):
            store.begin_send(record["id"])
        try:
            self.api.fail = True
            jd2_transfer.transfer(self.api, url, str(self.target))
        except Exception:
            store.finish_send(record["id"], "uncertain")
        self.assertEqual(store.links()[0]["state"], "uncertain")
        with self.assertRaises(ValueError):
            store.begin_send(record["id"])


class ContainerMappingTests(unittest.TestCase):
    """Container mode: the selected NAS folder is /downloads inside the container."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mounted = (self.root / "Download").resolve()
        (self.mounted / "Manga" / "Vol1").mkdir(parents=True)
        self.api = FakeJD2()
        self.api._options.update(nas_root=str(self.mounted), jd2_root="/downloads")

    def destination(self, path):
        return jd2_transfer.jd2_destination(str(path), self.api._options)

    def test_mounted_folder_and_subpaths_map_under_downloads(self):
        self.assertEqual(self.destination(self.mounted), "/downloads")
        self.assertEqual(self.destination(self.mounted / "Manga"), "/downloads/Manga")
        self.assertEqual(self.destination(self.mounted / "Manga" / "Vol1"), "/downloads/Manga/Vol1")
        out = jd2_transfer.transfer(self.api, "https://rapidgator.net/file/1", str(self.mounted))
        self.assertEqual(out["state"], "sent")
        self.assertEqual(self.api.records, [("https://rapidgator.net/file/1", "/downloads")])

    def test_paths_outside_the_mount_are_refused(self):
        sibling = self.root / "Download2"
        sibling.mkdir()
        other = self.root / "Private"
        other.mkdir()
        (self.mounted / "escape").symlink_to(other, target_is_directory=True)
        for value in (sibling, other, self.root, self.mounted / "escape", self.mounted / "missing",
                      "relative/path"):
            with self.subTest(path=str(value)), self.assertRaises(jd2_transfer.TransferBlocked):
                self.destination(value)

    def test_unmounted_state_refuses(self):
        self.api._options["nas_root"] = None
        with self.assertRaises(jd2_transfer.TransferBlocked):
            self.destination(self.mounted)


if __name__ == "__main__":
    unittest.main()
