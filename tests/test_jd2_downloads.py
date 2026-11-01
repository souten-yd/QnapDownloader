import unittest
from unittest.mock import Mock
import jd2_downloads as view
import jd2_local


class DownloadViewTests(unittest.TestCase):
    def test_readonly_query_fields_exclude_urls_passwords_and_paths(self):
        self.assertFalse({"url", "password", "downloadPassword", "saveTo",
                          "comment"}.intersection(view.LINK_FIELDS))
        self.assertFalse({"saveTo", "password"}.intersection(view.PACKAGE_FIELDS))
        options = view.query_options(view.LINK_FIELDS, 2)
        self.assertEqual(options[0]["startAt"], 60)
        self.assertEqual(options[0]["maxResults"], 30)
        self.assertNotIn("url", options[0])
        with self.assertRaises(ValueError):
            view.query_options(view.LINK_FIELDS, 21)
        with self.assertRaises(ValueError):
            view.query_options(view.LINK_FIELDS, True)

    def test_normalize_progress_and_ignore_extra_secrets(self):
        payload = view.snapshot([{
            "name": "Archive", "bytesLoaded": 500, "bytesTotal": 1000,
            "speed": 250, "running": True, "childCount": 2,
            "saveTo": "/share/private", "password": "super-secret"
        }], [{
            "name": "video.zip", "host": "rapidgator.net", "bytesLoaded": 99,
            "bytesTotal": 100, "speed": 2, "eta": 5, "finished": False,
            "url": "https://example.com/private", "password": "super-secret",
            "internalPath": "/share/private"
        }], 0)
        self.assertEqual(payload["packages"][0]["progress"], 50.0)
        self.assertEqual(payload["links"][0]["progress"], 99.0)
        self.assertEqual(payload["packages"][0]["child_count"], 2)
        self.assertEqual(payload["links"][0]["host"], "rapidgator.net")
        self.assertEqual(payload["links"][0]["eta_seconds"], 5)
        self.assertEqual(payload["source"], "local-readonly")
        self.assertNotIn("super-secret", str(payload))
        self.assertNotIn("/share/private", str(payload))
        self.assertNotIn("example.com/private", str(payload))

    def test_unknown_and_invalid_values_are_not_guessed(self):
        snap = view.snapshot([], [{
            "name": "", "speed": -1, "bytesLoaded": 400,
            "bytesTotal": 200, "running": "yes", "finished": 1, "eta": True
        }], 0)
        item = snap["links"][0]
        self.assertEqual(item["progress"], 100)
        self.assertIsNone(item["speed_bytes"])
        self.assertIsNone(item["running"])
        self.assertIsNone(item["finished"])
        self.assertIsNone(item["eta_seconds"])
        self.assertIn("名称不明", item["name"])
        self.assertIsNone(view.snapshot([], [{"name":"a"}],0)["links"][0]["progress"])

    def test_oversized_or_malformed_response_rejected(self):
        with self.assertRaises(ValueError):
            view.snapshot([{}] * 31, [], 0)
        with self.assertRaises(ValueError):
            view.snapshot({}, [], 0)
        with self.assertRaises(ValueError):
            view.snapshot([], [None], 0)

    def test_local_adapter_uses_query_methods_without_mutations(self):
        calls = []

        class Engine:
            preset_applied = False

            def is_ours_running(self):
                return True

            def connection_target(self):
                return 1

        adapter = jd2_local.LocalAdapter(Engine())

        def fake(namespace, method, *args):
            calls.append((namespace, method, args))
            if method == "queryPackages":
                return [{"name": "Pictures", "bytesTotal": 50}]
            return [{"name": "a.zip", "bytesLoaded": 5, "bytesTotal": 10}]
        adapter._call = fake
        result = adapter.list_downloads(0)
        self.assertEqual(result["packages"][0]["name"], "Pictures")
        self.assertEqual(result["links"][0]["progress"], 50)
        self.assertEqual([c[:2] for c in calls], [("downloadsV2", "queryPackages"),
                                                  ("downloadsV2", "queryLinks")])
        self.assertNotIn("password", str(calls))
        self.assertNotIn("url", str(calls))

if __name__ == "__main__":
    unittest.main()
