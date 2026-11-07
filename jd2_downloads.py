"""Read-only, bounded JD2 download snapshot safe for the Hub Web UI.

JD2's rich query response must never be passed to the browser directly:
it can contain download passwords, URLs, filesystem paths or future fields.
"""
import posixpath
import re

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


# v0.1.70: "the file does not exist on the hoster" in the download list. Conservative: JD2's definitive OFFLINE
# states, or a FAILED link whose status text says the file is gone. Never temporary errors, never "already exists"
# (FAILED_EXISTS, handled by the v0.1.56 sweep), never running or finished entries.
MISSING_TEXT = re.compile(r"file not found|file (?:does not|doesn't) exist|file (?:has been|was|is) (?:removed|deleted|no longer)"
                          r"|ファイルが(?:見つかりません|存在しません|削除)", re.I)


def is_missing_link(link):
    if not isinstance(link, dict) or link.get("running") is True or link.get("finished") is True:
        return False
    state = link.get("finalLinkState")
    if state == "FAILED_EXISTS":
        return False
    availability = link.get("availability")
    if state == "OFFLINE" or (isinstance(availability, str) and availability.upper() == "OFFLINE"):
        return True
    status = link.get("status")
    return state == "FAILED" and isinstance(status, str) and MISSING_TEXT.search(status) is not None


# ---- LinkGrabber (links JD2 holds but has not started) --------------------
LINKGRABBER_MAX = 50
# v0.1.70: availability (ONLINE / OFFLINE / UNKNOWN / TEMP_UNKNOWN) and enabled say why a link waits.
# Still no url / saveTo / password: only names, hosts and ids reach the Hub, and only names and ids the browser.
LINKGRABBER_FIELDS = {"name": True, "host": True, "bytesTotal": True, "uuid": True,
                      "packageUUID": True, "availability": True, "enabled": True}
# v0.1.70: names of the download list, to spot a link JD2 already has (name + host only).
DOWNLOAD_KEYS_MAX = 500
DOWNLOAD_KEY_FIELDS = {"name": True, "host": True}

WAITING_REASONS = {
    "online": "開始待ち（確認済み）",
    "offline": "オフライン（ファイルが消えている可能性）",
    "unknown": "確認中",
    "duplicate": "同じファイルがダウンロード一覧にあります",
    "disabled": "無効",
}
# Waiting links that will never start by themselves; the card can clear them in one tap.
WAITING_STUCK = ("offline", "duplicate")


def linkgrabber_query_options():
    return [dict(LINKGRABBER_FIELDS, maxResults=LINKGRABBER_MAX, startAt=0)]


def download_keys_query_options():
    return [dict(DOWNLOAD_KEY_FIELDS, maxResults=DOWNLOAD_KEYS_MAX, startAt=0)]


def _key_name(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def download_keys(raw):
    """{(name, host)} of the download list (casefolded). Anything that is not a list gives none."""
    keys = set()
    if isinstance(raw, list):
        for entry in raw[:DOWNLOAD_KEYS_MAX]:
            if isinstance(entry, dict) and _key_name(entry.get("name")):
                keys.add((_key_name(entry.get("name")), _key_name(entry.get("host"))))
    return keys


def _in_downloads(entry, keys):
    name, host = _key_name(entry.get("name")), _key_name(entry.get("host"))
    if not name or not keys:
        return False
    if (name, host) in keys:
        return True
    # one side without a host: the name alone decides
    return any(n == name and (not host or not h) for n, h in keys)


def waiting_state(entry, keys=frozenset()):
    """v0.1.70: why a LinkGrabber link waits: online / offline / unknown / duplicate / disabled.

    ONLINE and enabled means JD2 only has to move it (it normally does that itself with autostart);
    a missing or unknown availability counts as "unknown" so that nothing unchecked is ever started."""
    if entry.get("enabled") is False:
        return "disabled"
    availability = entry.get("availability")
    availability = availability.upper() if isinstance(availability, str) else ""
    if availability == "OFFLINE":
        return "offline"
    if _in_downloads(entry, keys):
        return "duplicate"
    if availability == "ONLINE":
        return "online"
    return "unknown"


def linkgrabber_snapshot(raw, keys=frozenset()):
    """Names and ids only; URLs, paths and passwords never reach the browser."""
    if not isinstance(raw, list) or len(raw) > LINKGRABBER_MAX:
        raise ValueError("JD2のLinkGrabber情報が上限を超えています")
    items = []
    stuck = 0
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("JD2のLinkGrabber情報の形式が不正です")
        state = waiting_state(entry, keys)
        stuck += state in WAITING_STUCK
        items.append({
            "id": _uuid(entry.get("uuid")),
            "package_id": _uuid(entry.get("packageUUID")),
            "name": _string(entry.get("name"), 240) or "ファイル（名称不明）",
            "host": _string(entry.get("host"), 120),
            "bytes_total": _nonnegative(entry.get("bytesTotal")),
            "state": state,
            "reason": WAITING_REASONS[state],
        })
    return {"items": items, "count": len(items), "has_more": len(items) == LINKGRABBER_MAX,
            "stuck": stuck}
