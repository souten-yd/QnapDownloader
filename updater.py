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
API_RC = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RELEASE_BYTES_MAX = 4 * 1024 * 1024
VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc([1-9]\d*))?$")
ARCH = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_BYTES = 512 * 1024 * 1024


def version(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError("version must be X.Y.Z or vX.Y.Z[-rcN]")
    return tuple(map(int, match.groups()[:3]))


def select(release, current, arch, include_rc=False):
    if not ARCH.fullmatch(arch):
        raise ValueError("invalid architecture")
    tag = release.get("tag_name", "")
    if not isinstance(tag, str):
        raise ValueError("invalid release tag")
    new = version(tag)
    # Experimental releases must be explicitly selected; only numbered
    # RC tags are accepted, never arbitrary beta/nightly build identifiers.
    is_rc = VERSION.fullmatch(tag).group(4) is not None
    if release.get("draft") or new <= version(current):
        return None
    if is_rc and (not include_rc or release.get("prerelease") is not True):
        return None
    if not is_rc and release.get("prerelease"):
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


def latest(current, arch, channel="stable"):
    """Select highest installable stable/RC package from trusted GitHub API."""
    if channel not in ("stable", "rc"):
        raise ValueError("unknown update channel")
    address = API if channel == "stable" else API_RC
    request = urllib.request.Request(address, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "QnapDownloader-updater/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read(RELEASE_BYTES_MAX + 1)
            if len(content) > RELEASE_BYTES_MAX:
                raise ValueError("GitHub release metadata exceeds size limit")
            releases = json.loads(content)
    except urllib.error.HTTPError as error:
        # Only /releases/latest returns 404 when there is no stable release.
        if channel == "stable" and error.code == 404:
            return None
        raise
    if channel == "stable":
        if not isinstance(releases, dict):
            raise ValueError("invalid stable GitHub release response")
        return select(releases, current, arch)
    if not isinstance(releases, list) or len(releases) > 30:
        raise ValueError("invalid GitHub release list")
    eligible = []
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("invalid GitHub release metadata")
        tag = release.get("tag_name")
        if not isinstance(tag, str) or VERSION.fullmatch(tag) is None:
            continue
        if release.get("draft") or version(tag) <= version(current):
            continue
        # Only stable and explicitly marked numbered RC prereleases.
        if release.get("prerelease") and VERSION.fullmatch(tag).group(4) is None:
            continue
        eligible.append(release)
    eligible.sort(key=lambda item: (
        version(item["tag_name"]),
        VERSION.fullmatch(item["tag_name"]).group(4) is None,
        int(VERSION.fullmatch(item["tag_name"]).group(4) or 0)), reverse=True)
    for release in eligible:
        asset = select(release, current, arch, include_rc=True)
        if asset is not None:
            return asset
    return None


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
    parser.add_argument("--channel", choices=("stable", "rc"), default="rc")
    parser.add_argument("--dest", help="existing private directory for verified downloads")
    args = parser.parse_args(argv)
    try:
        version(args.current_version)
        if args.action == "download" and not args.dest:
            parser.error("download requires --dest")
        asset = latest(args.current_version, args.arch, args.channel)
        if asset is None:
            print("No newer release for selected channel.")
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
