"""v0.1.51: links skipped because the file already exists are removed from the list only."""
import unittest

import jd2_local as jl


class FakeAdapter(jl.LocalAdapter):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _call(self, namespace, method, *args):
        self.calls.append((namespace, method, args))
        if (namespace, method) == ("downloadsV2", "queryLinks"):
            start = args[0]["startAt"] // args[0]["maxResults"]
            return self.pages[start] if start < len(self.pages) else []
        if (namespace, method) == ("downloadsV2", "removeLinks"):
            return None
        raise AssertionError((namespace, method))


class RemoveSkippedExistingTest(unittest.TestCase):
    def test_only_file_exists_skips(self):
        page = [
            {"uuid": 1, "finalLinkState": "FAILED_EXISTS", "skipped": False, "status": ""},
            {"uuid": 2, "skipped": True, "status": "Skipped - File already exists"},
            {"uuid": 3, "skipped": True, "status": "スキップ: ファイルが既に存在します"},
            {"uuid": 4, "skipped": True, "status": "Skipped - No account"},      # other skip: kept
            {"uuid": 5, "finished": True, "status": "Finished"},                  # handled by cleanup
            {"uuid": 6, "status": "Waiting"},
            {"uuid": "7", "finalLinkState": "FAILED_EXISTS"},                     # bad id: ignored
        ]
        fake = FakeAdapter([page])
        self.assertEqual(fake.remove_skipped_existing(), 3)
        removes = [c for c in fake.calls if c[1] == "removeLinks"]
        self.assertEqual(removes, [("downloadsV2", "removeLinks", ([1, 2, 3], []))])
        self.assertIn(("downloadsV2", "removeLinks"), jl.ALLOWED_CALLS)

    def test_pages_and_chunks(self):
        page = [{"uuid": i, "finalLinkState": "FAILED_EXISTS"} for i in range(30)]
        fake = FakeAdapter([page] * 5 + [page[:5]])
        self.assertEqual(fake.remove_skipped_existing(), 155)
        removes = [c for c in fake.calls if c[1] == "removeLinks"]
        self.assertTrue(all(len(c[2][0]) <= jl.ITEM_IDS_MAX for c in removes))

    def test_nothing_to_remove(self):
        fake = FakeAdapter([[{"uuid": 1, "status": "Waiting"}]])
        self.assertEqual(fake.remove_skipped_existing(), 0)
        self.assertFalse([c for c in fake.calls if c[1] == "removeLinks"])


if __name__ == "__main__":
    unittest.main()
