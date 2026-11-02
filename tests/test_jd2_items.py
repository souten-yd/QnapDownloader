"""v0.1.29: per-item start/stop/remove through JD2Controller (fake adapter)."""
import tempfile
import unittest
from pathlib import Path

import jd2_control as jc

BIG = "9223372036854775807"   # 2**63 - 1


class FakeAdapter:
    def __init__(self, root):
        self.calls = []
        self._options = {"allow_link_submit": True, "nas_root": str(root), "jd2_root": "/downloads"}
        self.save_to = {111: "/downloads/sub", 222: "/downloads/sub/deep", int(BIG): "/other/place"}
        self.waiting = [{"id": "501", "package_id": "50", "name": "w1", "host": "", "bytes_total": 1}]

    def list_downloads(self, page=0):
        self.calls.append(("list", page))
        return {"packages": [
            {"id": "111", "name": "Pack", "running": True, "finished": False},
            {"id": "222", "name": "Deep", "running": False, "finished": False},
            {"id": BIG, "name": "Elsewhere", "running": False, "finished": False}],
            "links": [{"id": "333", "package_id": "111", "name": "a.bin", "running": False},
                      {"id": "444", "package_id": None, "name": "orphan", "running": False}]}

    def list_linkgrabber(self):
        return {"items": self.waiting}

    def set_enabled(self, enabled, links, packages): self.calls.append(("enable", enabled, links, packages))
    def force_download(self, links, packages): self.calls.append(("force", links, packages))
    def remove_links(self, links, packages): self.calls.append(("remove", links, packages))
    def delete_with_files(self, links, packages): self.calls.append(("delete", links, packages))
    def remove_waiting(self, links): self.calls.append(("wremove", links))
    def move_to_downloadlist(self, links, packages): self.calls.append(("move", links, packages))
    def start_downloads(self): self.calls.append(("startall",))
    def package_save_path(self, ident): return self.save_to.get(ident)

    def mutations(self):
        return [c for c in self.calls if c[0] != "list"]


class ItemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sub").mkdir()
        self.adapter = FakeAdapter(self.root)
        self.ctl = jc.JD2Controller(self.adapter)
        self.folder = str(self.root / "sub")

    def test_id_validation(self):
        self.assertEqual(jc.parse_ids(["0", BIG, "7", "7"]), [0, 2 ** 63 - 1, 7])
        for bad in ([], "1", [1], [1.5], ["1.5"], ["-1"], ["01"], ["1e3"], [" 1"], ["9223372036854775808"],
                    ["12345678901234567890"], [""], [None], [True], ["１２"], [str(i) for i in range(101)]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    jc.parse_ids(bad)
        self.assertEqual(len(jc.parse_ids([str(i) for i in range(100)])), 100)
        self.assertTrue(all(type(i) is int for i in jc.parse_ids([BIG])))

    def test_unknown_ids_and_kind_are_refused_before_any_call(self):
        for kind, ids in (("package", ["999"]), ("link", ["111"]), ("package", ["111", "999"]), ("x", ["111"])):
            with self.subTest(kind=kind, ids=ids), self.assertRaises(ValueError):
                self.ctl.item_start(kind, ids)
        with self.assertRaises(ValueError):
            self.ctl.item_remove("package", ["999"])
        self.assertEqual(self.adapter.mutations(), [])
        self.assertIn(("list", 0), self.adapter.calls)   # snapshot refreshed server-side

    def test_start_stop_remove_call_arguments_are_exact(self):
        self.ctl.item_start("package", ["111"], page=2)
        self.assertEqual(self.adapter.calls[0], ("list", 2))
        self.assertEqual(self.adapter.mutations(), [("enable", True, [], [111]), ("force", [], [111])])
        self.adapter.calls.clear()
        self.ctl.item_stop("link", ["333"])
        self.assertEqual(self.adapter.mutations(), [("enable", False, [333], [])])
        self.adapter.calls.clear()
        self.ctl.item_remove("link", ["333"])
        self.assertEqual(self.adapter.mutations(), [("remove", [333], [])])
        self.adapter.calls.clear()
        self.ctl.item_start("package", [BIG])
        self.assertEqual(self.adapter.mutations()[0], ("enable", True, [], [2 ** 63 - 1]))

    def test_bulk_remove_is_list_only_and_capped(self):
        result = self.ctl.item_remove("package", ["111", "222"])
        self.assertFalse(result["files_deleted"])
        self.assertEqual(self.adapter.mutations(), [("remove", [], [111, 222])])
        with self.assertRaises(ValueError):
            self.ctl.item_remove("package", [str(i) for i in range(101)])
        with self.assertRaises(ValueError):
            self.ctl.item_remove("package", ["111", "222"], delete_files=True, confirm_name="Pack",
                                 download_dir=self.folder)

    def test_delete_files_needs_exact_name_and_a_path_inside_the_folder(self):
        ok = dict(delete_files=True, download_dir=self.folder)
        for name in (None, "", "pack", "Pack ", 5):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ctl.item_remove("package", ["111"], confirm_name=name, **ok)
        self.assertEqual(self.adapter.mutations(), [])
        with self.assertRaises(ValueError):
            self.ctl.item_remove("package", ["111"], delete_files="true", confirm_name="Pack",
                                 download_dir=self.folder)
        self.assertEqual(self.ctl.item_remove("package", ["111"], confirm_name="Pack", **ok)["files_deleted"], True)
        self.assertEqual(self.adapter.mutations(), [("delete", [], [111])])
        self.adapter.calls.clear()
        self.ctl.item_remove("package", ["222"], confirm_name="Deep", **ok)   # subfolder is inside
        self.assertEqual(self.adapter.mutations(), [("delete", [], [222])])
        self.adapter.calls.clear()
        self.ctl.item_remove("link", ["333"], confirm_name="a.bin", **ok)     # via its package
        self.assertEqual(self.adapter.mutations(), [("delete", [333], [])])

    def test_delete_files_refuses_paths_outside_or_unknown(self):
        ok = dict(delete_files=True, download_dir=self.folder)
        with self.assertRaises(ValueError):
            self.ctl.item_remove("package", [BIG], confirm_name="Elsewhere", **ok)
        with self.assertRaises(ValueError):                       # no package for the file
            self.ctl.item_remove("link", ["444"], confirm_name="orphan", **ok)
        for path in ("/downloads/sub/../x", "/downloads/subway", "/downloads", "relative", None):
            with self.subTest(path=path):
                self.adapter.save_to[111] = path
                with self.assertRaises(ValueError):
                    self.ctl.item_remove("package", ["111"], confirm_name="Pack", **ok)
        with self.assertRaises(ValueError):                       # folder outside the mounted root
            self.ctl.item_remove("package", ["111"], confirm_name="Pack", delete_files=True,
                                 download_dir=str(self.root.parent))
        self.assertEqual(self.adapter.mutations(), [])

    def test_inside_folder_is_lexical_and_strict(self):
        self.assertTrue(jc.inside_folder("/downloads/a", "/downloads"))
        self.assertTrue(jc.inside_folder("/downloads", "/downloads"))
        self.assertFalse(jc.inside_folder("/downloads2", "/downloads"))
        self.assertFalse(jc.inside_folder("/downloads/../etc", "/downloads"))
        self.assertFalse(jc.inside_folder("/x", "/"))

    def test_waiting_remove_and_start_only_known_ids(self):
        self.ctl.waiting_remove(["501"])
        self.assertEqual(self.adapter.mutations(), [("wremove", [501])])
        self.adapter.calls.clear()
        self.ctl.waiting_start(["501"])
        self.assertEqual(self.adapter.mutations(), [("move", [501], []), ("startall",)])
        self.adapter.calls.clear()
        for ids in (["502"], [], ["x"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.ctl.waiting_remove(ids)
        self.assertEqual(self.adapter.mutations(), [])


if __name__ == "__main__":
    unittest.main()
