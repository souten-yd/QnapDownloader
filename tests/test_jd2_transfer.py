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
            jd2_transfer.transfer(FakeJD2(fail=True), url, str(self.target))
        except Exception:
            store.finish_send(record["id"], "uncertain")
        self.assertEqual(store.links()[0]["state"], "uncertain")
        with self.assertRaises(ValueError):
            store.begin_send(record["id"])


if __name__ == "__main__":
    unittest.main()
