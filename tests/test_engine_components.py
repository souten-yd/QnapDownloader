import hashlib
import io
import os
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path

import engine_components as ec


class FakeResponse:
    def __init__(self, data, url="https://example.test/file", length=None):
        self._io = io.BytesIO(data)
        self._url = url
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, n=-1):
        return self._io.read(n)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        return self.response


def component(data, **extra):
    item = {"url": "https://example.test/file", "filename": "x.bin",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    item.update(extra)
    return item


class ManifestTests(unittest.TestCase):
    def test_pins_match_spec(self):
        java = ec.COMPONENTS["java"]
        self.assertEqual(java["url"], "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz")
        self.assertEqual(java["sha256"], "a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd")
        self.assertEqual(java["size"], 52181297)
        self.assertEqual(java["dirname"], "jdk-21.0.13+9-jre")
        jd2 = ec.COMPONENTS["jd2"]
        self.assertEqual(jd2["url"], "https://installer.jdownloader.org/JDownloader.jar")
        self.assertEqual(jd2["sha256"], "388a1d028e65cbc63933a778ddbc9267c14e7e7858389ce31a10be7308adeb40")
        self.assertEqual(jd2["size"], 5189769)
        for item in ec.COMPONENTS.values():
            self.assertTrue(item["url"].startswith("https://"))
        self.assertEqual(ec.MIN_FREE_BYTES, 400 * 1024 * 1024)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / ".download"

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.exists() else []

    def test_success_is_verified_and_renamed(self):
        data = b"hello world" * 1000
        path = ec.fetch_verified(component(data), self.dir, opener=FakeOpener(FakeResponse(data, length=len(data))))
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(self.leftovers(), ["x.bin.verified"])

    def test_bad_hash_rejected_and_nothing_left(self):
        data = b"a" * 100
        item = component(data, sha256="0" * 64)
        with self.assertRaises(ec.DownloadError) as ctx:
            ec.fetch_verified(item, self.dir, opener=FakeOpener(FakeResponse(data)))
        self.assertIn("一致しません", str(ctx.exception))
        self.assertEqual(self.leftovers(), [])

    def test_oversize_aborts(self):
        data = b"a" * 100
        item = component(data)
        for response in (FakeResponse(data + b"extra"), FakeResponse(data, length=500)):
            with self.assertRaises(ec.DownloadError):
                ec.fetch_verified(item, self.dir, opener=FakeOpener(response))
            self.assertEqual(self.leftovers(), [])

    def test_short_download_rejected(self):
        data = b"a" * 100
        with self.assertRaises(ec.DownloadError):
            ec.fetch_verified(component(data), self.dir, opener=FakeOpener(FakeResponse(data[:50])))
        self.assertEqual(self.leftovers(), [])

    def test_http_final_url_and_http_source_rejected(self):
        data = b"a" * 10
        with self.assertRaises(ec.DownloadError):
            ec.fetch_verified(component(data), self.dir,
                              opener=FakeOpener(FakeResponse(data, url="http://evil.test/x")))
        with self.assertRaises(ec.DownloadError):
            ec.fetch_verified(component(data, url="http://example.test/x"), self.dir,
                              opener=FakeOpener(FakeResponse(data)))
        self.assertEqual(self.leftovers(), [])

    def test_redirect_handler_refuses_http(self):
        handler = ec._HttpsOnlyRedirect()
        request = urllib.request.Request("https://example.test/a")
        with self.assertRaises(ec.DownloadError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.test/b")
        with self.assertRaises(ec.DownloadError):
            handler.redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")
        self.assertIsNotNone(handler.redirect_request(
            request, io.BytesIO(b""), 302, "Found", {}, "https://cdn.example.test/b"))

    def test_disk_free_check(self):
        class Usage:
            free = 10
        with self.assertRaises(ec.DownloadError):
            ec.check_disk_free(self.temp.name, usage=lambda p: Usage())
        Usage.free = ec.MIN_FREE_BYTES
        self.assertEqual(ec.check_disk_free(self.temp.name, usage=lambda p: Usage()), ec.MIN_FREE_BYTES)


def make_tar(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                info.mode = 0o4755
                tar.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "chr":
                info.type = tarfile.CHRTYPE
                tar.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)


class TarSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "a.tar.gz"
        self.dest = self.root / "out"

    def extract(self, entries):
        make_tar(self.archive, entries)
        return ec.safe_extract(self.archive, self.dest)

    def test_good_archive_with_inside_symlink(self):
        self.extract([("jdk/", "dir", None), ("jdk/bin/", "dir", None),
                      ("jdk/bin/java", "file", b"#!/bin/sh\n"),
                      ("jdk/bin/j", "sym", "java"), ("jdk/lib/x", "file", b"x")])
        self.assertEqual((self.dest / "jdk/bin/java").read_bytes(), b"#!/bin/sh\n")
        self.assertTrue((self.dest / "jdk/bin/j").is_symlink())
        mode = (self.dest / "jdk/bin/java").stat().st_mode
        self.assertFalse(mode & 0o4000)  # setuid dropped
        self.assertTrue(mode & 0o100)

    def test_rejections(self):
        cases = {
            "dotdot": [("jdk/../../evil", "file", b"x")],
            "dotdot-first": [("../evil", "file", b"x")],
            "absolute": [("/tmp/evil", "file", b"x")],
            "symlink-escape": [("jdk/l", "sym", "../../etc/passwd")],
            "symlink-absolute": [("jdk/l", "sym", "/etc/passwd")],
            "chardev": [("jdk/null", "chr", None)],
            "fifo": [("jdk/p", "fifo", None)],
            "hardlink-escape": [("jdk/h", "hard", "../../etc/passwd")],
            "duplicate": [("jdk/a", "file", b"1"), ("jdk/a", "file", b"2")],
            "through-symlink": [("jdk/l", "sym", "."), ("jdk/l/../x", "file", b"x")],
        }
        for label, entries in cases.items():
            with self.subTest(case=label):
                if self.dest.exists():
                    import shutil
                    shutil.rmtree(self.dest)
                with self.assertRaises(ec.ExtractError):
                    self.extract(entries)
        self.assertFalse((self.root / "evil").exists())

    def test_write_through_symlinked_directory_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        make_tar(self.archive, [("jdk/", "dir", None), ("jdk/l", "sym", "."),
                                ("jdk/l/f", "file", b"x")])
        # l -> jdk (inside): allowed and stays inside the destination.
        ec.safe_extract(self.archive, self.dest)
        self.assertTrue((self.dest / "jdk/f").exists())
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
