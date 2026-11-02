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

    def test_links_carry_an_id_string_and_packages_too(self):
        payload = view.snapshot([{"name": "P", "uuid": 9223372036854775807}],
                                [{"name": "f", "uuid": 12, "packageUUID": 9223372036854775807}], 0)
        self.assertEqual(payload["links"][0]["id"], "12")
        self.assertEqual(payload["links"][0]["package_id"], "9223372036854775807")
        self.assertEqual(payload["packages"][0]["id"], "9223372036854775807")
        self.assertIn("uuid", view.LINK_FIELDS)

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

    def test_package_grouping_ids_are_strings(self):
        big = 2**60 + 7
        snap = view.snapshot([{"name": "P", "uuid": big}],
                             [{"name": "a", "packageUUID": big}, {"name": "b", "packageUUID": -1}], 0)
        self.assertEqual(snap["packages"][0]["id"], str(big))
        self.assertEqual(snap["links"][0]["package_id"], str(big))
        self.assertIsNone(snap["links"][1]["package_id"])
        self.assertIn("uuid", view.PACKAGE_FIELDS)
        self.assertIn("packageUUID", view.LINK_FIELDS)

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

class LinkGrabberViewTests(unittest.TestCase):
    def test_query_requests_minimal_fields_without_urls_or_paths(self):
        options = view.linkgrabber_query_options()
        self.assertEqual(options[0]["maxResults"], 50)
        self.assertEqual(options[0]["startAt"], 0)
        self.assertFalse({"url", "password", "downloadPassword", "saveTo", "comment",
                          "availability"}.intersection(options[0]))

    def test_snapshot_projects_names_ids_and_drops_the_rest(self):
        out = view.linkgrabber_snapshot([
            {"uuid": 2 ** 62 + 1, "packageUUID": 9, "name": "x" * 500, "host": "rapidgator.net",
             "bytesTotal": 5, "url": "https://rapidgator.net/file/abc", "saveTo": "/private"},
            {"uuid": "bad", "name": None}])
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["items"][0]["id"], str(2 ** 62 + 1))      # strings: JS loses 64-bit precision
        self.assertEqual(len(out["items"][0]["name"]), 240)
        self.assertIsNone(out["items"][1]["id"])
        self.assertIn("名称不明", out["items"][1]["name"])
        self.assertFalse(out["has_more"])
        self.assertNotIn("/private", str(out))
        self.assertNotIn("rapidgator.net/file", str(out))

    def test_limit_and_shape_are_enforced(self):
        full = view.linkgrabber_snapshot([{"uuid": i} for i in range(50)])
        self.assertTrue(full["has_more"])
        for bad in ("x", {"a": 1}, [{"uuid": 1}] * 51, [5]):
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(ValueError):
                view.linkgrabber_snapshot(bad)


if __name__ == "__main__":
    unittest.main()
