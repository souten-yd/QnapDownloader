"""Select and verify QnapDownloader releases. Web installation uses self_update.py."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request


REPO = "souten-yd/QnapDownloader"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ARCH = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_BYTES = 512 * 1024 * 1024


def version(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError("version must be X.Y.Z or vX.Y.Z")
    return tuple(map(int, match.groups()))


def select(release, current, arch):
    if not ARCH.fullmatch(arch):
        raise ValueError("invalid architecture")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str):
        raise ValueError("invalid release tag")
    new = version(tag)
    if release.get("draft") or release.get("prerelease") or new <= version(current):
        return None
    name = f"QnapDownloader_{'.'.join(map(str, new))}_{arch}.qpkg"
    matching = [a for a in release.get("assets", []) if a.get("name") == name]
    if len(matching) != 1:
        raise ValueError("one matching QPKG asset is required")
    asset = matching[0]
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("release asset needs a SHA-256 digest")
    expected_url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
    if asset.get("browser_download_url") != expected_url:
        raise ValueError("unexpected asset URL")
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BYTES:
        raise ValueError("invalid asset size")
    return {"version": tag, "name": name, "url": expected_url,
            "sha256": digest[7:].lower(), "size": size}


def latest(current, arch):
    request = urllib.request.Request(API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "QnapDownloader-updater/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        # GitHub returns 404 until a repository has its first stable Release.
        # Other HTTP failures (rate limit, 5xx, permissions) remain visible.
        if error.code == 404:
            return None
        raise
    return select(release, current, arch)


def stage(asset, destination):
    directory = Path(destination)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("destination must be an existing, non-symlink directory")
    target = directory / asset["name"]
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    temp = None
    try:
        request = urllib.request.Request(asset["url"], headers={"User-Agent": "QnapDownloader-updater/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, tempfile.NamedTemporaryFile(
                mode="wb", prefix=".qnapdownloader-", dir=directory, delete=False) as out:
            temp = Path(out.name)
            digest = hashlib.sha256()
            count = 0
            deadline = time.monotonic() + 300
            while chunk := response.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise ValueError("download exceeded the five-minute time limit")
                count += len(chunk)
                if count > asset["size"] or count > MAX_BYTES:
                    raise ValueError("download exceeds release asset size")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if count != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError("release asset size or SHA-256 mismatch")
        os.link(temp, target)  # Fail if another process created the target meanwhile.
        return str(target)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "download"))
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--dest", help="existing private directory for verified downloads")
    args = parser.parse_args(argv)
    try:
        version(args.current_version)
        if args.action == "download" and not args.dest:
            parser.error("download requires --dest")
        asset = latest(args.current_version, args.arch)
        if asset is None:
            print("No newer stable release.")
        elif args.action == "check":
            print(json.dumps({"version": asset["version"], "asset": asset["name"]}))
        else:
            print(stage(asset, args.dest))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
