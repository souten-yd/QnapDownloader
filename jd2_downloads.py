"""Read-only, bounded JD2 download snapshot safe for the Hub Web UI.

JD2's rich query response must never be passed to the browser directly:
it can contain download passwords, URLs, filesystem paths or future fields.
"""
import posixpath

PAGE_SIZE = 30
MAX_PAGE = 20

LINK_FIELDS = {
    "name": True, "host": True, "bytesLoaded": True, "bytesTotal": True,
    "speed": True, "eta": True, "status": True, "running": True,
    "finished": True, "enabled": True, "packageUUID": True, "uuid": True
}
PACKAGE_FIELDS = {
    "name": True, "bytesLoaded": True, "bytesTotal": True,
    "speed": True, "eta": True, "status": True, "running": True,
    "finished": True, "childCount": True, "uuid": True,
    # saveTo is read to tell flat from subfolder; it never leaves the server
    # (only a flag and the folder name relative to the download folder do).
    "saveTo": True
}


def check_page(page):
    if type(page) is not int or not 0 <= page <= MAX_PAGE:
        raise ValueError("表示ページは0〜20の整数にしてください")
    return page


def query_options(fields, page):
    check_page(page)
    return [dict(fields, maxResults=PAGE_SIZE, startAt=page * PAGE_SIZE)]


def _string(value, limit=140):
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _nonnegative(value):
    if type(value) is not int or value < 0:
        return None
    return min(value, 2**53-1)


def _uuid(value):
    # JD2 UUIDs are 64-bit; hand them to the browser as strings (JS numbers lose precision).
    if type(value) is not int or value < 0:
        return None
    return str(value)


def _boolean(value):
    return value if type(value) is bool else None


def relative_folder(save_to, base):
    """Browser-safe view of where a package saves, relative to the download folder.

    Returns None when unknown, {"flat": True} when it saves directly into `base`,
    {"flat": False, "name": <relative name>} for a subfolder. Absolute paths and
    anything outside `base` are never returned.
    """
    if not isinstance(save_to, str) or not isinstance(base, str):
        return None
    if not save_to.startswith("/") or not base.startswith("/") or "\x00" in save_to:
        return None
    if ".." in save_to.split("/") or ".." in base.split("/"):
        return None
    path, folder = posixpath.normpath(save_to), posixpath.normpath(base)
    if folder == "/":
        return None
    if path == folder:
        return {"flat": True}
    if not path.startswith(folder + "/"):
        return None
    name = "".join(ch for ch in path[len(folder) + 1:] if ch.isprintable())
    return {"flat": False, "name": name[:120] or "（名称不明）"}


def _normalize(entry, is_package=False, base=None):
    if not isinstance(entry, dict):
        raise ValueError("JD2のダウンロード情報の形式が不正です")
    expected_name = "パッケージ" if is_package else "ファイル"
    name = _string(entry.get("name"), 240) or expected_name + "（名称不明）"
    loaded = _nonnegative(entry.get("bytesLoaded"))
    total = _nonnegative(entry.get("bytesTotal"))
    if loaded is not None and total is not None and total > 0:
        progress = min(100, round(loaded * 100 / total, 1))
    else:
        progress = None
    item = {
        "name": name,
        "status": _string(entry.get("status"), 120),
        "bytes_loaded": loaded,
        "bytes_total": total,
        "speed_bytes": _nonnegative(entry.get("speed")),
        "eta_seconds": _nonnegative(entry.get("eta")),
        "running": _boolean(entry.get("running")),
        "finished": _boolean(entry.get("finished")),
        "progress": progress,
    }
    if is_package:
        item["child_count"] = _nonnegative(entry.get("childCount"))
        item["id"] = _uuid(entry.get("uuid"))
        item["folder"] = relative_folder(entry.get("saveTo"), base)
    else:
        item["host"] = _string(entry.get("host"), 120)
        item["id"] = _uuid(entry.get("uuid"))
        item["package_id"] = _uuid(entry.get("packageUUID"))
        item["enabled"] = _boolean(entry.get("enabled"))
    return item


def snapshot(packages, links, page, base=None):
    check_page(page)
    for value in (packages, links):
        if not isinstance(value, list) or len(value) > PAGE_SIZE:
            raise ValueError("JD2のダウンロード情報が上限を超えています")
    return {
        "source": "local-readonly",
        "page": page,
        "page_size": PAGE_SIZE,
        "packages": [_normalize(entry, True, base) for entry in packages],
        "links": [_normalize(entry) for entry in links],
        "has_more": len(packages) == PAGE_SIZE or len(links) == PAGE_SIZE,
        "notice": "JD2実データの表示です。全体の開始・停止と、項目ごとの開始・停止・削除ができます",
    }


# ---- LinkGrabber (links JD2 holds but has not started) --------------------
LINKGRABBER_MAX = 50
LINKGRABBER_FIELDS = {"name": True, "host": True, "bytesTotal": True, "uuid": True,
                      "packageUUID": True}


def linkgrabber_query_options():
    return [dict(LINKGRABBER_FIELDS, maxResults=LINKGRABBER_MAX, startAt=0)]


def linkgrabber_snapshot(raw):
    """Names and ids only; URLs, paths and passwords never reach the browser."""
    if not isinstance(raw, list) or len(raw) > LINKGRABBER_MAX:
        raise ValueError("JD2のLinkGrabber情報が上限を超えています")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("JD2のLinkGrabber情報の形式が不正です")
        items.append({
            "id": _uuid(entry.get("uuid")),
            "package_id": _uuid(entry.get("packageUUID")),
            "name": _string(entry.get("name"), 240) or "ファイル（名称不明）",
            "host": _string(entry.get("host"), 120),
            "bytes_total": _nonnegative(entry.get("bytesTotal")),
        })
    return {"items": items, "count": len(items), "has_more": len(items) == LINKGRABBER_MAX}
