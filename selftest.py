"""On-device self-test (v0.1.44): one button measures this NAS, the NAS browser and the phone, and
produces a report the user can paste back to the developer.

Everything is RAM only (never written to disk). One run at a time, at most HARD_TIMEOUT seconds. The server
part runs in a background thread and streams its progress through a revision long-poll; the phone runs
its own part while the server waits (POST /api/selftest/client) and the single report holds both.

The built-in pages are served to a dedicated NAS-browser tab out of the Hub's RAM
(remote_browser.SELFTEST_ORIGIN, Fetch.fulfillRequest): the self-test needs no network and the tab is never
reachable through the URL validation path. Real sites are optional, validated by the caller like the omnibox.

Every threshold and every hint lives in this file so that the server and the phone are judged by the same
rules (the phone only sends measurements).
"""
import json
import os
import re
import secrets
import struct
import threading
import time
import zlib
from collections import Counter, deque
from urllib.parse import parse_qs, urlsplit

import container_runtime as cr
import debuglog
import diskio
import rb_resident

HARD_TIMEOUT = 240.0           # seconds for the whole run
CLIENT_WAIT = 90.0             # how long the server waits for the phone's measurements
HISTORY_MAX = 5                # finished runs kept in RAM
MAX_URLS = 5
SITES_DEADLINE = 190.0         # no new real site is started after this many seconds
LOG_TAIL = 50
LOG_CATEGORIES = ("rb", "render", "engine", "http")
DEBUG_ENABLE = ("rb", "render", "engine", "http", "net")
STATUS_WAIT_MAX = 20.0
CLIENT_BODY_MAX = 32768        # bytes of the phone's POST (enforced by the HTTP handler)
DEFAULT_VIEWPORT = (390, 844, 3.0)
MIB = 1024 * 1024

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
STATUSES = (PASS, WARN, FAIL, SKIP)

SECTIONS = (("env", "環境"), ("browser", "NASブラウザ起動"), ("builtin", "内蔵テストページ"),
            ("client", "この端末（スマホ）"), ("sites", "実サイト"), ("cleanup", "後片付け"))
SECTION_LABEL = dict(SECTIONS)

# ---------------------------------------------------------------------------------------- thresholds
MEM_AVAILABLE_WARN_MIB = 2048
DISK_FREE_WARN_MIB = 1024
DISK_FREE_FAIL_MIB = 100
JD2_LATENCY_WARN_MS = 1000
COLD_START_WARN_MS = 15000
BROWSER_MEM_WARN_PERCENT = 70
PAGE_READY_WARN_MS = 3000
READER_WARN_MS = 1500
READER_MIN_BLOCKS = 8
READER_MIN_IMAGES = 2
MIRROR_WARN_MS = 2000
MIRROR_MIN_NODES = 20
MIRROR_BIG_KB = 1024
MENU_WAIT_S = 4.0
DYNAMIC_WAIT_S = 5.0
TILES_WARN_MS = 3000
TILE_WARN_MS = 2000
META_WARN_MS = 1000
FPS_WARN = 5.0
FPS_SECONDS = 3.0
SITE_OPEN_WARN_MS = 8000
SITE_FPS_SECONDS = 3.0
HUB_WRITE_WARN_KIB = 512
CLIENT_RTT_WARN_MS = 300
CLIENT_RTT_FAIL_MS = 2000
CLIENT_READER_WARN_MS = 1500
CLIENT_MIRROR_LOAD_WARN_MS = 4000
CLIENT_MORPH_WARN_MS = 300
CLIENT_TILE_WARN_MS = 4000

# -------------------------------------------------------------------------------------------- hints
# Concrete Japanese advice for every check that is not a pass (key: check id, optionally "id:variant").
HINTS = {
    "env.mem": "空きメモリが少ないです。使っていないコンテナやアプリを止めるか、NASブラウザのタブを減らしてください。",
    "env.disk": "データ領域の空きが少ないです。不要なダウンロードや古いファイルを整理してください。",
    "env.docker": "Container Stationを使えません。QTSでContainer Stationをインストールして起動してください。",
    "env.image": "設定›ブラウザで「初期設定」（NASブラウザのイメージ作成）を実行してください。",
    "env.jd2": "設定›JD2でエンジンを起動してください（JD2を使わない場合は無視して構いません）。",
    "env.jd2:slow": "JD2の応答が遅いです。ダウンロード中のCPU・ディスク負荷が高い可能性があります。",
    "env.guard": "空きメモリの保護でNASブラウザを開けません。メモリが空くまで待つか、他のアプリを止めてください。",
    "browser.cold": "常駐をオンにすると開くのが速くなります（設定›ブラウザ›常駐）。",
    "browser.start": "NASブラウザを起動できませんでした。設定›ブラウザの状態表示と、この結果のログを開発者へ送ってください。",
    "browser.memory": "NASブラウザのメモリ使用量が大きいです。タブを減らすか、常駐をオフにして使わないときは停止してください。",
    "builtin.ready": "内蔵ページの読み込みが遅いです。NASのCPU負荷（JD2のダウンロード・展開など）を確認してください。",
    "builtin.reader": "リーダー表示の本文抽出が期待どおりではありません。この結果を開発者へ送ってください。",
    "builtin.reader:slow": "リーダー抽出が遅いです。NASのCPU負荷が高いか、ページが重い可能性があります。",
    "builtin.reader_image": "リーダー表示の画像をNASブラウザ経由で取得できませんでした。この結果を開発者へ送ってください。",
    "builtin.mirror": "ミラー表示のコピーが期待どおりではありません。この結果を開発者へ送ってください。",
    "builtin.mirror:slow": "ミラーのコピーが遅いです。NASのCPU負荷を確認し、重いサイトでは高速表示やリーダー表示を使ってください。",
    "builtin.mirror:large": "このサイトはリーダー表示が向いています（ミラーのデータが大きいです）。",
    "builtin.mirror_safe": "ミラーの出力にスクリプトやイベント属性が残っています。安全上の問題なので、すぐ開発者へ報告してください。",
    "builtin.menu": "JSメニュー（href=\"#\"のトグル）の操作がNASのページに届かないか、ページが再読み込みされました。この結果を開発者へ送ってください。",
    "builtin.dynamic": "ページが後から書き換わる内容がミラーに反映されませんでした。この結果を開発者へ送ってください。",
    "builtin.tiles": "高速表示（タイル）の取得が遅いか失敗しました。NASのCPU負荷を確認してください。",
    "builtin.fps": "ライブ表示のフレームレートが低いです。画質を速度優先にするか、NASの負荷を確認してください。",
    "builtin.meta": "ページの変化の通知が遅いです。NASの負荷かネットワークを確認してください。",
    "client.storage": "この端末でlocalStorageが使えません（プライベートブラウズ等）。表示の記憶ができませんが動作はします。",
    "client.clipboard": "クリップボードへ直接コピーできません。結果のテキストを長押しして選択し、コピーしてください。",
    "client.rtt": "この端末からHubまでの通信が遅いです。Tailscaleの経路（直接接続かDERP中継か）やWi-Fiを確認してください。",
    "client.reader": "この端末でのリーダー表示の描画が期待どおりではありません。この結果を開発者へ送ってください。",
    "client.reader:slow": "この端末でのリーダー表示が遅いです。画像を減らす設定や高速表示を試してください。",
    "client.mirror_load": "ミラー表示のページをこの端末で読み込めませんでした。この結果を開発者へ送ってください。",
    "client.mirror_load:slow": "ミラー表示の読み込みが遅いです。Hubへの通信速度を確認してください。",
    "client.mirror_events": "この端末のブラウザでミラー内のタップ（pointer/click）がアプリに届きません。iPhone WebKitの既知の問題です。この結果を必ず開発者へ送ってください。",
    "client.mirror_events:click": "ミラー内のclickが届きません（pointerは届いています）。通常の操作には影響しませんが、開発者へ知らせてください。",
    "client.mirror_morph": "ミラーの差し替えが遅いです。重いページでは高速表示やリーダー表示が向いています。",
    "client.tiles": "高速表示のタイルの表示が遅いです。Hubへの通信速度を確認し、画質を速度優先にしてください。",
    "client.live": "ライブ表示の受信が遅いか届きません。画質を速度優先にしてください。ストリームが届かない場合は自動で静止画ポーリングに切り替わります。",
    "client.live:polling": "ライブ表示のストリーム（MJPEG）がこの端末で最初のフレームを表示できず、静止画ポーリングに切り替わります。この結果を開発者へ送ってください。",
    "client.error": "端末側のテストが途中で止まりました。この結果を開発者へ送ってください。",
    "client.missing": "端末側の結果が届きませんでした（画面を閉じた・通信が切れた等）。もう一度実行してください。",
    "site.open": "ページの読み込みが遅いです。NASの回線か、サイト側の混雑かもしれません。",
    "site.challenge": "Cloudflare等の確認ページです。ライブ表示で人間が通過する必要があります（自動では通しません）。",
    "site.reader": "このサイトのリーダー抽出が期待どおりではありません。ミラー表示や高速表示を使ってください。",
    "site.mirror": "このサイトのミラー表示が重い／失敗します。リーダー表示や高速表示が向いています。",
    "site.tiles": "このサイトの高速表示（タイル）の取得が遅いです。",
    "site.live": "このサイトのライブ表示のフレームレートが低いです。画質を速度優先にしてください。",
    "cleanup.tabs": "セルフテスト用のタブが閉じられていません。NASブラウザを停止して再起動してください。",
    "cleanup.disk": "テスト中にHubがディスクへ多く書き込みました。デバッグログやDBの書き込みを確認してください。",
    "run.error": "セルフテストが途中で例外になりました。この結果を開発者へ送ってください。",
    "run.timeout": "4分の上限に達したため途中で終了しました。NASの負荷が高い可能性があります。",
}

CLIENT_NAMES = {
    "client.storage": "localStorage",
    "client.clipboard": "クリップボード",
    "client.rtt": "Hubまでの往復時間",
    "client.reader": "リーダー表示の描画",
    "client.mirror_load": "ミラー表示の読み込み",
    "client.mirror_events": "ミラー内のタップ（pointer/click）",
    "client.mirror_morph": "ミラーの差し替え",
    "client.tiles": "高速表示（タイル）",
    "client.live": "ライブ表示の受信",
    "client.error": "端末側のエラー",
}
CLIENT_ID = re.compile(r"client\.[a-z_]{1,24}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{8,40}\Z")
NUM_KEYS = ("ms", "n", "kb", "fps")


class Busy(RuntimeError):
    """A run is already in progress."""


class Cancelled(Exception):
    pass


class RunTimeout(Exception):
    pass


# ------------------------------------------------------------------------------------- fixtures
def make_png(width, height, color):
    """Solid-colour PNG (pure python)."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError, TypeError):
        r = g = b = 0x88
    row = b"\x00" + bytes((r, g, b)) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(row * height, 6)) + chunk(b"IEND", b""))


_PARA = ("これはセルフテスト用の記事です。本文はリーダー表示とミラー表示の両方で確認されます。"
         "十分な長さの文章を含めることで、本文抽出が記事として認識できるようにしています。"
         "The quick brown fox jumps over the lazy dog.")
_HEAD = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>'
         '<style>body{font:16px/1.7 sans-serif;margin:0;color:#222;background:#fff}'
         'header{background:#124;color:#fff;padding:10px 14px}header a{color:#fff;margin-right:12px}'
         'main{padding:12px 14px}a{color:#0a58ca}p{margin:.8em 0}img{max-width:100%%;height:auto}%s</style></head><body>')
_BEACON = ('<script>fetch("/__beacon?e=load&p="+encodeURIComponent(location.pathname),'
           '{cache:"no-store"}).catch(function(){});</script>')


def _page(title, body, css="", script=""):
    return (_HEAD % (title, css)) + body + _BEACON + script + "</body></html>"


def _article_images():
    return "".join(
        '<img src="/i/800x400.png?c=%s" srcset="/i/400x200.png?c=%s 400w, /i/800x400.png?c=%s 800w" '
        'sizes="100vw" width="800" height="400" alt="画像%d">' % (c, c, c, i)
        for i, c in enumerate(("3366cc", "cc6633", "33aa66"), 1))


def _article_body(extra=""):
    imgs = _article_images().split("><")
    parts = ["<h1>セルフテスト記事</h1>", "<p>%s</p>" % _PARA, imgs[0] + ">", "<p>%s</p>" % _PARA,
             "<ul><li>項目その1</li><li>項目その2</li><li>項目その3</li></ul>",
             "<" + imgs[1] + ">", "<p>%s</p>" % _PARA,
             "<p>次のページ: <a href=\"/menu\">メニューのテスト</a></p>",
             "<" + imgs[2], "<p>%s</p>" % _PARA, "<p>%s</p>" % _PARA]
    return ('<header><a href="/article">ホーム</a><a href="/menu">メニュー</a></header>'
            '<main><article>%s%s</article></main>' % ("".join(parts), extra))


PAGES = {
    "/article": _page("セルフテスト記事", _article_body()),
    "/menu": _page(
        "セルフテスト メニュー",
        '<nav style="background:#124;padding:10px 14px"><a href="#" id="t" aria-expanded="false" '
        'aria-controls="dd" aria-haspopup="true" style="color:#fff">メニュー</a>'
        '<ul id="dd" hidden><li><a href="/article">メニュー項目 記事</a></li></ul></nav>'
        '<main><h1>JS メニューのテスト</h1><p>メニューのトグルは href="#" のリンクで、ページ内のスクリプトが開閉します。'
        'ここを押してもページは再読み込みされてはいけません。</p></main>',
        css="#dd[hidden]{display:none}",
        script='<script>var t=document.getElementById("t"),dd=document.getElementById("dd");'
               't.addEventListener("click",function(ev){ev.preventDefault();var o=dd.hasAttribute("hidden");'
               'if(o)dd.removeAttribute("hidden");else dd.setAttribute("hidden","");'
               't.setAttribute("aria-expanded",o?"true":"false");'
               'fetch("/__beacon?e=toggle&p="+(o?"open":"close"),{cache:"no-store"}).catch(function(){});});</script>'),
    "/dynamic": _page(
        "セルフテスト 動的ページ",
        "<main><h1>スクリプトで変わるページ</h1><p>読み込みの1秒後に行が追加されます。</p><div id=\"list\"></div></main>",
        script='<script>setTimeout(function(){var d=document.createElement("div");'
               'd.textContent="SELFTEST-LATE-ROW 読み込み後に追加された行";document.getElementById("list").appendChild(d);},1000);</script>'),
    "/long": _page(
        "セルフテスト 長いページ",
        "<main><h1>長いページ</h1>%s</main>" % "".join(
            "<p>%d. %s</p>" % (i, _PARA) for i in range(1, 91))),
    "/anim": _page(
        "セルフテスト アニメーション",
        _article_body('<div id="a" style="height:120px;margin-top:10px;color:#fff;font-size:24px">frame 0</div>'),
        script='<script>var n=0,e=document.getElementById("a");setInterval(function(){n++;'
               'e.style.background="hsl("+(n*23%360)+",70%,45%)";e.textContent="frame "+n;},40);</script>'),
}


class Fixtures:
    """The virtual site's answers plus the counters the checks read (page loads, toggles)."""

    def __init__(self):
        self.hits = Counter()
        self.beacons = Counter()
        self._lock = threading.Lock()

    def __call__(self, url):
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        path = parts.path or "/"
        query = parse_qs(parts.query)
        if path == "/__beacon":
            with self._lock:
                self.beacons[(query.get("e", [""])[0], query.get("p", [""])[0])] += 1
            return (204, "text/plain", b"")
        if path.startswith("/i/"):
            match = re.fullmatch(r"/i/([0-9]{1,4})x([0-9]{1,4})\.png", path)
            if not match:
                return None
            w, h = min(int(match.group(1)), 1200), min(int(match.group(2)), 1200)
            return (200, "image/png", make_png(w, h, query.get("c", ["888888"])[0]))
        page = PAGES.get(path)
        if page is None:
            return None
        with self._lock:
            self.hits[path] += 1
        return (200, "text/html; charset=utf-8", page.encode("utf-8"))

    def loads(self, path):
        with self._lock:
            return self.hits[path]

    def toggles(self, detail):
        with self._lock:
            return self.beacons[("toggle", detail)]


# --------------------------------------------------------------------------------------- helpers
def _num(value, low=-1e9, high=1e9):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and
            low <= value <= high)


def _short(text, limit=200):
    return debuglog.clean_text(text, limit)


def parse_cpu(text):
    """(model, logical cores) from the text of /proc/cpuinfo."""
    model, cores = "", 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "processor":
            cores += 1
        elif key == "model name" and not model:
            model = " ".join(value.split())
    return model, cores


def rate(value, warn=None, fail=None, low=False):
    """pass / warn / fail for a number against thresholds (low=True: smaller is worse)."""
    if not _num(value):
        return SKIP
    if low:
        if fail is not None and value < fail:
            return FAIL
        if warn is not None and value < warn:
            return WARN
        return PASS
    if fail is not None and value > fail:
        return FAIL
    if warn is not None and value > warn:
        return WARN
    return PASS


def sanitize_viewport(value):
    try:
        width, height, dpr = value.get("width"), value.get("height"), value.get("dpr")
    except AttributeError:
        return DEFAULT_VIEWPORT
    if (isinstance(width, int) and not isinstance(width, bool) and 320 <= width <= 1600 and
            isinstance(height, int) and not isinstance(height, bool) and 320 <= height <= 2400 and
            _num(dpr, 1.0, 3.0)):
        return width, height, float(dpr)
    return DEFAULT_VIEWPORT


# ----------------------------------------------------------------------------- client validation
def client_evaluate(item):
    """The check (id, name, status, value, threshold) for one validated measurement of the phone."""
    cid = item["id"]
    ms, n, kb, fps = (item.get(k) for k in NUM_KEYS)
    flags = item.get("flags") or {}
    note = item.get("note") or ""
    name = CLIENT_NAMES.get(cid, cid)
    status, value, threshold, variant = PASS, "", "", None
    if cid in ("client.storage", "client.clipboard"):
        ok = item.get("ok") is True
        status, value = (PASS if ok else WARN), ("使える" if ok else "使えません")
        threshold = "使えること"
    elif cid == "client.rtt":
        status = rate(ms, CLIENT_RTT_WARN_MS, CLIENT_RTT_FAIL_MS)
        value = "中央値 %s ms" % _fmt(ms)
        threshold = "<= %d ms" % CLIENT_RTT_WARN_MS
    elif cid == "client.reader":
        slow = rate(ms, CLIENT_READER_WARN_MS) == WARN
        if item.get("ok") is not True or not _num(n) or n < READER_MIN_BLOCKS:
            status = FAIL
        elif slow:
            status, variant = WARN, "slow"
        value = "%s ms / %s ブロック" % (_fmt(ms), _fmt(n))
        threshold = "ブロック >= %d、<= %d ms" % (READER_MIN_BLOCKS, CLIENT_READER_WARN_MS)
    elif cid == "client.mirror_load":
        if item.get("ok") is not True:
            status = FAIL
        elif rate(ms, CLIENT_MIRROR_LOAD_WARN_MS) == WARN:
            status, variant = WARN, "slow"
        value = "%s ms%s" % (_fmt(ms), "" if item.get("ok") is True else "（読み込めず）")
        threshold = "<= %d ms" % CLIENT_MIRROR_LOAD_WARN_MS
    elif cid == "client.mirror_events":
        pointer, click = flags.get("pointer") is True, flags.get("click") is True
        if not pointer:
            status = FAIL
        elif not click:
            status, variant = WARN, "click"
        value = "pointer=%s click=%s" % ("OK" if pointer else "届かず", "OK" if click else "届かず")
        threshold = "pointerとclickがアプリに届くこと"
    elif cid == "client.mirror_morph":
        status = FAIL if item.get("ok") is not True else rate(ms, CLIENT_MORPH_WARN_MS)
        value = "%s ms" % _fmt(ms)
        threshold = "<= %d ms" % CLIENT_MORPH_WARN_MS
    elif cid == "client.tiles":
        if item.get("ok") is not True:
            status = FAIL
        else:
            status = rate(ms, CLIENT_TILE_WARN_MS)
        value = "最初のタイル %s ms / %s 枚" % (_fmt(ms), _fmt(n))
        threshold = "<= %d ms" % CLIENT_TILE_WARN_MS
    elif cid == "client.live":
        img_first = flags.get("img") is True
        if not _num(fps) or fps <= 0:
            status = FAIL
        elif not img_first:
            status, variant = WARN, "polling"
        elif fps < FPS_WARN:
            status = WARN
        value = "%s fps / %s KB毎フレーム / ストリーム%s" % (
            _fmt(fps), _fmt(kb), "OK" if img_first else "NG（静止画ポーリングに切替）")
        threshold = ">= %g fps" % FPS_WARN
    else:
        status, value = FAIL, "エラー"
        threshold = ""
    if note:
        value = "%s（%s）" % (value, note) if value else note
    return {"id": cid, "name": name, "status": status, "value": value[:300], "threshold": threshold,
            "variant": variant}


def _fmt(value):
    if not _num(value):
        return "?"
    return ("%d" % value) if float(value).is_integer() else ("%.1f" % value)


def validate_client(payload):
    """Validated copy of the phone's POST body, or ValueError. Strings are cleaned (credentials, URLs)."""
    if not isinstance(payload, dict):
        raise ValueError("端末側の結果が不正です")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise ValueError("run_idが不正です")
    device_raw = payload.get("device")
    if not isinstance(device_raw, dict):
        raise ValueError("deviceが不正です")
    device = {}
    for key, limit in (("ua", 300), ("lang", 20), ("platform", 60)):
        value = device_raw.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("device.%sが不正です" % key)
            device[key] = _short(value, limit)
    for key, low, high in (("width", 0, 20000), ("height", 0, 20000), ("dpr", 0, 10), ("touch", 0, 100)):
        value = device_raw.get(key)
        if value is not None:
            if not _num(value, low, high):
                raise ValueError("device.%sが不正です" % key)
            device[key] = value
    for key in ("standalone", "storage", "clipboard"):
        value = device_raw.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise ValueError("device.%sが不正です" % key)
            device[key] = value
    flags_raw = device_raw.get("flags", {})
    if not isinstance(flags_raw, dict) or len(flags_raw) > 24:
        raise ValueError("device.flagsが不正です")
    flags = {}
    for key, value in flags_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", key) or not isinstance(value, (bool, str, int)):
            raise ValueError("device.flagsが不正です")
        flags[key] = value if isinstance(value, (bool, int)) else _short(value, 40)
    device["flags"] = flags
    checks_raw = payload.get("checks")
    if not isinstance(checks_raw, list) or len(checks_raw) > 24:
        raise ValueError("checksが不正です")
    checks, seen = [], set()
    for raw in checks_raw:
        if not isinstance(raw, dict):
            raise ValueError("checksの要素が不正です")
        cid = raw.get("id")
        if not isinstance(cid, str) or not CLIENT_ID.match(cid) or cid not in CLIENT_NAMES or cid in seen:
            raise ValueError("checksのidが不正です")
        seen.add(cid)
        item = {"id": cid}
        for key in NUM_KEYS:
            value = raw.get(key)
            if value is not None:
                if not _num(value, -1e7, 1e7):
                    raise ValueError("%s.%sが不正です" % (cid, key))
                item[key] = value
        if raw.get("ok") is not None:
            if not isinstance(raw["ok"], bool):
                raise ValueError("%s.okが不正です" % cid)
            item["ok"] = raw["ok"]
        sub = raw.get("flags")
        if sub is not None:
            if (not isinstance(sub, dict) or len(sub) > 8 or
                    any(not isinstance(k, str) or not re.fullmatch(r"[a-z_]{1,16}", k) or not isinstance(v, bool)
                        for k, v in sub.items())):
                raise ValueError("%s.flagsが不正です" % cid)
            item["flags"] = dict(sub)
        note = raw.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("%s.noteが不正です" % cid)
            item["note"] = _short(note, 160)
        checks.append(item)
    return {"run_id": run_id, "device": device, "checks": checks}


# -------------------------------------------------------------------------------------- the run
class Run:
    def __init__(self, run_id, options, t0, wall):
        self.id = run_id
        self.options = options
        self.t0 = t0
        self.started = wall
        self.state = "running"          # running | done | cancelled | timeout | error
        self.phase = "env"
        self.rev = 1
        self.checks = []
        self.sections = [{"id": i, "label": l, "status": "pending", "n": 0} for i, l in SECTIONS]
        self.tab = None
        self.device = None
        self.client = "pending"          # pending | received | none
        self.info = {}
        self.current = ""
        self.cancel = threading.Event()
        self.client_evt = threading.Event()
        self.elapsed_ms = None
        self.log = []
        self.debug_by_us = False
        self.debug_seq = 0
        self.io_before = {}

    def counts(self):
        out = {s: 0 for s in STATUSES}
        for check in self.checks:
            out[check["status"]] += 1
        return out


class SelfTest:
    def __init__(self, rb, *, engine=None, jd2=None, version="", data_dir=None, debug=None,
                 clock=time.monotonic, wall=time.time, sleep=None, meminfo=None,
                 cpuinfo="/proc/cpuinfo", statvfs=os.statvfs, hub_io=diskio.read_hub_io,
                 container_io=None, fixtures=None, thread=True):
        self.rb = rb
        self.engine = engine
        self.jd2 = jd2
        self.version = version
        self.data_dir = data_dir
        self.debug = debug if debug is not None else debuglog.LOG
        self._clock = clock
        self._wall = wall
        self._meminfo = meminfo or getattr(rb, "_meminfo", "/proc/meminfo")
        self._cpuinfo = cpuinfo
        self._statvfs = statvfs
        self._hub_io = hub_io
        self._container_io = container_io if container_io is not None else self._cgroup_io
        self._fixtures_factory = fixtures or Fixtures
        self._thread_on = thread
        self._cv = threading.Condition()
        self._run = None
        self._history = deque(maxlen=HISTORY_MAX)   # finished Run objects, newest last
        self._sleep = sleep

    # ----------------------------------------------------------------------------- public API
    def start(self, options=None):
        options = options or {}
        with self._cv:
            self._expire_locked()
            if self._run is not None and self._run.state == "running":
                raise Busy("セルフテストはすでに実行中です")
            urls = []
            for item in (options.get("urls") or [])[:MAX_URLS]:
                urls.append({"url": item["url"], "host": _host(item["url"])})
            viewport = sanitize_viewport(options.get("viewport")) if options.get("viewport") else DEFAULT_VIEWPORT
            run = Run(secrets.token_urlsafe(9), {"urls": urls, "viewport": viewport,
                                                 "client": options.get("client", True) is not False},
                      self._clock(), self._wall())
            self._run = run
            self._cv.notify_all()
        if self._thread_on:
            threading.Thread(target=self._main, args=(run,), daemon=True, name="selftest").start()
        else:
            self._main(run)
        return run.id

    def cancel(self):
        with self._cv:
            run = self._run
            if run is None or run.state != "running":
                return False
            run.cancel.set()
            run.client_evt.set()
            run.current = "中止しています…"
            self._bump(run)
        return True

    def status(self, since=None, wait=0.0):
        """Snapshot of the newest run; with `since` waits (long-poll) until its revision is newer."""
        wait = max(0.0, min(float(wait), STATUS_WAIT_MAX))
        end = self._clock() + wait
        with self._cv:
            while True:
                self._expire_locked()
                run = self._run
                rev = run.rev if run else 0
                if since is None or rev != since or wait <= 0:
                    break
                remaining = end - self._clock()
                if remaining <= 0:
                    break
                self._cv.wait(min(remaining, 1.0))
            return self._snapshot(run)

    def history(self):
        with self._cv:
            return [self._summary(run) for run in reversed(self._history)]

    def result(self, run_id):
        with self._cv:
            run = self._find(run_id)
            return None if run is None else self._snapshot(run, full=True)

    def report(self, run_id, include_urls=False):
        """{text, data} for a run (the newest when run_id is None)."""
        with self._cv:
            run = self._find(run_id)
            if run is None:
                return None
            return {"text": self._report_text(run, include_urls is True),
                    "data": self._snapshot(run, full=True, urls=include_urls is True)}

    def submit_client(self, payload):
        """The phone's measurements (validated by validate_client); only while the run waits for them."""
        data = validate_client(payload)
        with self._cv:
            self._expire_locked()
            run = self._run
            if run is None or run.id != data["run_id"] or run.state != "running":
                raise ValueError("このセルフテストは進行中ではありません")
            if run.phase != "client" or run.client != "pending":
                raise ValueError("端末側の結果を受け付けられる段階ではありません")
            run.device = data["device"]
            for item in data["checks"]:
                result = client_evaluate(item)
                self._add_locked(run, "client", result["id"], result["name"], result["status"],
                                 result["value"], result["threshold"], hint_key=result["id"],
                                 variant=result.get("variant"))
            run.client = "received"
            run.client_evt.set()
            self._bump(run)
        return {"ok": True, "checks": len(data["checks"])}

    # --------------------------------------------------------------------------- internals
    def _find(self, run_id):
        if run_id is None:
            return self._run
        if self._run is not None and self._run.id == run_id:
            return self._run
        for run in self._history:
            if run.id == run_id:
                return run
        return None

    def _expire_locked(self):
        run = self._run
        if run is not None and run.state == "running" and self._clock() - run.t0 > HARD_TIMEOUT + 15.0:
            # The worker is stuck in a blocking call: end the run for the viewer anyway.
            run.cancel.set()
            run.client_evt.set()
            self._finish_locked(run, "timeout")

    def _bump(self, run):
        run.rev += 1
        self._cv.notify_all()

    def _guard(self, run):
        if run.cancel.is_set():
            raise Cancelled()
        if self._clock() - run.t0 > HARD_TIMEOUT:
            raise RunTimeout()

    def _wait(self, run, seconds):
        end = self._clock() + seconds
        while True:
            self._guard(run)
            remaining = end - self._clock()
            if remaining <= 0:
                return
            if self._sleep is not None:
                self._sleep(min(remaining, 0.05))
            else:
                run.cancel.wait(min(remaining, 0.05))

    def _add(self, run, section, cid, name, status, value="", threshold="", hint_key=None, variant=None, extra=None):
        with self._cv:
            self._add_locked(run, section, cid, name, status, value, threshold, hint_key, variant, extra)

    def _add_locked(self, run, section, cid, name, status, value="", threshold="", hint_key=None, variant=None,
                    extra=None):
        hint = ""
        if status in (WARN, FAIL):
            key = hint_key or cid
            hint = HINTS.get("%s:%s" % (key, variant)) if variant else None
            hint = hint or HINTS.get(key, "")
        check = {"id": cid, "section": section, "name": name, "status": status,
                 "value": str(value)[:300], "threshold": str(threshold)[:120], "hint": hint}
        if extra:
            check.update(extra)
        run.checks.append(check)
        for sec in run.sections:
            if sec["id"] == section:
                sec["n"] += 1
        self._bump(run)

    def _enter(self, run, section, current=""):
        with self._cv:
            run.phase = section
            run.current = current or SECTION_LABEL.get(section, "")
            for sec in run.sections:
                if sec["id"] == section:
                    sec["status"] = "running"
                elif sec["status"] == "running":
                    sec["status"] = "done"
            self._bump(run)

    def _leave(self, run, section, skipped=False):
        with self._cv:
            for sec in run.sections:
                if sec["id"] == section:
                    sec["status"] = "skipped" if skipped else "done"
            self._bump(run)

    def _step(self, run, text):
        self._guard(run)
        with self._cv:
            run.current = text
            self._bump(run)

    def _finish_locked(self, run, state):
        if run.state != "running":
            return
        run.state = state
        run.phase = "finished"
        run.elapsed_ms = int((self._clock() - run.t0) * 1000)
        run.current = ""
        for sec in run.sections:
            if sec["status"] in ("running", "pending"):
                sec["status"] = "skipped" if sec["status"] == "pending" else "done"
        self._history.append(run)
        self._bump(run)

    # ------------------------------------------------------------------------------ the thread
    def _main(self, run):
        state = "done"
        fixtures = self._fixtures_factory()
        try:
            self._debug_on(run)
            run.io_before = self._io_totals()
            usable = self._section_env(run)
            if usable:
                self._section_browser(run, fixtures)
            else:
                self._add(run, "browser", "browser.skipped", "NASブラウザの確認", SKIP,
                          "NASブラウザを使えないため省略しました")
                self._leave(run, "browser", skipped=True)
        except Cancelled:
            state = "cancelled"
        except RunTimeout:
            state = "timeout"
            self._add(run, run.phase if run.phase in SECTION_LABEL else "cleanup", "run.timeout", "全体の時間",
                      FAIL, "%d 秒で打ち切り" % HARD_TIMEOUT, "<= %d 秒" % HARD_TIMEOUT)
        except Exception as error:      # a bug in a check must never leave the NAS browser tab behind
            state = "error"
            self._add(run, run.phase if run.phase in SECTION_LABEL else "cleanup", "run.error", "内部エラー", FAIL,
                      "%s: %s" % (type(error).__name__, _short(str(error), 120)))
        finally:
            try:
                self._section_cleanup(run, state)
            except Exception:
                pass
            self._debug_off(run)
            with self._cv:
                self._finish_locked(run, state)

    # -------------------------------------------------------------------- section 1: environment
    def _io_totals(self):
        out = {}
        try:
            out["hub"] = self._hub_io()[1]
        except Exception:
            pass
        for name in ("browser", "jd2"):
            try:
                value = self._container_io(name)
                if value is not None:
                    out[name] = value
            except Exception:
                pass
        return out

    def _cgroup_io(self, which):
        runtime = getattr(self.rb, "runtime", None)
        if runtime is None:
            return None
        name = cr.BROWSER_NAME if which == "browser" else cr.JD2_NAME
        paths = diskio.container_cgroup_paths(runtime, name)
        got = diskio.read_container_cgroup_io(paths) if paths else None
        return None if got is None else got[1]

    def _section_env(self, run):
        self._enter(run, "env", "環境を確認しています")
        rb = self.rb
        info = run.info
        info["version"] = self.version
        self._add(run, "env", "env.version", "Hubのバージョン", PASS, "v%s" % self.version)
        try:
            with open(self._cpuinfo, "r", encoding="utf-8", errors="replace") as handle:
                model, cores = parse_cpu(handle.read(1 << 20))
        except OSError:
            model, cores = "", os.cpu_count() or 0
        cores = cores or os.cpu_count() or 0
        info["cpu"] = _short(model or "不明", 80)
        info["cores"] = cores
        self._add(run, "env", "env.cpu", "CPU", PASS, "%s × %d コア" % (info["cpu"], cores))
        self._step(run, "メモリを確認しています")
        total = rb_resident.mem_total_kib(self._meminfo)
        avail = cr.read_mem_available_kib(self._meminfo)
        info["ram_total_mib"] = None if total is None else total // 1024
        info["ram_avail_mib"] = None if avail is None else avail // 1024
        if avail is None:
            self._add(run, "env", "env.mem", "NASのメモリ", SKIP, "取得できません")
        else:
            self._add(run, "env", "env.mem", "NASのメモリ（空き）",
                      rate(avail // 1024, MEM_AVAILABLE_WARN_MIB, low=True),
                      "空き %d MiB / 合計 %s MiB" % (avail // 1024, "?" if total is None else total // 1024),
                      ">= %d MiB" % MEM_AVAILABLE_WARN_MIB)
        free = None
        if self.data_dir:
            try:
                st = self._statvfs(str(self.data_dir))
                free = st.f_bavail * st.f_frsize // MIB
            except OSError:
                free = None
        if free is None:
            self._add(run, "env", "env.disk", "データ領域の空き", SKIP, "取得できません")
        else:
            self._add(run, "env", "env.disk", "データ領域の空き",
                      rate(free, DISK_FREE_WARN_MIB, DISK_FREE_FAIL_MIB, low=True),
                      "%d MiB" % free, ">= %d MiB" % DISK_FREE_WARN_MIB)
        self._step(run, "Container Stationを確認しています")
        try:
            status = rb.status()
        except Exception as error:
            status = {"container_station": {"available": False, "version": None}, "image_ready": False}
            info["status_error"] = _short(str(error), 80)
        station = (status.get("container_station") or {})
        info["container_station"] = station.get("version")
        self._add(run, "env", "env.docker", "Container Station", PASS if station.get("available") else FAIL,
                  "v%s" % station.get("version") if station.get("available") else "使えません", "利用できること")
        image_ready = bool(status.get("image_ready"))
        tag = None
        try:
            tag = rb.image_tag()
        except Exception:
            tag = None
        info["image"] = (tag.rsplit(":", 1)[-1][:12] if isinstance(tag, str) else None)
        self._add(run, "env", "env.image", "NASブラウザのイメージ", PASS if image_ready else FAIL,
                  ("あり（%s）" % info["image"]) if image_ready else "ありません", "作成済みであること")
        resident = status.get("resident") or {}
        info["resident"] = resident.get("on") is True
        self._add(run, "env", "env.resident", "常駐の設定", PASS, "オン" if info["resident"] else "オフ")
        self._step(run, "JD2エンジンを確認しています")
        self._engine_check(run)
        usable = bool(station.get("available")) and image_ready
        if usable:
            reason = None
            try:
                reason = rb.memory_reason()      # the user's own tabs at their limit do not matter here
            except Exception as error:
                reason = _short(str(error), 100)
            if reason:
                self._add(run, "env", "env.guard", "メモリの保護", FAIL, reason[:200], "NASブラウザを開けること")
                usable = False
        self._leave(run, "env")
        return usable

    def _engine_check(self, run):
        engine, jd2 = self.engine, self.jd2
        if engine is None:
            self._add(run, "env", "env.jd2", "JD2エンジン", SKIP, "確認できません")
            return
        try:
            es = engine.status()
        except Exception as error:
            self._add(run, "env", "env.jd2", "JD2エンジン", WARN, "状態を取得できません: %s" % _short(str(error), 80))
            return
        running = es.get("running") is True or es.get("ready") is True
        phase = es.get("phase") or "?"
        run.info["engine"] = "%s%s" % (phase, "（%s）" % es["mode"] if es.get("mode") else "")
        if not running:
            self._add(run, "env", "env.jd2", "JD2エンジン", WARN, "停止中（%s）" % phase, "起動していること")
            return
        started = self._clock()
        connected = False
        try:
            connected = (jd2.status() or {}).get("connected") is True if jd2 is not None else False
        except Exception:
            connected = False
        ms = int((self._clock() - started) * 1000)
        if not connected:
            self._add(run, "env", "env.jd2", "JD2エンジン（API）", WARN, "起動中ですがAPIに接続できません（%s）" % phase)
            return
        status = rate(ms, JD2_LATENCY_WARN_MS)
        self._add(run, "env", "env.jd2", "JD2エンジン（API）", status, "起動中・API応答 %d ms" % ms,
                  "<= %d ms" % JD2_LATENCY_WARN_MS, variant="slow")

    # ------------------------------------------------------------------ section 2: browser start
    def _mem_used_mib(self):
        try:
            value = self.rb.container_memory(fresh=True)
        except Exception:
            return None
        return None if not value else int(value["used_bytes"] // MIB)

    def _section_browser(self, run, fixtures):
        rb = self.rb
        self._enter(run, "browser", "NASブラウザを起動しています")
        try:
            was_running = rb.status().get("phase") == "running"
        except Exception:
            was_running = False
        before = self._mem_used_mib() if was_running else None
        width, height, dpr = run.options["viewport"]
        started = self._clock()
        try:
            created = rb.open_tab(self._origin() + "/article", selftest={"fixture": fixtures})
        except Exception as error:
            self._add(run, "browser", "browser.start", "NASブラウザの起動", FAIL, _short(str(error), 160),
                      "起動できること")
            self._leave(run, "browser")
            return
        tab = created["id"]
        opened_at = self._clock()
        opened_ms = int((opened_at - started) * 1000)
        if was_running:
            self._add(run, "browser", "browser.cold", "NASブラウザの起動時間", SKIP, "すでに起動していました")
        else:
            self._add(run, "browser", "browser.cold", "NASブラウザの起動時間（コールドスタート）",
                      rate(opened_ms, COLD_START_WARN_MS), "%d ms" % opened_ms, "<= %d ms" % COLD_START_WARN_MS)
        version = None
        try:
            version = rb.browser_version()
        except Exception:
            version = None
        run.info["chromium"] = version
        self._add(run, "browser", "browser.version", "Chromiumのバージョン", PASS if version else SKIP,
                  version or "取得できません")
        after = self._mem_used_mib()
        run.info["browser_mem_before_mib"], run.info["browser_mem_after_mib"] = before, after
        try:
            status = rb.status()
            percent = (status.get("memory") or {}).get("percent")
        except Exception:
            percent = None
        if after is None:
            self._add(run, "browser", "browser.memory", "NASブラウザのメモリ", SKIP, "取得できません")
        else:
            self._add(run, "browser", "browser.memory", "NASブラウザのメモリ（コンテナ）",
                      rate(percent, BROWSER_MEM_WARN_PERCENT) if _num(percent) else PASS,
                      "開始前 %s MiB → タブを開いた後 %d MiB%s" % (
                          "-" if before is None else before, after,
                          "（使用率 %.0f%%）" % percent if _num(percent) else ""),
                      "使用率 < %d%%" % BROWSER_MEM_WARN_PERCENT)
        self._leave(run, "browser")
        self._enter(run, "builtin", "内蔵テストページを測っています")
        run.tab = tab
        try:
            rb.set_viewport(tab, width, height, dpr)
        except Exception as error:
            self._add(run, "builtin", "builtin.viewport", "画面サイズの設定", WARN, _short(str(error), 120))
        self._builtin(run, tab, fixtures, opened_at)
        self._leave(run, "builtin")
        # the phone's part
        self._client_phase(run, tab)
        # real sites (the fixture tab is closed first: memory)
        self._close_quiet(tab)
        run.tab = None
        self._sites(run)

    @staticmethod
    def _origin():
        import remote_browser
        return remote_browser.SELFTEST_ORIGIN

    def _close_quiet(self, tab):
        try:
            self.rb.close_tab(tab)
        except Exception:
            pass

    # ----------------------------------------------------------------- section 3: built-in pages
    def _wait_loaded(self, run, tab, path, timeout=15.0, since=None):
        """Milliseconds (from `since`, default now) until the page `path` has finished loading, or None."""
        started = self._clock() if since is None else since
        limit = self._clock() + timeout
        url = self._origin() + path
        while self._clock() < limit:
            self._guard(run)
            info = self.rb.selftest_info(tab)
            if not info["loading"] and info["url"].split("#")[0] == url:
                return int((self._clock() - started) * 1000)
            self._wait(run, 0.05)
        return None

    def _goto(self, run, tab, path):
        self.rb.selftest_goto(tab, path)
        return self._wait_loaded(run, tab, path)

    def _builtin(self, run, tab, fixtures, opened_at):
        rb = self.rb
        sec = "builtin"
        # page ready (the first page was requested by open_tab)
        self._step(run, "内蔵の記事ページを読み込んでいます")
        ready = self._wait_loaded(run, tab, "/article", 20.0, since=opened_at)
        self._add(run, sec, "builtin.ready", "内蔵ページの読み込み", FAIL if ready is None else rate(ready, PAGE_READY_WARN_MS),
                  "読み込めず" if ready is None else "%d ms" % ready, "<= %d ms" % PAGE_READY_WARN_MS)
        if ready is None:
            return
        # reader
        self._step(run, "リーダー抽出を測っています")
        try:
            t = self._clock()
            data = rb.reader(tab)
            ms = int((self._clock() - t) * 1000)
            blocks = data.get("count") or len(data.get("blocks") or [])
            images = sum(1 for b in data.get("blocks") or [] if b.get("t") == "img")
            if not data.get("title") or blocks < READER_MIN_BLOCKS or images < READER_MIN_IMAGES:
                status, variant = FAIL, None
            elif ms > READER_WARN_MS:
                status, variant = WARN, "slow"
            else:
                status, variant = PASS, None
            self._add(run, sec, "builtin.reader", "リーダー抽出", status,
                      "%d ms / %d ブロック / 画像 %d" % (ms, blocks, images),
                      "ブロック >= %d、画像 >= %d、<= %d ms" % (READER_MIN_BLOCKS, READER_MIN_IMAGES, READER_WARN_MS),
                      variant=variant)
            if images:
                index = next((b["i"] for b in data["blocks"] if b.get("t") == "img"), 0)
                t = self._clock()
                try:
                    body, mime = rb.reader_image(tab, index)
                    self._add(run, sec, "builtin.reader_image", "リーダーの画像取得（NASブラウザ経由）",
                              PASS if body else FAIL, "%s %d バイト / %d ms" % (mime, len(body),
                                                                            int((self._clock() - t) * 1000)))
                except Exception as error:
                    self._add(run, sec, "builtin.reader_image", "リーダーの画像取得（NASブラウザ経由）", WARN,
                              _short(str(error), 120))
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.reader", "リーダー抽出", FAIL, _short(str(error), 160))
        # mirror capture + sanitization
        self._step(run, "ミラーのコピーを測っています")
        try:
            t = self._clock()
            cap = rb.mirror(tab, full=True, force=True)
            ms = int((self._clock() - t) * 1000)
            stats = cap.get("stats") or {}
            kb = (stats.get("html_bytes", 0) + stats.get("css_bytes", 0)) // 1024
            nodes = stats.get("nodes", 0)
            if nodes < MIRROR_MIN_NODES or cap.get("truncated"):
                status, variant = FAIL, None
            elif kb > MIRROR_BIG_KB:
                status, variant = WARN, "large"
            elif ms > MIRROR_WARN_MS:
                status, variant = WARN, "slow"
            else:
                status, variant = PASS, None
            self._add(run, sec, "builtin.mirror", "ミラーのコピー", status,
                      "%d ms / %d ノード / HTML %d KB + CSS %d KB / 画像 %s" % (
                          ms, nodes, stats.get("html_bytes", 0) // 1024, stats.get("css_bytes", 0) // 1024,
                          stats.get("images", "?")),
                      "ノード >= %d、<= %d ms、<= %d KB" % (MIRROR_MIN_NODES, MIRROR_WARN_MS, MIRROR_BIG_KB),
                      variant=variant)
            doc = rb.mirror_document(tab, cap["rev"])
            bad = unsafe_markup(doc)
            self._add(run, sec, "builtin.mirror_safe", "ミラー出力の安全性（script・on*属性なし）",
                      FAIL if bad else PASS, ("残っている: " + ", ".join(bad)) if bad else "問題なし",
                      "script・イベント属性・javascript: がないこと")
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.mirror", "ミラーのコピー", FAIL, _short(str(error), 160))
        # JS dropdown
        self._menu_check(run, tab, fixtures)
        # page that changes itself
        self._dynamic_check(run, tab)
        # tiles
        self._tiles_check(run, tab)
        # meta long-poll + screencast
        self._meta_and_fps(run, tab)

    def _menu_check(self, run, tab, fixtures):
        rb, sec = self.rb, "builtin"
        self._step(run, "JSメニューの操作を測っています")
        name = "JSメニューのトグル（再読み込みなし）"
        try:
            if self._goto(run, tab, "/menu") is None:
                raise RuntimeError("メニューのページを読み込めません")
            loads = fixtures.loads("/menu")
            cap = rb.mirror(tab, full=True, force=True)
            before = cap.get("html", "")
            m = find_toggle(before)
            if m is None:
                raise RuntimeError("コピーの中にメニューのトグルが見つかりません")
            if "メニュー項目" in before:
                raise RuntimeError("メニューが最初から開いています")
            rb.mirror_click(tab, m, 0.5, 0.5)
            end = self._clock() + MENU_WAIT_S
            opened = False
            while self._clock() < end:
                self._guard(run)
                if "メニュー項目" in rb.mirror(tab, full=True, force=True).get("html", ""):
                    opened = True
                    break
                self._wait(run, 0.25)
            reloaded = fixtures.loads("/menu") != loads
            toggled = fixtures.toggles("open") >= 1
            ok = opened and toggled and not reloaded
            value = "メニュー項目が出現=%s / ページ側スクリプト実行=%s / 再読み込み=%s" % (
                "はい" if opened else "いいえ", "はい" if toggled else "いいえ", "あり" if reloaded else "なし")
            self._add(run, sec, "builtin.menu", name, PASS if ok else FAIL, value, "開く・再読み込みなし")
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.menu", name, FAIL, _short(str(error), 160))

    def _dynamic_check(self, run, tab):
        rb, sec = self.rb, "builtin"
        self._step(run, "後から書き換わるページを測っています")
        name = "ページが後から変わる反映（ミラー）"
        try:
            started = self._clock()
            rb.selftest_goto(tab, "/dynamic")
            seen = None
            while self._clock() - started < DYNAMIC_WAIT_S + 1.0:
                self._guard(run)
                info = rb.selftest_info(tab)
                if not info["loading"] and info["url"].endswith("/dynamic"):
                    if "SELFTEST-LATE-ROW" in rb.mirror(tab, full=True, force=True).get("html", ""):
                        seen = int((self._clock() - started) * 1000)
                        break
                self._wait(run, 0.2)
            self._add(run, sec, "builtin.dynamic", name, PASS if seen is not None else FAIL,
                      ("%d ms で反映" % seen) if seen is not None else "%d 秒以内に反映されませんでした" % DYNAMIC_WAIT_S,
                      "追加行が反映されること")
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.dynamic", name, FAIL, _short(str(error), 160))

    def _tiles_check(self, run, tab):
        rb, sec = self.rb, "builtin"
        self._step(run, "高速表示（タイル）を測っています")
        try:
            if self._goto(run, tab, "/long") is None:
                raise RuntimeError("長いページを読み込めません")
            t = self._clock()
            snap = rb.snapshot(tab, force=True)
            ms = int((self._clock() - t) * 1000)
            tiles = snap.get("tiles") or 0
            status = FAIL if tiles < 2 else rate(ms, TILES_WARN_MS)
            self._add(run, sec, "builtin.tiles", "高速表示のスナップショット", status,
                      "%d ms / タイル %d 枚 / 高さ %s px" % (ms, tiles, snap.get("height", "?")),
                      "<= %d ms、タイル >= 2" % TILES_WARN_MS)
            t = self._clock()
            data = rb.tile(tab, snap["snap_id"], 0)
            tms = int((self._clock() - t) * 1000)
            self._add(run, sec, "builtin.tile", "タイル1枚の取得", FAIL if not data else rate(tms, TILE_WARN_MS),
                      "%d ms / %d KB" % (tms, len(data) // 1024), "<= %d ms" % TILE_WARN_MS,
                      hint_key="builtin.tiles")
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.tiles", "高速表示のスナップショット", FAIL, _short(str(error), 160))

    def _meta_and_fps(self, run, tab):
        rb, sec = self.rb, "builtin"
        self._step(run, "ライブ表示のフレームレートを測っています")
        try:
            rev = rb.selftest_info(tab)["rev"]
            box = {}

            def poll():
                try:
                    rb.page_meta(tab, rev=rev, wait=5.0)
                except Exception:
                    pass
                box["end"] = self._clock()
            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
            self._wait(run, 0.1)
            t = self._clock()
            rb.selftest_goto(tab, "/anim")
            worker.join(6.0)
            latency = None if "end" not in box else int((box["end"] - t) * 1000)
            self._add(run, sec, "builtin.meta", "メタ情報のロングポーリング応答",
                      SKIP if latency is None else rate(latency, META_WARN_MS),
                      "取得できません" if latency is None else "変化から %d ms" % latency, "<= %d ms" % META_WARN_MS)
            if self._wait_loaded(run, tab, "/anim") is None:
                raise RuntimeError("アニメーションのページを読み込めません")
            fps, kb, frames = self._measure_stream(run, tab, FPS_SECONDS)
            status = FAIL if frames == 0 else rate(fps, FPS_WARN, low=True)
            self._add(run, sec, "builtin.fps", "ライブ表示（スクリーンキャスト）", status,
                      "%.1f fps（%d フレーム / %g 秒）/ 1フレーム %d KB" % (fps, frames, FPS_SECONDS, kb),
                      ">= %g fps" % FPS_WARN)
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.fps", "ライブ表示（スクリーンキャスト）", FAIL, _short(str(error), 160))

    def _measure_stream(self, run, tab, seconds):
        """(fps, KB per frame, frames) of the live stream of `tab` over `seconds` (the Hub's own stream)."""
        counts = {"parts": 0, "bytes": 0}
        before = self.rb.selftest_info(tab)["seq"]

        def write(part):
            counts["parts"] += 1
            counts["bytes"] += len(part)
            if run.cancel.is_set():
                raise OSError("cancelled")
        started = self._clock()
        try:
            self.rb.stream(tab, write, max_seconds=seconds)
        except OSError:
            pass
        elapsed = max(self._clock() - started, 0.001)
        frames = counts["parts"]
        # a stream sends only fresh frames; seq shows how many the page really produced
        produced = max(0, self.rb.selftest_info(tab)["seq"] - before)
        frames = min(frames, produced) if produced else frames
        return frames / elapsed, (counts["bytes"] // max(counts["parts"], 1)) // 1024, frames

    # ---------------------------------------------------------------- section 4: the phone
    def _client_phase(self, run, tab):
        sec = "client"
        if not run.options["client"]:
            with self._cv:
                run.client = "none"
            self._add(run, sec, "client.missing", "端末側のテスト", SKIP, "この実行では端末側を行いません")
            self._leave(run, sec, skipped=True)
            return
        try:
            self.rb.selftest_goto(tab, "/anim")
            self._wait_loaded(run, tab, "/anim")
        except Exception:
            pass
        with self._cv:
            run.tab = tab
            run.phase = "client"
            run.current = "この端末で表示を測っています"
            for s in run.sections:
                if s["id"] == "client":
                    s["status"] = "running"
                elif s["status"] == "running":
                    s["status"] = "done"
            self._bump(run)
        end = self._clock() + CLIENT_WAIT
        while not run.client_evt.is_set():
            self._guard(run)
            if self._clock() >= end:
                break
            run.client_evt.wait(0.25)
        self._guard(run)
        with self._cv:
            received = run.client == "received"
            if not received:
                run.client = "none"
        if not received:
            self._add(run, sec, "client.missing", "端末側のテスト", WARN,
                      "%d 秒待ちましたが結果が届きませんでした" % CLIENT_WAIT, "結果が届くこと")
        self._leave(run, sec)

    # ----------------------------------------------------------------- section 5: real sites
    def _sites(self, run):
        urls = run.options["urls"]
        if not urls:
            self._leave(run, "sites", skipped=True)
            return
        self._enter(run, "sites", "実サイトを測っています")
        rb = self.rb
        width, height, dpr = run.options["viewport"]
        for index, item in enumerate(urls, 1):
            label = "実サイト%d（%s）" % (index, item["host"])
            self._guard(run)
            if self._clock() - run.t0 > SITES_DEADLINE:
                self._add(run, "sites", "site.%d.skipped" % index, label, SKIP, "時間の上限に近いため省略しました",
                          extra={"site": item["host"]})
                continue
            reason = None
            try:
                reason = rb.memory_reason()
            except Exception as error:
                reason = _short(str(error), 100)
            if reason:
                self._add(run, "sites", "site.%d.skipped" % index, label, SKIP, reason[:160],
                          extra={"site": item["host"]})
                continue
            self._site(run, index, item, label, width, height, dpr)
        self._leave(run, "sites")

    def _site(self, run, index, item, label, width, height, dpr):
        rb = self.rb
        sec = "sites"
        extra = {"site": item["host"], "_url": item["url"]}
        tab = None
        base = "site.%d." % index
        try:
            self._step(run, "%s を開いています" % item["host"])
            t = self._clock()
            try:
                created = rb.open_tab(item["url"], selftest={})
            except Exception as error:
                self._add(run, sec, base + "open", label + " 開く", SKIP, _short(str(error), 160), extra=extra,
                          hint_key="site.open")
                return
            tab = created["id"]
            try:
                rb.set_viewport(tab, width, height, dpr)
            except Exception:
                pass
            early = None
            loaded = None
            while self._clock() - t < 25.0:
                self._guard(run)
                info = rb.selftest_info(tab)
                elapsed = self._clock() - t
                if early is None and elapsed >= 3.0:
                    early = info["loading"]
                if not info["loading"] and info["url"] and info["url"] != "about:blank":
                    loaded = int(elapsed * 1000)
                    break
                self._wait(run, 0.1)
            if early is None:
                early = False
            meta = rb.page_meta(tab, wait=0)
            challenge = meta.get("challenge") is True
            value = ("読み込み完了 %d ms" % loaded) if loaded is not None else "25秒で読み込み未完了"
            value += "（3秒時点: %s）" % ("まだ読み込み中" if early else "読み込み済み")
            if challenge:
                self._add(run, sec, base + "open", label + " 開く", WARN, value + " / 確認ページ（チャレンジ）を検出",
                          "チャレンジなし", hint_key="site.challenge", extra=extra)
                return
            status = FAIL if loaded is None else rate(loaded, SITE_OPEN_WARN_MS)
            self._add(run, sec, base + "open", label + " 開く", status, value, "<= %d ms" % SITE_OPEN_WARN_MS,
                      hint_key="site.open", extra=extra)
            self._step(run, "%s のリーダー抽出を測っています" % item["host"])
            self._site_measure(run, base + "reader", label + " リーダー", "site.reader", extra,
                               lambda: self._m_reader(tab))
            self._site_measure(run, base + "mirror", label + " ミラー", "site.mirror", extra,
                               lambda: self._m_mirror(tab))
            self._site_measure(run, base + "tiles", label + " 高速表示", "site.tiles", extra,
                               lambda: self._m_tiles(tab))
            self._step(run, "%s のライブ表示を測っています" % item["host"])
            self._site_measure(run, base + "live", label + " ライブ", "site.live", extra,
                               lambda: self._m_live(run, tab))
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, base + "error", label, FAIL, _short(str(error), 160), extra=extra,
                      hint_key="run.error")
        finally:
            if tab is not None:
                self._close_quiet(tab)

    def _site_measure(self, run, cid, name, hint_key, extra, fn):
        try:
            status, value, threshold = fn()
            self._add(run, "sites", cid, name, status, value, threshold, hint_key=hint_key, extra=extra)
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, "sites", cid, name, FAIL, _short(str(error), 160), hint_key=hint_key, extra=extra)

    def _m_reader(self, tab):
        t = self._clock()
        data = self.rb.reader(tab)
        ms = int((self._clock() - t) * 1000)
        blocks = data.get("count") or len(data.get("blocks") or [])
        status = FAIL if blocks == 0 else rate(ms, READER_WARN_MS)
        return status, "%d ms / %d ブロック / 画像 %d / hosterリンク %d%s" % (
            ms, blocks, sum(1 for b in data.get("blocks") or [] if b.get("t") == "img"),
            data.get("hoster_links", 0), " / 途中まで" if data.get("truncated") else ""), \
            "ブロック > 0、<= %d ms" % READER_WARN_MS

    def _m_mirror(self, tab):
        t = self._clock()
        cap = self.rb.mirror(tab, full=False, force=True)
        ms = int((self._clock() - t) * 1000)
        stats = cap.get("stats") or {}
        kb = (stats.get("html_bytes", 0) + stats.get("css_bytes", 0)) // 1024
        if not stats.get("nodes"):
            status = FAIL
        elif cap.get("truncated") or kb > MIRROR_BIG_KB:
            status = WARN
        else:
            status = rate(ms, MIRROR_WARN_MS)
        return status, "%d ms / %d ノード / %d KB%s" % (ms, stats.get("nodes", 0), kb,
                                                        " / 途中まで" if cap.get("truncated") else ""), \
            "<= %d ms、<= %d KB" % (MIRROR_WARN_MS, MIRROR_BIG_KB)

    def _m_tiles(self, tab):
        t = self._clock()
        snap = self.rb.snapshot(tab, force=True)
        ms = int((self._clock() - t) * 1000)
        return (FAIL if not snap.get("tiles") else rate(ms, TILES_WARN_MS)), \
            "%d ms / タイル %s 枚" % (ms, snap.get("tiles", "?")), "<= %d ms" % TILES_WARN_MS

    def _m_live(self, run, tab):
        fps, kb, frames = self._measure_stream(run, tab, SITE_FPS_SECONDS)
        # a still page makes no frames: that is not a failure of the stream
        status = PASS if frames == 0 else rate(fps, FPS_WARN, low=True)
        return status, "%.1f fps（%d フレーム）/ %d KB" % (fps, frames, kb) + (
            " / ページが静止しているため更新なし" if frames == 0 else ""), ">= %g fps（動きがあるとき）" % FPS_WARN

    # ---------------------------------------------------------------- section 6: cleanup
    def _section_cleanup(self, run, state):
        self._enter(run, "cleanup", "後片付けをしています")
        rb = self.rb
        left = []
        try:
            for tab in rb.selftest_tabs():
                self._close_quiet(tab)
            left = rb.selftest_tabs()
        except Exception:
            left = []
        run.tab = None
        self._add(run, "cleanup", "cleanup.tabs", "セルフテスト用タブの後片付け", FAIL if left else PASS,
                  "閉じ残り %d" % len(left) if left else "すべて閉じました", "0枚")
        mem = self._mem_used_mib()
        run.info["browser_mem_end_mib"] = mem
        if mem is None:
            self._add(run, "cleanup", "cleanup.memory", "NASブラウザのメモリ（後片付け後）", SKIP,
                      "停止中または取得できません")
        else:
            self._add(run, "cleanup", "cleanup.memory", "NASブラウザのメモリ（後片付け後）", PASS, "%d MiB" % mem)
        after = self._io_totals()
        before = run.io_before
        parts, hub_kib = [], None
        for key, label in (("hub", "Hub"), ("browser", "NASブラウザ"), ("jd2", "JD2")):
            if key in before and key in after:
                delta = max(0, after[key] - before[key]) // 1024
                parts.append("%s +%d KiB" % (label, delta))
                if key == "hub":
                    hub_kib = delta
        if not parts:
            self._add(run, "cleanup", "cleanup.disk", "ディスクへの書き込み（この実行中）", SKIP, "取得できません")
        else:
            self._add(run, "cleanup", "cleanup.disk", "ディスクへの書き込み（この実行中）",
                      rate(hub_kib, HUB_WRITE_WARN_KIB) if hub_kib is not None else PASS,
                      " / ".join(parts), "Hub < %d KiB" % HUB_WRITE_WARN_KIB)
        self._leave(run, "cleanup")

    # ------------------------------------------------------------------------------ debug log
    def _debug_on(self, run):
        try:
            status = self.debug.status()
            run.debug_seq = status.get("seq", 0)
            if not status.get("enabled"):
                self.debug.enable("debug", list(DEBUG_ENABLE), duration=HARD_TIMEOUT + 60)
                run.debug_by_us = True
        except Exception:
            pass

    def _debug_off(self, run):
        try:
            data = self.debug.entries(run.debug_seq, 1000)
            lines = [format_log(e) for e in data.get("entries", []) if e.get("category") in LOG_CATEGORIES]
            run.log = lines[-LOG_TAIL:]
            if run.debug_by_us:
                self.debug.disable()
                self.debug.clear()
        except Exception:
            pass

    # -------------------------------------------------------------------------------- output
    def _summary(self, run):
        counts = run.counts()
        return {"id": run.id, "started": int(run.started), "state": run.state, "elapsed_ms": run.elapsed_ms,
                "pass": counts[PASS], "warn": counts[WARN], "fail": counts[FAIL], "skip": counts[SKIP],
                "version": self.version}

    def _snapshot(self, run, full=False, urls=False):
        if run is None:
            return {"rev": 0, "state": "idle", "phase": "", "run_id": None, "checks": [], "sections": [],
                    "history": [self._summary(r) for r in reversed(self._history)]}
        elapsed = run.elapsed_ms if run.elapsed_ms is not None else int((self._clock() - run.t0) * 1000)
        checks = []
        for check in run.checks:
            item = {k: v for k, v in check.items() if not k.startswith("_")}
            if urls and "_url" in check:
                item["url"] = check["_url"]
            checks.append(item)
        data = {"rev": run.rev, "run_id": run.id, "state": run.state, "phase": run.phase,
                "current": run.current, "elapsed_ms": elapsed, "started": int(run.started),
                "sections": [dict(s) for s in run.sections], "checks": checks,
                "summary": self._summary(run), "tab": run.tab if run.phase == "client" else None,
                "viewport": {"width": run.options["viewport"][0], "height": run.options["viewport"][1],
                             "dpr": run.options["viewport"][2]},
                "client": run.client, "history": [self._summary(r) for r in reversed(self._history)]}
        if full:
            data["info"] = dict(run.info)
            data["device"] = dict(run.device) if run.device else None
            data["log"] = list(run.log)
        return data

    def _report_text(self, run, include_urls):
        counts = run.counts()
        elapsed = (run.elapsed_ms if run.elapsed_ms is not None else int((self._clock() - run.t0) * 1000)) / 1000.0
        info, dev = run.info, run.device or {}
        lines = [
            "== QnapDownloader 自己テスト / Self-test report ==",
            "app: v%s | run: %s | state: %s | %s | %.1f s" % (
                self.version, run.id, run.state,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(run.started)), elapsed),
            "結果 / summary: 合格 %d / 注意 %d / 失敗 %d / 省略 %d  (PASS %d / WARN %d / FAIL %d / SKIP %d)" % (
                counts[PASS], counts[WARN], counts[FAIL], counts[SKIP],
                counts[PASS], counts[WARN], counts[FAIL], counts[SKIP]),
            "[NAS] cpu=%s x%s | ram total=%s MiB avail=%s MiB | container-station=%s | browser-image=%s | "
            "chromium=%s | resident=%s | engine=%s" % (
                info.get("cpu", "?"), info.get("cores", "?"), info.get("ram_total_mib", "?"),
                info.get("ram_avail_mib", "?"), info.get("container_station") or "?", info.get("image") or "?",
                info.get("chromium") or "?", "on" if info.get("resident") else "off", info.get("engine") or "?"),
        ]
        if dev:
            flags = dev.get("flags") or {}
            lines.append("[Phone] ua=%s | viewport=%sx%s dpr=%s | standalone=%s storage=%s clipboard=%s touch=%s | flags: %s" % (
                dev.get("ua", "?"), dev.get("width", "?"), dev.get("height", "?"), dev.get("dpr", "?"),
                dev.get("standalone", "?"), dev.get("storage", "?"), dev.get("clipboard", "?"), dev.get("touch", "?"),
                " ".join("%s=%s" % (k, ("1" if v is True else "0" if v is False else v)) for k, v in sorted(flags.items()))))
        else:
            lines.append("[Phone] (端末側の結果なし / no client result)")
        tag = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
        for sec_id, label in SECTIONS:
            rows = [c for c in run.checks if c["section"] == sec_id]
            if not rows:
                continue
            lines.append("")
            lines.append("## %s" % label)
            for c in rows:
                site = ""
                if c.get("site"):
                    site = " [%s]" % (c["_url"] if include_urls and c.get("_url") else c["site"])
                lines.append("[%s] %s%s | %s | value: %s | threshold: %s" % (
                    tag[c["status"]], c["id"], site, c["name"], c["value"], c["threshold"] or "-"))
                if c["hint"]:
                    lines.append("      改善のヒント: %s" % c["hint"])
        lines.append("")
        lines.append("## debug log (last %d lines: %s; times are UTC)" % (LOG_TAIL, "/".join(LOG_CATEGORIES)))
        lines.extend(run.log or ["(none)"])
        lines.append("== end ==")
        return "\n".join(lines)


def format_log(entry):
    data = entry.get("data")
    text = "%s %s/%s %s" % (time.strftime("%H:%M:%S", time.gmtime(entry.get("ts", 0))), entry.get("category"),
                            entry.get("level"), entry.get("message", ""))
    if data:
        text += " " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return _short(text, 300)


def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_SCRIPT = re.compile(r"<\s*script\b", re.I)
_HANDLER = re.compile(r"<[^>]*\son[a-z]+\s*=", re.I)
_JSURL = re.compile(r"javascript\s*:", re.I)


def unsafe_markup(text):
    """Names of the executable constructs found in a mirror document (empty = clean)."""
    found = []
    if _SCRIPT.search(text):
        found.append("<script>")
    if _HANDLER.search(text):
        found.append("on*属性")
    if _JSURL.search(text):
        found.append("javascript:")
    return found


_TOGGLE_TAG = re.compile(r"<a\b[^>]*>")


def find_toggle(html):
    """The mirror element number (data-m) of the first link that carries aria-expanded, or None."""
    for tag in _TOGGLE_TAG.findall(html or ""):
        if "aria-expanded" in tag:
            match = re.search(r'data-m="([0-9]{1,9})"', tag)
            if match:
                return int(match.group(1))
    return None
