"""Premium account management on top of the Hub-managed JD2 engine (v0.1.23).

Two layers:

* Pure helpers that project JD2's account objects onto the public model
  (docs/premium-accounts-design.md section 4). JD2's response is NEVER passed on
  as it is: only the fixed fields below leave this module, the username is
  masked and free text is scrubbed. Unknown fields (stored passwords, cookies,
  tokens, ...) are dropped by construction.
* AccountService: validation, short RAM caches, the add/update rate limit and
  the readback after every change. Secrets (password) only live in local
  variables for the duration of one call and are never logged or cached.
"""
import datetime
import re
import threading
import time

MAX_ACCOUNTS = 200
MAX_HOSTS = 1000
LIST_TTL = 30.0
HOSTS_TTL = 600.0
RATE_LIMIT, RATE_WINDOW = 10, 600
PRIORITY_HOST = "rapidgator.net"
USERNAME_MAX, PASSWORD_MAX = 200, 500
SETTLE_ATTEMPTS, SETTLE_DELAY = 5, 1.0

# AccountQuery handed to accountsV2/listAccounts (only flags, no secrets).
LIST_QUERY = {"userName": True, "validUntil": True, "trafficLeft": True,
              "trafficMax": True, "enabled": True, "valid": True, "error": True,
              "maxResults": -1, "startAt": 0}

NOT_RUNNING = "JD2エンジンが起動していません。設定 › エンジン で起動してください"
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?$")
ID_RE = re.compile(r"^[0-9]{1,19}$")
MAX_VALID_UNTIL_MS = 253402300799000      # 9999-12-31
STATUSES = ("valid", "invalid", "expired", "temporarily_unavailable", "disabled", "unknown")
ACCOUNT_TYPES = ("premium", "free", "unknown")

_EMAIL = re.compile(r"[^\s@<>\"']{1,64}@[A-Za-z0-9.-]{1,255}")
_URL = re.compile(r"https?://\S+", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{24,}")


class ServiceError(Exception):
    """An error with an HTTP status and a message that is safe to show."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------ normalization

def _keep(text):
    # First 2 characters, but never more than half of a very short name.
    return text[:min(2, len(text) // 2)]


def mask_username(value):
    """First 2 characters + "••••" (+ the domain part of an e-mail address)."""
    if not isinstance(value, str) or not value:
        return None
    local, at, domain = value.partition("@")
    if at and local and domain:
        return _keep(local) + "••••@" + domain[:100]
    return _keep(value) + "••••"


def scrub_text(text, username=None, limit=120):
    """Short, single-line, secret-free text (JD2 error strings can echo input)."""
    if not isinstance(text, str):
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    if isinstance(username, str) and len(username) >= 3:
        text = text.replace(username, "••••")
    text = _URL.sub("[URL]", text)
    text = _EMAIL.sub("••••@••••", text)
    text = _TOKEN.sub("***", text)
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit - 1] + "…" if len(text) > limit else text


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _int(value):
    return value if type(value) is int else None


def _first(entry, *names):
    for name in names:
        if name in entry:
            return entry[name]
    return None


def iso_from_millis(value):
    value = _int(value)
    if value is None or not 0 < value < MAX_VALID_UNTIL_MS:
        return None
    moment = datetime.datetime.fromtimestamp(value / 1000.0, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(enabled, valid, kind, text):
    marker = ((kind if isinstance(kind, str) else "") + " " +
              (text if isinstance(text, str) else "")).upper()
    if enabled is False:
        return "disabled"
    if "EXPIRED" in marker:
        return "expired"
    if "TEMP" in marker:
        return "temporarily_unavailable"
    if "INVALID" in marker or valid is False:
        return "invalid"
    if valid is True and not (isinstance(kind, str) and kind.strip()):
        return "valid"
    return "unknown"


def _account_type(entry, valid_until_ms, valid, now_ms):
    declared = _first(entry, "accountType", "type")
    if isinstance(declared, str):
        lowered = declared.lower()
        if "premium" in lowered:
            return "premium"
        if "free" in lowered:
            return "free"
    if (_int(valid_until_ms) is not None and valid is not False and
            now_ms < valid_until_ms < MAX_VALID_UNTIL_MS):
        return "premium"      # a future expiry date only exists for paid accounts
    return "unknown"


def normalize_account(entry, now_ms=None):
    """One JD2 account object -> the public model, or None if it has no usable id/host."""
    if not isinstance(entry, dict):
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    raw_id = _first(entry, "UUID", "uuid", "id")
    if type(raw_id) is int and 0 <= raw_id < 2 ** 63:
        ident = str(raw_id)
    elif isinstance(raw_id, str) and ID_RE.fullmatch(raw_id):
        ident = raw_id
    else:
        return None
    host = _first(entry, "hostname", "host")
    host = host.strip().lower() if isinstance(host, str) else ""
    if not HOST_RE.fullmatch(host):
        return None
    username = _first(entry, "username", "userName")
    enabled = entry.get("enabled")
    enabled = enabled if type(enabled) is bool else None
    valid = entry.get("valid")
    valid = valid if type(valid) is bool else None
    kind = _first(entry, "errorType")
    text = _first(entry, "errorString", "error")
    valid_until = _int(entry.get("validUntil"))
    left, maximum = _int(entry.get("trafficLeft")), _int(entry.get("trafficMax"))
    status = _status(enabled, valid, kind, text)
    summary = None
    if status not in ("valid",):
        summary = scrub_text(text if isinstance(text, str) and text.strip() else kind, username)
    return {
        "id": ident,
        "host": host,
        "display_username": mask_username(username),
        "account_type": _account_type(entry, valid_until, valid, now_ms),
        "enabled": enabled,
        "status": status,
        "valid_until": iso_from_millis(valid_until),
        "traffic_left_bytes": left if left is not None and 0 <= left < 2 ** 53 else None,
        "traffic_max_bytes": maximum if maximum is not None and 0 <= maximum < 2 ** 53 else None,
        "traffic_unlimited": left == -1,
        "error_summary": summary,
    }


def normalize_accounts(raw, now_ms=None):
    """-> (items, truncated). Capped at MAX_ACCOUNTS."""
    if not isinstance(raw, list):
        raise ValueError("JD2のアカウント情報の形式が不正です")
    items, truncated = [], False
    for entry in raw:
        item = normalize_account(entry, now_ms)
        if item is None:
            continue
        if len(items) >= MAX_ACCOUNTS:
            truncated = True
            break
        items.append(item)
    return items, truncated


def normalize_hosts(raw):
    """Premium hoster names, rapidgator.net first, the rest alphabetical."""
    if not isinstance(raw, list):
        raise ValueError("JD2のホスト一覧の形式が不正です")
    found = set()
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip().lower()
            if HOST_RE.fullmatch(name):
                found.add(name)
    ordered = sorted(found - {PRIORITY_HOST})[:MAX_HOSTS - 1]
    return ([PRIORITY_HOST] if PRIORITY_HOST in found else []) + ordered


# --------------------------------------------------------------- validation

def check_text(value, label, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("%sは1〜%d文字で入力してください" % (label, maximum))
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or 0x80 <= code < 0xA0 or 0xD800 <= code <= 0xDFFF:
            raise ValueError("%sに使用できない文字（制御文字など）が含まれています" % label)
    if label == "ユーザー名" and not value.strip():
        raise ValueError("ユーザー名を入力してください")
    return value


def check_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or int(value) >= 2 ** 63:
        raise ValueError("アカウントIDが不正です")
    return value


def check_bool(value, label):
    if type(value) is not bool:
        raise ValueError(label + "はtrueまたはfalseで指定してください")
    return value


# ------------------------------------------------------------------ service

class AccountService:
    """Hub-side account operations; one instance per server (RAM caches only)."""

    def __init__(self, clock=time.time, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()          # caches and rate-limit hits
        self._write_lock = threading.Lock()    # one account change at a time
        self._accounts = None                  # (at, items, truncated)
        self._hosts = None                     # (at, items)
        self._hits = []

    # -- JD2 access (the controller lock serialises with every other JD2 call)
    def _call(self, jd2, name, *args):
        try:
            with jd2.lock:
                adapter = jd2.require_adapter()
                if getattr(adapter, "engine_running", False) is not True:
                    raise ServiceError(NOT_RUNNING, 503)
                return getattr(adapter, name)(*args)
        except ServiceError:
            raise
        except RuntimeError as exc:      # LocalUnavailable / PresetUnavailable: fixed texts
            message = str(exc)
        except ValueError:
            raise ServiceError("JD2のアカウント情報の形式が不正です", 502) from None
        except Exception:
            raise ServiceError("アカウント操作に失敗しました", 500) from None
        raise ServiceError(message, 503)

    def _fetch_accounts(self, jd2):
        try:
            items, truncated = normalize_accounts(self._call(jd2, "list_accounts"))
        except ValueError:
            raise ServiceError("JD2のアカウント情報の形式が不正です", 502) from None
        with self._lock:
            self._accounts = (self._clock(), items, truncated)
        return items, truncated

    def _invalidate(self):
        with self._lock:
            self._accounts = None

    def _rate_limit(self):
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits if now - t < RATE_WINDOW]
            if len(hits) >= RATE_LIMIT:
                self._hits = hits
                raise ServiceError("認証情報の送信回数が多すぎます（10分間に10回まで）。しばらく待ってください", 429)
            self._hits = hits + [now]

    # -- reads
    def list(self, jd2, refresh=False):
        now = self._clock()
        with self._lock:
            hit = self._accounts
        if hit and not refresh and now - hit[0] < LIST_TTL:
            return {"items": hit[1], "truncated": hit[2], "age_seconds": int(now - hit[0])}
        items, truncated = self._fetch_accounts(jd2)
        return {"items": items, "truncated": truncated, "age_seconds": 0}

    def hosts(self, jd2, refresh=False):
        now = self._clock()
        with self._lock:
            hit = self._hosts
        if hit and not refresh and now - hit[0] < HOSTS_TTL:
            return {"items": hit[1]}
        try:
            items = normalize_hosts(self._call(jd2, "list_premium_hosters"))
        except ValueError:
            raise ServiceError("JD2のホスト一覧の形式が不正です", 502) from None
        with self._lock:
            self._hosts = (self._clock(), items)
        return {"items": items}

    def schema(self, jd2):
        """Schema-only diagnostic: key names and JSON type names, never values."""
        raw = self._call(jd2, "list_accounts")
        hosters = self._call(jd2, "list_premium_hosters")
        try:
            kept = len(normalize_accounts(raw)[0])
        except ValueError:
            kept = 0
        samples = []
        for entry in raw[:3]:
            keys = sorted((str(key)[:60], json_type(value)) for key, value in entry.items())
            samples.append([{"key": key, "type": kind} for key, kind in keys[:100]])
        names = [h for h in hosters[:3] if isinstance(h, str) and HOST_RE.fullmatch(h.strip().lower())]
        return {"count": len(raw), "normalized": kept, "dropped": len(raw) - kept,
                "samples": samples,
                "hoster_count": len(hosters),
                "hoster_item_type": json_type(hosters[0]) if hosters else None,
                "hoster_first": [h.strip().lower() for h in names]}

    # -- helpers for writes
    def _find(self, jd2, ident):
        items, _ = self._fetch_accounts(jd2)
        for item in items:
            if item["id"] == ident:
                return item
        raise ServiceError("アカウントが見つかりません。一覧を更新してください", 404)

    def _settled(self, jd2, ident):
        """Read the account back; wait briefly while JD2 has not judged it yet."""
        item = None
        for attempt in range(SETTLE_ATTEMPTS):
            items, _ = self._fetch_accounts(jd2)
            item = next((entry for entry in items if entry["id"] == ident), None)
            if item is None or item["status"] != "unknown":
                break
            if attempt + 1 < SETTLE_ATTEMPTS:
                self._sleep(SETTLE_DELAY)
        return item

    def _refresh(self, jd2, ident):
        try:
            self._call(jd2, "refresh_accounts", [int(ident)])
            return True
        except ServiceError:
            return False

    # -- writes
    def add(self, jd2, body):
        host = body.get("host")
        username = check_text(body.get("username"), "ユーザー名", USERNAME_MAX)
        password = check_text(body.get("password"), "パスワード", PASSWORD_MAX)
        if not isinstance(host, str) or host.strip().lower() not in self.hosts(jd2)["items"]:
            raise ServiceError("対応していないホストです。一覧から選んでください", 400)
        host = host.strip().lower()
        self._rate_limit()
        try:
            with self._write_lock:
                before = {item["id"] for item in self._fetch_accounts(jd2)[0]}
                try:
                    self._call(jd2, "add_account", host, username, password)
                except ServiceError as exc:
                    self._invalidate()
                    raise ServiceError(str(exc) + "。追加された可能性があるため一覧を確認してください",
                                       exc.status) from None
                finally:
                    password = None
                items, _ = self._fetch_accounts(jd2)
                masked = mask_username(username)
                fresh = [item for item in items if item["host"] == host and item["id"] not in before]
                if not fresh:
                    # JD2 may merge an identical account: fall back to host + masked name.
                    fresh = [item for item in items if item["host"] == host and
                             item["display_username"] == masked]
                if len(fresh) != 1:
                    return {"item": None, "refreshed": False,
                            "message": "追加しました。一覧を更新して状態を確認してください"}
                ident = fresh[0]["id"]
                refreshed = self._refresh(jd2, ident)
                item = self._settled(jd2, ident) or fresh[0]
                return {"item": item, "refreshed": refreshed, "message": None}
        finally:
            username = password = None
            body.clear()

    def update(self, jd2, body):
        ident = check_id(body.get("id"))
        username = check_text(body.get("username"), "ユーザー名", USERNAME_MAX)
        password = check_text(body.get("password"), "パスワード", PASSWORD_MAX)
        self._rate_limit()
        try:
            with self._write_lock:
                self._find(jd2, ident)
                try:
                    self._call(jd2, "set_credentials", int(ident), username, password)
                finally:
                    password = None
                    self._invalidate()
                refreshed = self._refresh(jd2, ident)
                item = self._settled(jd2, ident)
                if item is None:
                    raise ServiceError("更新後にアカウントを確認できません。一覧を更新してください", 502)
                return {"item": item, "refreshed": refreshed}
        finally:
            username = password = None
            body.clear()

    def set_enabled(self, jd2, body):
        ident = check_id(body.get("id"))
        wanted = check_bool(body.get("enabled"), "enabled")
        with self._write_lock:
            self._find(jd2, ident)
            self._call(jd2, "enable_accounts" if wanted else "disable_accounts", [int(ident)])
            items, _ = self._fetch_accounts(jd2)
            item = next((entry for entry in items if entry["id"] == ident), None)
            if item is None or item["enabled"] is not wanted:
                raise ServiceError("JD2側で状態が変わったことを確認できません。一覧を更新してください", 502)
            return {"item": item}

    def refresh(self, jd2, body):
        ident = check_id(body.get("id"))
        with self._write_lock:
            self._find(jd2, ident)
            self._call(jd2, "refresh_accounts", [int(ident)])
            item = self._settled(jd2, ident)
            if item is None:
                raise ServiceError("再確認後にアカウントを確認できません。一覧を更新してください", 502)
            return {"item": item}

    def delete(self, jd2, body):
        ident = check_id(body.get("id"))
        confirm = body.get("confirm_host")
        with self._write_lock:
            item = self._find(jd2, ident)
            if not isinstance(confirm, str) or confirm.strip().lower() != item["host"]:
                raise ServiceError("確認用のホスト名が一致しません。アカウントは削除していません", 400)
            self._call(jd2, "remove_accounts", [int(ident)])
            items, _ = self._fetch_accounts(jd2)
            if any(entry["id"] == ident for entry in items):
                raise ServiceError("JD2から削除されたことを確認できません。一覧を更新してください", 502)
            return {"deleted": True, "id": ident}
