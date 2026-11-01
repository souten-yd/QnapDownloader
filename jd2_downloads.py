"""Read-only, bounded JD2 download snapshot safe for the Hub Web UI.

JD2's rich query response must never be passed to the browser directly:
it can contain download passwords, URLs, filesystem paths or future fields.
"""
PAGE_SIZE = 30
MAX_PAGE = 20

LINK_FIELDS = {
    "name": True, "host": True, "bytesLoaded": True, "bytesTotal": True,
    "speed": True, "eta": True, "status": True, "running": True,
    "finished": True, "enabled": True
}
PACKAGE_FIELDS = {
    "name": True, "bytesLoaded": True, "bytesTotal": True,
    "speed": True, "eta": True, "status": True, "running": True,
    "finished": True, "childCount": True
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


def _boolean(value):
    return value if type(value) is bool else None


def _normalize(entry, is_package=False):
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
    else:
        item["host"] = _string(entry.get("host"), 120)
        item["enabled"] = _boolean(entry.get("enabled"))
    return item


def snapshot(packages, links, page):
    check_page(page)
    for value in (packages, links):
        if not isinstance(value, list) or len(value) > PAGE_SIZE:
            raise ValueError("JD2のダウンロード情報が上限を超えています")
    return {
        "source": "local-readonly",
        "page": page,
        "page_size": PAGE_SIZE,
        "packages": [_normalize(entry, True) for entry in packages],
        "links": [_normalize(entry) for entry in links],
        "has_more": len(packages) == PAGE_SIZE or len(links) == PAGE_SIZE,
        "notice": "JD2実データの読取専用表示です。開始・停止・削除はまだ実装していません",
    }
