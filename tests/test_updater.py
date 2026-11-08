import hashlib
import io
import json
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

    def test_rc_channel_accepts_numbered_prerelease_with_verified_asset(self):
        for tag in ("v1.2.3-rc1", "v1.2.3-rc2"):
            asset={**self.asset(), "browser_download_url":
                f"https://github.com/{updater.REPO}/releases/download/{tag}/QnapDownloader_1.2.3_x86_64.qpkg"}
            release={"tag_name":tag, "prerelease":True, "assets":[asset]}
            self.assertIsNone(updater.select(release,"1.2.2","x86_64"))
            chosen=updater.select(release,"1.2.2","x86_64",include_rc=True)
            self.assertEqual(chosen["version"],tag)
            self.assertEqual(chosen["name"],"QnapDownloader_1.2.3_x86_64.qpkg")
            self.assertIsNone(updater.select(release,"1.2.3","x86_64",include_rc=True))
        for tag in ("v1.2.3-beta1", "v1.2.3-dev", "v1.2.3-rc0"):
            with self.assertRaises(ValueError):
                updater.version(tag)

    def test_rc_api_lists_releases_selects_highest_and_ignores_drafts(self):
        def release(tag, prerelease):
            name="QnapDownloader_"+tag.split("-")[0].removeprefix("v")+"_x86_64.qpkg"
            payload=b"candidate package"
            return {"tag_name":tag, "prerelease":prerelease,
              "assets":[{"name":name,"size":len(payload),
                 "digest":"sha256:"+hashlib.sha256(payload).hexdigest(),
                 "browser_download_url":
                   f"https://github.com/{updater.REPO}/releases/download/{tag}/{name}"}]}
        choices=[release("v1.2.5-rc1",True),release("v1.2.4",False),
                 {**release("v9.9.9-rc1",True),"draft":True},
                 release("v1.2.5-rc2",True)]
        with patch.object(updater.urllib.request,"urlopen",
                          return_value=io.BytesIO(json.dumps(choices).encode())) as call:
            candidate=updater.latest("1.2.3","x86_64","rc")
        self.assertEqual(candidate["version"],"v1.2.5-rc2")
        self.assertEqual(call.call_args.args[0].full_url, updater.API_RC)
        with patch.object(updater.urllib.request,"urlopen",
                          return_value=io.BytesIO(json.dumps(choices).encode())):
            self.assertIsNone(updater.latest("1.2.5","x86_64","rc"))

    def test_release_list_is_asked_again_after_a_github_5xx(self):
        # v0.1.75: a passing 504 from GitHub no longer fails the update check at once
        import urllib.error
        gateway = urllib.error.HTTPError(updater.API_RC, 504, "Gateway Time-out", None, None)
        waits = []
        with patch.object(updater, "_sleep", waits.append), \
                patch.object(updater.urllib.request, "urlopen",
                             side_effect=[gateway, io.BytesIO(b"[]")]) as call:
            self.assertIsNone(updater.latest("1.2.3", "x86_64", "rc"))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(waits, [updater.METADATA_RETRY_WAIT])
        with patch.object(updater, "_sleep", lambda s: None), \
                patch.object(updater.urllib.request, "urlopen", side_effect=[gateway] * 3) as call:
            with self.assertRaises(urllib.error.HTTPError):
                updater.latest("1.2.3", "x86_64", "rc")
        self.assertEqual(call.call_count, updater.METADATA_ATTEMPTS)
        forbidden = urllib.error.HTTPError(updater.API_RC, 403, "rate limited", None, None)
        with patch.object(updater, "_sleep", lambda s: None), \
                patch.object(updater.urllib.request, "urlopen", side_effect=[forbidden]) as call:
            with self.assertRaises(urllib.error.HTTPError):
                updater.latest("1.2.3", "x86_64", "rc")
        self.assertEqual(call.call_count, 1)                 # a 4xx is not retried

    def test_rc_channel_never_uses_stable_latest_or_ignores_integrity(self):
        asset={**self.asset(), "browser_download_url":
              f"https://github.com/{updater.REPO}/releases/download/v1.2.3-rc1/QnapDownloader_1.2.3_x86_64.qpkg",
              "digest":None}
        release={"tag_name":"v1.2.3-rc1","prerelease":True,"assets":[asset]}
        with patch.object(updater.urllib.request,"urlopen",
             return_value=io.BytesIO(json.dumps([release]).encode())):
            with self.assertRaisesRegex(ValueError,"SHA-256"):
                updater.latest("1.2.2","x86_64","rc")
        import urllib.error
        with patch.object(updater.urllib.request,"urlopen",
             side_effect=urllib.error.HTTPError(updater.API_RC,404,"Not found",None,None)):
            with self.assertRaises(urllib.error.HTTPError):
                updater.latest("1.2.2","x86_64","rc")
        with self.assertRaises(ValueError):
            updater.latest("1.2.2","x86_64","nightly")

    def test_no_release_is_not_an_installable_update(self):
        import urllib.error
        with patch.object(updater.urllib.request, "urlopen",
                          side_effect=urllib.error.HTTPError(updater.API, 404,
                           "No release", None, None)):
            self.assertIsNone(updater.latest("0.1.0", "x86_64"))
        for code in (403, 429, 500):
            with self.subTest(http_status=code):
                with patch.object(updater.urllib.request, "urlopen",
                                  side_effect=urllib.error.HTTPError(updater.API, code,
                                   "API failure", None, None)):
                    with self.assertRaises(urllib.error.HTTPError):
                        updater.latest("0.1.0", "x86_64")

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
