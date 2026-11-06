"""First-load timeline of a NAS-browser tab (v0.1.54).

One `Timeline` per tab open (restarted by a later navigation) records when each stage of "tap ->
page visible" happened. Server marks are milliseconds since the Hub received the request; the phone
adds its own marks (milliseconds since the tap) with POST /api/rb/firstload. Everything is RAM only.

Server stages (the Hub's clock):
  create   request -> NAS tab exists and the navigation was sent
  navigate navigation sent -> committed (DNS, connect, first byte, redirects: the NAS's network)
  parse    committed -> DOMContentLoaded (the page's own HTML/scripts)
  wait     DOMContentLoaded -> the phone asked for the first copy (round trip, display choice)
  capture  first copy / reader extraction (Chromium JS + Hub sanitizing)
  serve    copy ready -> the phone fetched the document
and `load` (committed -> load event) is shown for information only (ads can keep it open for ever).

Phone marks: created (tab request answered), choose (display decided), doc (copy document parsed), paint (above-the-fold images
loaded or a 1 s cap), full (copy fully loaded).

Thresholds and hints live here so the self-test and the log use the same rules.
"""
import time

STAGES = (
    # id, from mark, to mark, label, warn ms, fail ms
    ("create", "req", "target", "タブ作成", 1500, 6000),
    ("navigate", "target", "commit", "接続・遷移", 2500, 10000),
    ("parse", "commit", "dcl", "DOM構築", 3000, 12000),
    ("wait", "dcl", "cap0", "コピー開始待ち", 1000, 5000),
    ("capture", "cap0", "cap1", "コピー作成", 1500, 8000),
    ("serve", "cap1", "doc", "配信", 1500, 8000),
)
# The self-test drives the capture itself and has no phone attached: only these stages mean anything.
SELFTEST_STAGES = ("create", "navigate", "parse", "capture")
CLIENT_MARKS = ("created", "choose", "doc", "paint", "full")
CLIENT_LABELS = {"created": "作成応答", "choose": "表示決定", "doc": "文書", "paint": "初描画", "full": "全体"}
MARK_MAX_MS = 10 * 60 * 1000
PAINT_WARN_MS = 8000
PAINT_FAIL_MS = 20000

HINTS = {
    "create": "NASブラウザのタブ作成が遅いです。常駐をオンにする、予備タブ（設定›ブラウザ）をオンにする、NASの空きメモリ・CPU負荷を確認してください。",
    "navigate": "サイトへの接続が遅いです（NASの回線・DNS・サイト側の混雑）。リダイレクトが多いサイトでは長くなります。",
    "parse": "ページ自身の読み込みが遅いです。広告・外部スクリプトが多いサイトでは広告ブロックをオンにしてください。",
    "wait": "DOMContentLoadedから最初のコピー要求までが遅いです。端末とHubの間の通信が遅いか、端末が忙しい可能性があります。",
    "capture": "ページのコピー作成が遅いです。要素が多いページです。リーダー表示や高速表示が向いています。",
    "serve": "コピーの配信が遅いです。端末とHubの間の通信（Tailscaleの経路）を確認してください。",
}


def rate(ms, warn, fail):
    if ms is None:
        return "skip"
    if ms > fail:
        return "fail"
    return "warn" if ms > warn else "pass"


class Timeline:
    """Marks of one page open. Thread-safe enough for its use: every write is a single dict set."""

    def __init__(self, clock=time.monotonic, kind="mirror"):
        self._clock = clock
        self.t0 = clock()
        self.kind = kind
        self.marks = {"req": 0}
        self.client = {}
        self.capture_ms = {}      # kind ("mirror" / "reader") -> duration of its first capture / extraction
        self.blocked = 0          # requests the ad/tracker block list cancelled on this page
        self.reported = False     # the phone's marks were merged

    def mark(self, name, at=None):
        """Records `name` once (the first call wins). Returns True when this call recorded it."""
        if name in self.marks:
            return False
        now = self._clock() if at is None else at
        self.marks[name] = max(0, min(MARK_MAX_MS, int(round((now - self.t0) * 1000))))
        return True

    def has(self, name):
        return name in self.marks

    def set_client(self, values):
        """Merges the phone's marks (already validated numbers, milliseconds since the tap)."""
        for key in CLIENT_MARKS:
            value = values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MARK_MAX_MS:
                self.client[key] = int(round(value))
        self.reported = True

    def stage_ms(self, begin, end):
        a, b = self.marks.get(begin), self.marks.get(end)
        if a is None or b is None:
            return None
        return max(0, b - a)

    def breakdown(self, stages=None, capture_kind=None):
        """{stages: [{id,label,ms,status,warn_ms}], total_ms, slowest, load_ms, client, blocked, kind}.

        `capture_kind` picks which display's first capture the `capture` stage reports (the self-test asks for
        the mirror even when the reader was measured first)."""
        rows = []
        for sid, begin, end, label, warn, fail in STAGES:
            if stages is not None and sid not in stages:
                continue
            ms = self.stage_ms(begin, end)
            if sid == "capture" and capture_kind in self.capture_ms:
                ms = self.capture_ms[capture_kind]
            rows.append({"id": sid, "label": label, "ms": ms, "status": rate(ms, warn, fail), "warn_ms": warn})
        known = [r for r in rows if r["ms"] is not None]
        slowest = max(known, key=lambda r: r["ms"]) if known else None
        end_mark = self.marks.get("doc") or self.marks.get("cap1")
        return {
            "kind": self.kind,
            "stages": rows,
            "total_ms": end_mark,
            "slowest": slowest["id"] if slowest else None,
            "load_ms": self.stage_ms("commit", "load"),
            "marks": dict(self.marks),
            "client": dict(self.client),
            "blocked": self.blocked,
        }


def worst(rows):
    """The worst status of a breakdown's measured stages (pass < warn < fail); 'skip' when nothing was measured."""
    order = {"pass": 0, "warn": 1, "fail": 2}
    found = [r["status"] for r in rows if r["status"] in order]
    return max(found, key=lambda s: order[s]) if found else "skip"


def hint_for(stage_id):
    return HINTS.get(stage_id, "")


def line(data):
    """One compact Japanese line for the perf overlay and the self-test value."""
    parts = []
    for row in data.get("stages") or []:
        if row["ms"] is not None:
            parts.append("%s %.1fs" % (row["label"], row["ms"] / 1000.0))
    text = " · ".join(parts)
    client = data.get("client") or {}
    tail = [("%s %.1fs" % (CLIENT_LABELS[k], client[k] / 1000.0)) for k in CLIENT_MARKS if k in client]
    if tail:
        text += (" │ " if text else "") + "端末(タップから) " + " · ".join(tail)
    load_ms = data.get("load_ms")
    if load_ms is not None:
        text += " │ load %.1fs" % (load_ms / 1000.0)
    if data.get("blocked"):
        text += " │ 広告等ブロック %d件" % data["blocked"]
    return text or "計測中"


def debug_fields(data):
    """Flat numbers for the debug log (category render)."""
    out = {"kind": data.get("kind")}
    for row in data.get("stages") or []:
        out[row["id"]] = row["ms"]
    for key, value in (data.get("client") or {}).items():
        out["c_" + key] = value
    out["load"] = data.get("load_ms")
    out["blocked"] = data.get("blocked", 0)
    return out
