import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater


class UpdaterTests(unittest.TestCase):
    def asset(self, contents=b"verified qpkg"):
        name = "QnapDownloader_1.2.3_x86_64.qpkg"
        return {"name": name, "size": len(contents),
                "digest": "sha256:" + hashlib.sha256(contents).hexdigest(),
                "browser_download_url": f"https://github.com/{updater.REPO}/releases/download/v1.2.3/{name}"}

    def test_select_requires_stable_newer_exact_asset(self):
        release = {"tag_name": "v1.2.3", "assets": [self.asset()]}
        self.assertIsNotNone(updater.select(release, "1.2.2", "x86_64"))
        self.assertIsNone(updater.select(release, "1.2.3", "x86_64"))
        self.assertIsNone(updater.select({**release, "prerelease": True}, "1.2.2", "x86_64"))
        self.assertIsNone(updater.select({**release, "draft": True}, "1.2.2", "x86_64"))
        for changed in ({"digest": None}, {"size": 0}, {"browser_download_url": "https://evil.example/q"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                updater.select({**release, "assets": [{**self.asset(), **changed}]}, "1.2.2", "x86_64")
        with self.assertRaises(ValueError):
            updater.select(release, "1.2.2", "../x86_64")

    def test_download_verifies_bytes_and_never_overwrites(self):
        contents = b"verified qpkg"
        release = {"tag_name": "v1.2.3", "assets": [self.asset(contents)]}
        asset = updater.select(release, "1.2.2", "x86_64")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(contents)):
                path = updater.stage(asset, directory)
            self.assertEqual(Path(path).read_bytes(), contents)
            with self.assertRaises(FileExistsError):
                updater.stage(asset, directory)

    def test_bad_download_is_removed(self):
        release = {"tag_name": "v1.2.3", "assets": [self.asset()]}
        asset = updater.select(release, "1.2.2", "x86_64")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong content!!")):
                with self.assertRaises(ValueError):
                    updater.stage(asset, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_slow_download_is_bounded_and_never_staged(self):
        contents = b'verified qpkg'
        asset = updater.select({'tag_name':'v1.2.3', 'assets':[self.asset(contents)]}, '1.2.2', 'x86_64')
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(updater.urllib.request, 'urlopen', return_value=io.BytesIO(contents)), \
                 patch.object(updater.time, 'monotonic', side_effect=[0, 301]), self.assertRaises(ValueError):
                updater.stage(asset, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
