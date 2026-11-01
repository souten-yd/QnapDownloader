"""Pinned engine components (Java + JDownloader installer) and secure fetch.

Nothing here is bundled in the QPKG. The administrator-initiated setup
downloads the pinned official artifacts, verifies SHA-256 and size, and only
then places them in the persistent data area. See docs/engine-setup.md.

Stdlib only; Python 3.8 compatible. All network/IO is injectable for tests.
"""
import hashlib
import os
import posixpath
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request

JAVA_DIR_NAME = "jdk-21.0.13+9-jre"

COMPONENTS = {
    "java": {
        "label": "Java 21 JRE (Eclipse Temurin 21.0.13+9, x64 Linux)",
        "version": "21.0.13+9",
        "url": ("https://github.com/adoptium/temurin21-binaries/releases/download/"
                "jdk-21.0.13%2B9/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz"),
        "sha256": "a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd",
        "size": 52181297,
        "filename": "OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz",
        "dirname": JAVA_DIR_NAME,
    },
    "jd2": {
        "label": "JDownloader.jar (official installer)",
        "version": "installer",
        "url": "https://installer.jdownloader.org/JDownloader.jar",
        "sha256": "388a1d028e65cbc63933a778ddbc9267c14e7e7858389ce31a10be7308adeb40",
        "size": 5189769,
        "filename": "JDownloader.jar",
    },
}

MIN_FREE_BYTES = 400 * 1024 * 1024
CHUNK = 64 * 1024
MAX_TAR_MEMBERS = 20000
MAX_TAR_TOTAL = 600 * 1024 * 1024
MAX_REDIRECTS = 5

HASH_MISMATCH_MESSAGE = "公式の値と一致しません。QnapDownloaderの更新を待ってください"


class DownloadError(RuntimeError):
    pass


class ExtractError(RuntimeError):
    pass


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https URLs, with a small bounded count."""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname:
            raise DownloadError("HTTPS以外へのリダイレクトを拒否しました")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener():
    # Default TLS verification (system/proxy CA); never an unverified context.
    return urllib.request.build_opener(_HttpsOnlyRedirect())


def check_disk_free(path, need=MIN_FREE_BYTES, usage=shutil.disk_usage):
    free = usage(str(path)).free
    if free < need:
        raise DownloadError("空き容量が不足しています（%dMB以上必要）" % (need // (1024 * 1024)))
    return free


def file_sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _remove(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


def fetch_verified(component, download_dir, *, opener=None, timeout=30,
                   progress=None, cancelled=None):
    """Download one pinned component. Returns the verified file path.

    Streams into <download_dir>/<filename>.part while hashing, aborts when the
    byte count exceeds the pinned size, fsyncs, verifies sha256+size, then
    atomically renames to <download_dir>/<filename>.verified.
    Any failure removes the partial file and raises DownloadError.
    """
    url = component["url"]
    if urllib.parse.urlsplit(url).scheme != "https":
        raise DownloadError("HTTPS以外の取得元は使用できません")
    expected_size = component["size"]
    expected_hash = component["sha256"]
    os.makedirs(str(download_dir), mode=0o700, exist_ok=True)
    part = os.path.join(str(download_dir), component["filename"] + ".part")
    final = os.path.join(str(download_dir), component["filename"] + ".verified")
    _remove(part)
    opener = opener if opener is not None else default_opener()
    request = urllib.request.Request(url, headers={"User-Agent": "QnapDownloader-engine-setup"})
    digest = hashlib.sha256()
    total = 0
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except DownloadError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise DownloadError("取得に失敗しました: %s" % getattr(error, "reason", error)) from None
        with response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if urllib.parse.urlsplit(final_url).scheme != "https":
                raise DownloadError("HTTPS以外へのリダイレクトを拒否しました")
            length = None
            headers = getattr(response, "headers", None)
            if headers is not None and headers.get("Content-Length", "").isdigit():
                length = int(headers.get("Content-Length"))
            if length is not None and length > expected_size:
                raise DownloadError("想定サイズを超えるため中止しました")
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                while True:
                    if cancelled is not None and cancelled():
                        raise DownloadError("中止されました")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        raise DownloadError("想定サイズを超えるため中止しました")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(total, expected_size)
                output.flush()
                os.fsync(output.fileno())
        if total != expected_size:
            raise DownloadError("取得サイズが一致しません")
        if digest.hexdigest() != expected_hash:
            raise DownloadError(HASH_MISMATCH_MESSAGE)
        os.replace(part, final)
        return final
    except BaseException:
        _remove(part)
        raise


def _member_target(dest, name):
    if not name or "\x00" in name or name.startswith("/") or "\\" in name:
        raise ExtractError("不正なパスが含まれています: %r" % name[:80])
    if ".." in name.split("/"):
        raise ExtractError("不正なパスが含まれています: %r" % name[:80])
    norm = posixpath.normpath(name)
    parts = norm.split("/")
    if norm.startswith("/") or ".." in parts:
        raise ExtractError("不正なパスが含まれています: %r" % name[:80])
    if norm == ".":
        return None, ""
    return os.path.join(dest, *parts), norm


def _inside(root, path):
    return path == root or path.startswith(root + os.sep)


def safe_extract(archive, dest):
    """Extract a .tar.gz into an existing empty-ish dest with manual checks.

    Rejects absolute paths, '..', device/fifo members, links escaping dest,
    duplicates, writes through symlinked parents, and oversize archives.
    No tarfile 'filter' argument (Python 3.8 compatible).
    """
    os.makedirs(str(dest), mode=0o700, exist_ok=True)
    root = os.path.realpath(str(dest))
    total = 0
    count = 0
    try:
        tar = tarfile.open(str(archive), "r:gz")
    except (tarfile.TarError, OSError) as error:
        raise ExtractError("アーカイブを開けません: %s" % error) from None
    with tar:
        for member in tar:
            count += 1
            if count > MAX_TAR_MEMBERS:
                raise ExtractError("アーカイブのファイル数が多すぎます")
            if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                raise ExtractError("デバイスファイルを含むため中止しました")
            target, norm = _member_target(root, member.name)
            if target is None:
                continue
            parent = os.path.realpath(os.path.dirname(target))
            if not _inside(root, parent):
                raise ExtractError("展開先の外に書き込むため中止しました")
            if os.path.lexists(target) and not member.isdir():
                raise ExtractError("重複するパスがあるため中止しました")
            if member.isdir():
                os.makedirs(target, mode=0o755, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                link = member.linkname
                if not link or "\x00" in link or link.startswith("/") or "\\" in link:
                    raise ExtractError("不正なリンクが含まれています")
                if member.issym():
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(norm), link))
                else:
                    resolved = posixpath.normpath(link)
                if resolved.startswith("/") or resolved == ".." or resolved.startswith("../"):
                    raise ExtractError("展開先の外を指すリンクがあるため中止しました")
                if member.islnk():
                    source = os.path.join(root, *resolved.split("/"))
                    if (not _inside(root, os.path.realpath(source)) or
                            not os.path.isfile(source)):
                        raise ExtractError("不正なハードリンクが含まれています")
                    os.link(source, target)
                else:
                    os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                    os.symlink(link, target)
                continue
            if not member.isfile():
                raise ExtractError("未対応のファイル種別を含むため中止しました")
            total += member.size
            if total > MAX_TAR_TOTAL:
                raise ExtractError("展開サイズが大きすぎます")
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise ExtractError("ファイルを読み取れません")
            # Drop setuid/setgid/sticky; keep rwx bits only.
            mode = (member.mode & 0o755) | 0o600
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output, CHUNK)
            os.chmod(target, mode)
    return count
