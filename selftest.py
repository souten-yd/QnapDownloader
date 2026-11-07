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
import hashlib
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
import rb_firstload
import rb_resident

HARD_TIMEOUT = 360.0           # seconds for the whole run (v0.1.65: +60 s for 「タブの容量」)
CLIENT_WAIT = 150.0             # how long the server waits for the phone's measurements
HISTORY_MAX = 5                # finished runs kept in RAM
MAX_URLS = 5
SITES_DEADLINE = 190.0         # no new real site is started after this many seconds
LOG_TAIL = 50
LOG_CATEGORIES = ("rb", "render", "engine", "http")
DEBUG_ENABLE = ("rb", "render", "engine", "http", "net")
STATUS_WAIT_MAX = 20.0
CLIENT_BODY_MAX = 32768        # bytes of the phone's POST (enforced by the HTTP handler)
SWIPE_BODY_MAX = 65536         # v0.1.52: bytes of one swipe-diagnostic POST (one per display mode)
SWIPE_WAIT = 75.0              # how long the server waits for the phone's swipe diagnostic
SWIPE_EVENTS_MAX = 80          # timeline events accepted per step
SWIPE_REPORT_LINES = 60        # timeline lines printed per non-passing step in the report
SWIPE_REPORT_PASS_LINES = 14   # ... and per passing step
SWIPE_REPORT_BYTES = 90000     # the swipe part of the report text is cut here
DEFAULT_VIEWPORT = (390, 844, 3.0)
MIB = 1024 * 1024
# v0.1.65: 「タブの容量」 (memory per open page and the recommended number of tabs)
CAPACITY_BUDGET_S = 60.0       # the whole section; pages that do not fit are left out (and said so)
CAPACITY_SETTLE_S = 1.5        # after DOMContentLoaded / load: idle time before the memory is read
CAPACITY_SITE_LOAD_S = 10.0    # a real site gets this long to reach DOMContentLoaded here
CAPACITY_MARGIN = 1.25         # safety factor on the per-page cost in the recommendation
CAPACITY_FLOOR_MIB = 30        # a page never costs less than this in the recommendation (measurement noise)
CAPACITY_PAGE_WARN_MIB = 300   # one page this big is worth a hint
CAPACITY_MAX_TABS = 20         # the largest setting of rb_max_tabs
# v0.1.65: 「リンク解析キュー」: this many virtual long presses are queued at once and must finish one after another
QUEUE_PROBES = 10
QUEUE_BUDGET_S = 45.0
CAPACITY_VIRTUAL = (("/article", "軽い仮想ページ"), ("/heavy?k=1", "重い仮想ページ1"),
                    ("/heavy?k=2", "重い仮想ページ2"), ("/heavy?k=3", "重い仮想ページ3"))

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
STATUSES = (PASS, WARN, FAIL, SKIP)

SECTIONS = (("env", "環境"), ("browser", "NASブラウザ起動"), ("builtin", "内蔵テストページ"),
            ("client", "この端末（スマホ）"), ("response", "操作の応答"), ("swipe", "スワイプ（自動）"), ("swipe_manual", "スワイプ（手動）"),
            ("sites", "実サイト"), ("capacity", "タブの容量"), ("queue", "リンク解析キュー"), ("pt", "パススルー"),
            ("cleanup", "後片付け"))
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
SITE_SCROLL_STEP_S = 0.3      # v0.1.46: the real-site live measurement scrolls the NAS page this often
SITE_SCROLL_PX = 200
SITE_LOAD_LIMIT_S = 25.0
SITE_LOAD_GRACE_S = 5.0        # after DOMContentLoaded: how long the full load event is awaited
LIVE_MJPEG_FIRST_S = 2.5       # shown to the user: MJPEG first frame, then SSE, then polling
LIVE_SSE_FIRST_S = 3.0
HUB_WRITE_WARN_KIB = 512
CLIENT_RTT_WARN_MS = 300
CLIENT_RTT_FAIL_MS = 2000
CLIENT_READER_WARN_MS = 1500
CLIENT_MIRROR_LOAD_WARN_MS = 4000
CLIENT_MORPH_WARN_MS = 300
CLIENT_TILE_WARN_MS = 4000
# v0.1.59: 「操作の応答」 (responsiveness of the app on this phone)
RESPONSE_IDS = ("client.route", "client.toggle", "client.idle", "client.open_rtt")
CLIENT_ROUTE_WARN_MS = 250        # hash change -> target view visible (all local: no request is needed)
CLIENT_ROUTE_FAIL_MS = 1500
CLIENT_TOGGLE_WARN_MS = 100       # tap on a settings switch -> the change is visible (optimistic, before the reply)
CLIENT_TOGGLE_FAIL_MS = 300
IDLE_STATE_PASS = 3               # /api/state answers in 30 s with nothing happening
IDLE_STATE_WARN = 6
IDLE_TOTAL_WARN = 15              # all /api requests in the same 30 s
OPEN_RTT_PASS = 3                 # requests between the tap and the first paint of a page (mirror)
OPEN_RTT_WARN = 5

# -------------------------------------------------------------------------------------------- hints
# Concrete Japanese advice for every check that is not a pass (key: check id, optionally "id:variant").
HINTS = {
    "env.mem": "空きメモリが少ないです。使っていないコンテナやアプリを止めるか、NASブラウザのタブを減らしてください。",
    "env.disk": "データ領域の空きが少ないです。不要なダウンロードや古いファイルを整理してください。",
    "env.docker": "Container Stationを使えません。QTSでContainer Stationをインストールして起動してください。",
    "env.image": "設定›ブラウザで「初期設定」（NASブラウザのイメージ作成）を実行してください。",
    "env.jd2": "設定›JD2でエンジンを起動してください（JD2を使わない場合は無視して構いません）。",
    "env.jd2:slow": "JD2の応答が遅いです。ダウンロード中のCPU・ディスク負荷が高い可能性があります。",
    "env.cgroup": "コンテナのメモリ・ディスクI/Oをcgroupから読めず、docker statsに頼っています（CPU負荷が大きくなります）。この結果（cgroupの行）を開発者へ送ってください。",
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
    "builtin.first_load": "初回読み込みのどこかが遅いです（内訳の遅い段階を見てください）。",
    "site.first_load": "このサイトの初回読み込みのどこかが遅いです（内訳の遅い段階を見てください）。",
    "builtin.menu": "JSメニュー（href=\"#\"のトグル）の操作がNASのページに届かないか、ページが再読み込みされました。この結果を開発者へ送ってください。",
    "builtin.dynamic": "ページが後から書き換わる内容がミラーに反映されませんでした。この結果を開発者へ送ってください。",
    "builtin.tiles": "高速表示（タイル）の取得が遅いか失敗しました。NASのCPU負荷を確認してください。",
    "builtin.fps": "ライブ表示のフレームレートが低いです。画質を速度優先にするか、NASの負荷を確認してください。",
    "builtin.meta": "ページの変化の通知が遅いです。NASの負荷かネットワークを確認してください。",
    "client.storage": "この端末でlocalStorageが使えません（プライベートブラウズ等）。表示の記憶ができませんが動作はします。",
    "client.clipboard": "クリップボードへ直接コピーできません。結果のテキストを長押しして選択し、コピーしてください。",
    "client.secure": "https（Tailscaleの https://…ts.net）で開くと、クリップボードや一部の機能が安定します。",
    "client.rtt": "この端末からHubまでの通信が遅いです。Tailscaleの経路（直接接続かDERP中継か）やWi-Fiを確認してください。",
    "client.reader": "この端末でのリーダー表示の描画が期待どおりではありません。この結果を開発者へ送ってください。",
    "client.reader:slow": "この端末でのリーダー表示が遅いです。画像を減らす設定や高速表示を試してください。",
    "client.mirror_load": "ミラー表示のページをこの端末で読み込めませんでした。この結果を開発者へ送ってください。",
    "client.mirror_load:slow": "ミラー表示の読み込みが遅いです。Hubへの通信速度を確認してください。",
    "client.mirror_events": "この端末のブラウザでミラー内のタップ（pointer/click）がアプリに届きません。iPhone WebKitの既知の問題です。この結果を必ず開発者へ送ってください。",
    "client.mirror_events:click": "ミラー内のclickが届きません（pointerは届いています）。通常の操作には影響しませんが、開発者へ知らせてください。",
    "client.mirror_morph": "ミラーの差し替えが遅いです。重いページでは高速表示やリーダー表示が向いています。",
    "client.tiles": "高速表示のタイルの表示が遅いです。Hubへの通信速度を確認し、画質を速度優先にしてください。",
    "client.live": "ライブ表示のフレームが届きません。経路（プロキシ）がストリームを溜め込んでいる可能性があります。この結果を開発者へ送ってください。",
    "client.live:polling": "ストリーム（MJPEG・SSE）がこの端末へ届かず、静止画ポーリング（5fps未満）で動いています。経由しているプロキシがストリームを溜め込んでいる可能性があります。画質を速度優先にし、この結果を開発者へ送ってください。",
    "client.route": "画面の切り替えが遅いです（通信なしで切り替わるはずです）。この端末の負荷（他のタブ・アプリ）を確認し、この結果を開発者へ送ってください。",
    "client.toggle": "スイッチを押してから見た目が変わるまでが遅いです。端末が忙しいか、画面の描き直しが重い可能性があります。この結果を開発者へ送ってください。",
    "client.idle": "何もしていないのにHubへの問い合わせが多いです（/api/state が30秒に3回を超えています）。NASブラウザのメモリ表示などが変化として扱われている可能性があります。デバッグログを有効にしてこの結果を開発者へ送ってください。",
    "client.open_rtt": "ページを開くまでの通信回数が多いです（3回以内が目標）。Hubが古い版のままか、先読みが効いていません。この結果を開発者へ送ってください。",
    "client.error": "端末側のテストが途中で止まりました。この結果を開発者へ送ってください。",
    "client.missing": "端末側の結果が届きませんでした（画面を閉じた・通信が切れた等）。もう一度実行してください。",
    "site.open": "ページの読み込み（DOMContentLoaded）が遅いか、完了しませんでした。NASの回線か、サイト側の混雑かもしれません。",
    "site.challenge": "Cloudflare等の確認ページです。ライブ表示で人間が通過する必要があります（自動では通しません）。",
    "site.reader": "このサイトのリーダー抽出が期待どおりではありません。ミラー表示や高速表示を使ってください。",
    "site.mirror": "このサイトのミラー表示が重い／失敗します。リーダー表示や高速表示が向いています。",
    "site.tiles": "このサイトの高速表示（タイル）の取得が遅いです。",
    "site.live": "スクロールで動きを作ってもライブ表示のフレームレートが低いです。画質を速度優先にしてください。",
    "queue.finish": "リンク解析が全件終わりませんでした。メモリ保護で待たされたか、NASブラウザが重い可能性があります。",
    "queue.sequential": "解析が同時に走っています。リンク解析は1件ずつ順に実行する設計です。",
    "capacity.page": "このページ1枚で大きくメモリを使います。重いサイトを何枚も開くとメモリ保護でタブを開けなくなります。",
    "capacity.recommend": "現在のタブ上限が推奨枚数より多いです。上限まで開くと、メモリ保護で新しいタブを開けなくなることがあります（設定›ブラウザの「推奨値にする」）。",
    "capacity.stopped": "メモリ保護のため途中までしか測れませんでした。推奨枚数は測れた範囲の値です。",
    "cleanup.tabs": "セルフテスト用のタブが閉じられていません。NASブラウザを停止して再起動してください。",
    "cleanup.disk": "テスト中にHubがディスクへ多く書き込みました。デバッグログやDBの書き込みを確認してください。",
    "swipe.missing": "端末側のスワイプ確認の結果が届きませんでした（画面を閉じた・通信が切れた等）。もう一度実行してください。",
    "swipe.display": "要求した表示に切り替わりませんでした（リーダー抽出に失敗すると高速表示になります）。そのモードの判定は参考値です。",
    "swipe.history": "表示の切り替えや戻る操作でブラウザの履歴が増えました。戻る操作が何度も必要になる原因です。この結果を開発者へ送ってください。",
    "run.error": "セルフテストが途中で例外になりました。この結果を開発者へ送ってください。",
    "run.timeout": "6分の上限に達したため途中で終了しました。NASの負荷が高い可能性があります。",
}

for _sid, _text in rb_firstload.HINTS.items():
    HINTS["builtin.first_load:" + _sid] = _text
    HINTS["site.first_load:" + _sid] = _text

CLIENT_NAMES = {
    "client.storage": "localStorage",
    "client.clipboard": "クリップボード",
    "client.secure": "安全なコンテキスト（https）",
    "client.rtt": "Hubまでの往復時間",
    "client.reader": "リーダー表示の描画",
    "client.mirror_load": "ミラー表示の読み込み",
    "client.mirror_events": "ミラー内のタップ（pointer/click）",
    "client.mirror_morph": "ミラーの差し替え",
    "client.tiles": "高速表示（タイル）",
    "client.live": "ライブ表示の受信",
    "client.route": "画面の切り替え時間",
    "client.toggle": "スイッチの反応（押してから見た目が変わるまで）",
    "client.idle": "待機中の通信量（30秒）",
    "client.open_rtt": "ページを開くまでの通信回数",
    "client.error": "端末側のエラー",
}
CLIENT_ID = re.compile(r"client\.[a-z_]{1,24}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{8,40}\Z")
NUM_KEYS = ("ms", "n", "kb", "fps", "fps_mjpeg", "fps_sse", "fps_poll", "state", "total")


def remote_browser_origin():
    import remote_browser
    return remote_browser.SELFTEST_ORIGIN


def recommend_tabs(per_tab_mib, base_mib, limit_mib, available_mib, open_tabs=0, limit_percent=80.0,
                   min_available_mib=2048, margin=CAPACITY_MARGIN, max_tabs=CAPACITY_MAX_TABS):
    """v0.1.65: how many tabs fit, from the measured cost of one page. None when there is no usable per-page cost.

    per_tab_mib   cost of one more page (a high value of the measured real pages, else the heavy virtual page)
    base_mib      container memory when the measurement started (the browser itself + the `open_tabs` user tabs)
    limit_mib     the container's memory limit (None: unknown, the container condition is left out)
    available_mib MemAvailable of the NAS when the measurement started (None: left out)
    The guard refuses a new tab at `limit_percent` of the container limit or when MemAvailable would drop below
    `min_available_mib`; both are turned into a number of pages of `per_tab_mib * margin`. Clamped to 1..max_tabs.
    Returns {"tabs", "by_container", "by_host", "per_tab_mib"} (the two limits are None when not computable).
    """
    if not isinstance(per_tab_mib, (int, float)) or per_tab_mib != per_tab_mib or per_tab_mib <= 0:
        return None
    cost = max(float(per_tab_mib), float(CAPACITY_FLOOR_MIB)) * margin
    by_container = by_host = None
    if isinstance(limit_mib, (int, float)) and limit_mib > 0 and isinstance(base_mib, (int, float)):
        browser_only = max(0.0, base_mib - open_tabs * per_tab_mib)       # the browser without its user tabs
        by_container = int(max(0.0, limit_mib * limit_percent / 100.0 - browser_only) // cost)
    if isinstance(available_mib, (int, float)):
        by_host = open_tabs + int(max(0.0, available_mib - min_available_mib) // cost)
    candidates = [v for v in (by_container, by_host) if v is not None]
    tabs = min(candidates) if candidates else max_tabs
    return {"tabs": max(1, min(max_tabs, tabs)), "by_container": by_container, "by_host": by_host,
            "per_tab_mib": round(per_tab_mib, 1)}


def page_costs(samples):
    """Per-page cost statistics from [(label, delta_mib, real)] in opening order. The first page also pays for the
    renderer process that starts with it, so it is shown but not averaged unless it is the only one.
    Returns {"avg", "max", "per_tab", "basis", "n"}; per_tab = the highest real-site cost, else the heaviest virtual
    page after the first (the first when nothing else was measured). All None without samples."""
    deltas = [(label, max(0.0, d), real) for label, d, real in samples if d is not None]
    if not deltas:
        return {"avg": None, "max": None, "per_tab": None, "basis": None, "n": 0}
    rest = deltas[1:] or deltas
    avg = sum(d for _, d, _ in rest) / len(rest)
    top = max(d for _, d, _ in rest)
    real = [d for _, d, r in rest if r]
    if real:
        per_tab, basis = max(real), "real"
    else:
        heavy = [d for label, d, _ in rest if "重い" in label] or [d for _, d, _ in rest]
        per_tab, basis = max(heavy), "virtual"
    return {"avg": avg, "max": top, "per_tab": per_tab, "basis": basis, "n": len(deltas)}


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


def _swipe_page(letter):
    """v0.1.52 swipe fixture: a huge label, article-like text (the reader accepts it) and a wide table."""
    rows = "".join("<tr>%s</tr>" % "".join("<td>%s%d-%d</td>" % (letter, r, c) for c in range(1, 13))
                   for r in range(1, 4))
    para = "<p>%s</p>" % _PARA
    return _page(
        "ページ %s" % letter,
        '<main><article><h1 style="font-size:64px;margin:.2em 0">ページ %s</h1>%s'
        '<div id="wide" style="overflow-x:auto;width:100%%;border:1px solid #888"><table style="width:1400px">%s</table></div>'
        '%s</article></main>' % (letter, para * 3, rows, para * 3))


def _heavy_page():
    """v0.1.65: a page that costs real memory like a busy site: ~3000 DOM nodes, 12 large images (decoded size
    ~2.4 MB each), a few hundred KB of script data and style rules. Static (no timers): only memory is measured."""
    rows = "".join('<li class="r%d"><span>項目%d</span> <a href="/article">リンク%d</a> <em>%s</em></li>' % (
        i % 7, i, i, "あいうえお" * 4) for i in range(1, 1201))
    images = "".join('<img src="/i/1000x600.png?c=%s" width="1000" height="600" alt="重い画像%d" style="width:100%%;height:auto">'
                     % (("3366cc", "cc6633", "33aa66", "aa3366", "6633cc", "33cccc")[i % 6], i) for i in range(12))
    css = "".join(".r%d{padding:%dpx;color:#%02x%02x%02x}" % (i, i % 5, i * 7 % 256, i * 13 % 256, i * 29 % 256)
                  for i in range(7))
    script = ('<script>var d=[];for(var i=0;i<60000;i++)d.push({i:i,s:"item"+i});window.__heavy=d;'
              'var ul=document.getElementById("extra");for(var j=0;j<600;j++){var li=document.createElement("li");'
              'li.textContent="動的な項目 "+j;ul.appendChild(li);}</script>')
    return _page("セルフテスト 重いページ",
                 "<main><h1>重いページ</h1><ul>%s</ul>%s<ul id=\"extra\"></ul></main>" % (rows, images),
                 css=css, script=script)


SWIPE_LETTERS = ("A", "B", "C")
SWIPE_CHALLENGE_PATH = "/cdn-cgi/challenge-platform/h/b/swipe"
SWIPE_ROUTE = ("/swipe/a", SWIPE_CHALLENGE_PATH, "/swipe/a?__cf_chl_tk=selftest", "/swipe/b", "/swipe/c")
PAGES = {
    SWIPE_CHALLENGE_PATH: _page(
        "しばらくお待ちください…",
        '<main><h1>安全性を確認しています</h1><p>これはセルフテスト用の偽の確認ページです（履歴に確認ページが残る状況の再現）。</p></main>'),
    "/swipe/a": _swipe_page("A"),
    "/swipe/b": _swipe_page("B"),
    "/swipe/c": _swipe_page("C"),
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
    "/heavy": _heavy_page(),
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
        match = re.fullmatch(r"/go/([0-9]{1,3})", path)
        if match:
            # v0.1.65: a virtual page that forwards to a (fake) Rapidgator file page, like a link-protection site does
            digest = hashlib.md5(("selftest-%s" % match.group(1)).encode("ascii")).hexdigest()
            page = _page("リンク先 %s" % match.group(1), "<main><p>転送しています…</p></main>",
                         script='<script>setTimeout(function(){location.replace("https://rapidgator.net/file/%s");},150);</script>' % digest)
            with self._lock:
                self.hits["/go"] += 1
            return (200, "text/html; charset=utf-8", page.encode("utf-8"))
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
    ms, n, kb, fps = (item.get(k) for k in NUM_KEYS[:4])
    flags = item.get("flags") or {}
    note = item.get("note") or ""
    name = CLIENT_NAMES.get(cid, cid)
    status, value, threshold, variant = PASS, "", "", None
    if cid in ("client.storage", "client.clipboard", "client.secure"):
        ok = item.get("ok") is True
        status, value = (PASS if ok else WARN), ("使える" if ok else "使えません")
        threshold = "使えること"
        if cid == "client.clipboard":
            value = ("使える（%s）" % ("clipboard API" if flags.get("api") is True else "copyコマンド")) if ok else "使えません"
        elif cid == "client.secure":
            value = "はい（https）" if ok else "いいえ（httpのため）"
            threshold = "secure context であること"
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
        transport = live_transport(flags)
        label = TRANSPORT_LABEL.get(transport, "なし")
        if transport is None or not _num(fps) or fps <= 0:
            status = FAIL
        elif transport == "poll" and fps < FPS_WARN:
            status, variant = WARN, "polling"
        parts = []
        for key, name_, fkey in (("mjpeg", "MJPEG", "fps_mjpeg"), ("sse", "SSE", "fps_sse"), ("poll", "ポーリング", "fps_poll")):
            figure = item.get(fkey)
            if flags.get(key) is True:
                parts.append("%s %s fps" % (name_, _fmt(figure)) if _num(figure) else "%s OK" % name_)
            elif key in flags or _num(figure):
                parts.append("%s 届かず" % name_ if not _num(figure) or figure <= 0 else "%s %s fps" % (name_, _fmt(figure)))
        value = "経路: %s / %s fps / %s KB毎フレーム（%s）" % (label, _fmt(fps), _fmt(kb), "、".join(parts) or "測定なし")
        threshold = "ストリーム(MJPEG/SSE)が届くこと。ポーリングなら >= %g fps" % FPS_WARN
    elif cid == "client.route":
        status = FAIL if item.get("ok") is not True else rate(ms, CLIENT_ROUTE_WARN_MS, CLIENT_ROUTE_FAIL_MS)
        value = "最も遅い %s ms（%s 画面）" % (_fmt(ms), _fmt(n))
        threshold = "<= %d ms" % CLIENT_ROUTE_WARN_MS
    elif cid == "client.toggle":
        status = FAIL if item.get("ok") is not True else rate(ms, CLIENT_TOGGLE_WARN_MS, CLIENT_TOGGLE_FAIL_MS)
        value = "見た目が変わるまで %s ms / Hubの確定まで %s ms" % (_fmt(ms), _fmt(n))
        threshold = "<= %d ms（返事を待たずに変わること）" % CLIENT_TOGGLE_WARN_MS
    elif cid == "client.idle":
        state_n, total = item.get("state"), item.get("total")
        if item.get("ok") is not True or not _num(state_n):
            status = FAIL
        elif state_n > IDLE_STATE_WARN:
            status = FAIL
        elif state_n > IDLE_STATE_PASS or (_num(total) and total > IDLE_TOTAL_WARN):
            status = WARN
        value = "/api/state %s 回 · 全通信 %s 回（%s 秒間）" % (_fmt(state_n), _fmt(total), _fmt(round(ms / 1000.0)) if _num(ms) else "?")
        threshold = "/api/state <= %d 回 / 30秒" % IDLE_STATE_PASS
    elif cid == "client.open_rtt":
        if item.get("ok") is not True or not _num(n):
            status = FAIL
        elif n > OPEN_RTT_WARN:
            status = FAIL
        elif n > OPEN_RTT_PASS:
            status = WARN
        value = "%s 回 · %s ms（タップから最初の描画まで）" % (_fmt(n), _fmt(ms))
        threshold = "<= %d 回" % OPEN_RTT_PASS
    else:
        status, value = FAIL, "エラー"
        threshold = ""
    if note:
        value = "%s（%s）" % (value, note) if value else note
    return {"id": cid, "name": name, "status": status, "value": value[:300], "threshold": threshold,
            "variant": variant}


TRANSPORT_LABEL = {"mjpeg": "ストリーム(MJPEG)", "sse": "ストリーム(SSE)", "poll": "ポーリング"}


def live_transport(flags):
    """The transport the app would end up on, in its own order: MJPEG, then SSE, then polling (or None)."""
    for key in ("mjpeg", "sse", "poll"):
        if (flags or {}).get(key) is True:
            return key
    return None


def _fmt(value):
    if not _num(value):
        return "?"
    return ("%d" % value) if float(value).is_integer() else ("%.1f" % value)


def _validate_device(device_raw):
    """Validated copy of the phone's device facts, or ValueError."""
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
    return device


def validate_client(payload):
    """Validated copy of the phone's POST body, or ValueError. Strings are cleaned (credentials, URLs)."""
    if not isinstance(payload, dict):
        raise ValueError("端末側の結果が不正です")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise ValueError("run_idが不正です")
    device = _validate_device(payload.get("device"))
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


# ------------------------------------------------------------------------ swipe diagnostic (v0.1.52)
# The phone feeds its own gesture handlers synthetic input (history.back(), touch events, a toolbar pull)
# while the Hub holds a fixture tab at A -> B -> C. The phone sends what it saw (facts, event counts, a
# compact timeline); every judgement is made here, from those numbers only.
SWIPE_MODES = ("mirror", "reader", "tiles")
SWIPE_MODE_LABEL = {"mirror": "ミラー", "reader": "リーダー", "tiles": "高速"}
SWIPE_STEPS = (                     # (id, title, page the NAS tab must show when the step starts)
    ("edge_back", "ブラウザの戻る（iOSの端スワイプ相当 history.back）", "C"),
    ("flick_right", "ページ中央で右へフリック（戻る・確認ページを飛ばす）", "B"),
    ("back_first", "最初のページでブラウザの戻る（アプリの前の画面へ戻らない）", "A"),
    ("flick_left", "ページ中央で左へフリック（進む・確認ページを飛ばす）", "A"),
    ("slow_move", "ゆっくり横へ動かす（フリックにならない）", "B"),
    ("edge_start", "画面の端から横へ動かす（フリックにならない）", "B"),
    ("forward", "ブラウザの進む（history.forward）", "B"),
    ("pull_cancel", "ツールバーを下へ引いて戻す（閉じない）", "B"),
    ("pull_close", "ツールバーを下へ引いて離す（閉じる）", "B"),
    ("pull_omnibox", "アドレスバー（フォーカスなし）から下へ引いて戻す（ヒントが出る・閉じない）", "B"),
    ("hscroll", "横スクロールできる表の上で横にフリック（フリックにならない）", "B"),
    ("pop_stay", "戻る先が別のアプリ画面でもタブに留まる（履歴スキップ相当）", "B"),
)
# The manual (real finger) flow: id, title, start page, and the automatic step whose rules judge it.
MANUAL_STEPS = (
    ("m_edge_back", "画面の左端から右へスワイプ（iPhoneの戻る操作）", "C", "edge_back"),
    ("m_flick_right", "ページの中央で右へすばやくフリック", "B", "flick_right"),
    ("m_flick_left", "ページの中央で左へすばやくフリック", "A", "flick_left"),
    ("m_table", "表の部分を横にスクロール（ゆっくり）", "B", "slow_move"),
    ("m_edge_forward", "画面の右端から左へスワイプ（iPhoneの進む操作、ある場合）", "B", "forward"),
    ("m_pull", "ツールバー（アドレスバー）を下へ引く（離さずに戻す）", "B", "pull_cancel"),
)
SWIPE_STEP = {i: (t, page) for i, t, page in SWIPE_STEPS}
SWIPE_STEP.update({i: (t, page) for i, t, page, _ in MANUAL_STEPS})
SWIPE_BASE = {i: base for i, _, _, base in MANUAL_STEPS}
SWIPE_ANSWERS = ("", "ok", "bad", "skip")
MANUAL_HARD_TIMEOUT = 600.0    # the real-finger flow takes a person minutes
SWIPE_SYNTHETIC = ("touch", "direct", "history", "none", "")
SWIPE_EVENT_NAME = re.compile(r"[a-z][a-z0-9_.]{0,31}\Z")
SWIPE_PATH = re.compile(r"/swipe/[abc]\Z")


def _swipe_pages(facts, step_id):
    start = SWIPE_STEP[step_id][1]
    return start, facts.get("nas_after") or "?", facts.get("nas_seq") or ""


def judge_swipe_step(step_id, facts, counts):
    """(status, value, [hints]) for one swipe step from the phone's facts and event counts.

    A manual step (real finger) is judged by the rules of its automatic twin, then the user's answer is applied."""
    manual = step_id in SWIPE_BASE
    base = SWIPE_BASE.get(step_id, step_id)
    answer = facts.get("answer", "")
    facts = dict(facts, nas_before=facts.get("nas_before", ""))
    if answer == "skip":
        return SKIP, "利用者がスキップしました", []
    status, value, hints = _judge_swipe(base, step_id, facts, counts, manual)
    if answer == "bad":
        hints.append("利用者が「できない/違う動き」と回答しました")
        status = FAIL
    return status, value, hints


def _judge_swipe(step_id, real_id, facts, counts, manual):
    def n(key):
        return counts.get(key, 0)
    start, after, seq = _swipe_pages(facts, step_id)
    before = facts.get("nas_before") or "?"
    label = facts.get("label") or ""
    synthetic = facts.get("synthetic") or ""
    moved_steps = n("step.moved")
    hints, status = [], PASS
    value = "NAS %s→%s / popstate %d / stepHistory %d(移動%d) / フリック受理 %d" % (
        before, after, n("popstate"), n("step.call"), moved_steps, n("flick.accepted"))
    gestures = facts.get("gestures") if manual else None
    if gestures is not None:
        return _judge_manual(step_id, facts, counts, gestures, value)

    def worse(new, hint):
        nonlocal status
        if hint:
            hints.append(hint)
        if new == FAIL or (new == WARN and status == PASS):
            status = new
    if facts.get("route_left"):
        worse(FAIL, "アプリがタブ画面から離れました（戻る処理のフォールバック history.back が働いた可能性）")
    if step_id in ("flick_right", "flick_left", "slow_move", "edge_start", "pull_cancel", "pull_close", "pull_omnibox",
                   "hscroll") and synthetic == "none":
        return SKIP, value + " / 入力を作れませんでした", ["この端末では合成タッチ入力を作れず、内部ハンドラの直接呼び出しもできませんでした"]
    expect = {"edge_back": "B", "flick_right": "A", "flick_left": "B"}.get(step_id)
    if step_id == "edge_back":
        if n("popstate") == 0:
            worse(FAIL, "エッジスワイプでpopstateが来ない（iOSが戻る操作をしていない）" if manual else
                  "history.back() で popstate が来ませんでした（ガード用の履歴がない、またはブラウザが戻る操作を止めた）")
        elif n("popstate") >= 2:
            worse(FAIL, "popstate が%d回発生（1回のはず）" % n("popstate"))
        if n("flick.accepted"):
            worse(FAIL, "フリックと戻るの二重実行（戻る操作中にフリックも受理された）")
        if n("popstate") and n("step.call") == 0:
            worse(FAIL, "popstate は来たが stepHistory が呼ばれていません（onPopStateが戻る処理をしていない）")
    elif step_id == "back_first":
        if n("popstate") == 0:
            worse(FAIL, "history.back() で popstate が来ませんでした（ガード用の履歴がない）")
        if moved_steps:
            worse(FAIL, "最初のページなのにNASが戻りました")
        if n("pop.leave"):
            worse(FAIL, "戻れないのにアプリの前の画面へ戻ろうとしました（history.back のフォールバック）")
        if n("toast") == 0:
            worse(WARN, "「これより前のページはありません」の案内が出ていません")
        expect = "A"
    elif step_id in ("flick_right", "flick_left"):
        if n("flick.accepted") == 0:
            worse(FAIL, "フリックが認識されませんでした（理由: %s）" % (facts.get("flick_why") or "判定ログなし"))
        elif n("flick.accepted") >= 2 or n("step.call") >= 2:
            worse(FAIL, "フリックの二重実行（受理 %d 回 / stepHistory %d 回）" % (n("flick.accepted"), n("step.call")))
        if n("popstate"):
            worse(FAIL if moved_steps >= 2 else WARN,
                  "フリックと戻るの二重実行（フリック中に popstate が%d回発生）" % n("popstate"))
    elif step_id in ("slow_move", "edge_start"):
        if n("flick.accepted") or n("step.call"):
            worse(FAIL, "フリックではない動きが戻る/進むとして実行されました（%s）" %
                  ("ゆっくりでも受理" if step_id == "slow_move" else "画面の端でも受理"))
        elif n("flick.reject") == 0:
            worse(WARN, "判定ログがありません（入力がハンドラに届いたか不明）")
        expect = start
    elif step_id == "hscroll":
        if n("flick.accepted") or n("step.call"):
            worse(FAIL, "横にスクロールできる表の上のフリックが戻る/進むとして実行されました")
        elif facts.get("flick_why") != "hscroll":
            worse(WARN, "横スクロール要素として見分けられていません（拒否の理由: %s）" % (facts.get("flick_why") or "判定ログなし"))
        expect = start
    elif step_id == "pop_stay":
        if n("popstate") == 0:
            worse(FAIL, "history.back() で popstate が来ませんでした（テスト用の履歴を作れていません）")
        elif n("pop.stay") == 0:
            worse(FAIL, "戻り先が別のアプリ画面でしたが、タブに留まる処理（pop.stay）が働きませんでした")
        if n("pop.stay") and n("step.call") != 1:
            worse(FAIL, "タブに留まったあとの stepHistory が%d回です（1回のはず）" % n("step.call"))
        if n("route"):
            worse(FAIL, "アプリの画面が切り替わりました（route %d回）" % n("route"))
        expect = "A"
    elif step_id == "forward":
        if n("popstate") >= 2:
            worse(FAIL, "popstate が%d回発生" % n("popstate"))
        if after not in (start, "C"):
            worse(FAIL, "進む操作で想定外のページ（%s）になりました" % after)
        value += "（進めるページ%s）" % ("あり" if after == "C" else "なし")
        expect = after
    elif step_id in ("pull_cancel", "pull_omnibox"):
        if facts.get("hint_shown") is not True:
            worse(FAIL, "下へ引いてもヒントが出ませんでした（touchmove がツールバーに届いていない）")
        if facts.get("hint_hidden") is not True:
            worse(FAIL, "戻してもヒントが消えませんでした")
        if facts.get("close_calls", 0):
            worse(FAIL, "引いて戻しただけでタブを閉じる処理が呼ばれました")
        value = "ヒント表示=%s 消去=%s 閉じる呼び出し=%d" % (
            facts.get("hint_shown") is True, facts.get("hint_hidden") is True, facts.get("close_calls", 0))
        expect = start
    elif step_id == "pull_close":
        calls = facts.get("close_calls", 0)
        if calls == 0:
            worse(FAIL, "引いて離してもタブを閉じる処理が呼ばれませんでした")
        elif calls >= 2:
            worse(FAIL, "タブを閉じる処理が%d回呼ばれました" % calls)
        if facts.get("hint_shown") is not True:
            worse(WARN, "閉じる前のヒントが出ませんでした")
        value = "ヒント表示=%s 閉じる呼び出し=%d（実際には閉じていません）" % (facts.get("hint_shown") is True, calls)
        expect = start
    if step_id not in ("pull_cancel", "pull_close", "pull_omnibox") and expect and after != expect:
        if after == "":
            worse(FAIL, "NASのページを取得できませんでした")
        elif after == before:
            worse(FAIL, "NASが動きませんでした（moved:false か、戻る処理が呼ばれていません）")
        elif "?" in seq or after == "?":
            worse(FAIL, "確認ページ（チャレンジ）や空白ページの履歴に止まりました（NASが %s と動きました）" % " → ".join(seq))
        elif moved_steps >= 2 or len(seq) >= 3:
            worse(FAIL, "戻る/進むの二重実行（NASが %s と動きました）" % " → ".join(seq))
        else:
            worse(FAIL, "想定と違うページ（期待 %s、実際 %s）" % (expect, after))
    elif step_id in ("edge_back", "flick_right", "flick_left") and moved_steps >= 2:
        worse(FAIL, "戻る/進むの二重実行（stepHistory の移動が%d回）" % moved_steps)
    if status != FAIL and expect and step_id not in ("pull_cancel", "pull_close", "pull_omnibox", "slow_move", "edge_start",
                                                      "hscroll") \
            and label and label != after:
        worse(WARN, "NASは%sですが表示は%sのままです（画面が更新されていません）" % (after, label))
    if status != FAIL and step_id == "edge_back" and n("guard.push") == 0:
        worse(WARN, "戻ったあとにガード用の履歴が積み直されていません（次の戻るが効かない恐れ）")
    return status, value, hints


MANUAL_GESTURE_KEYS = ("c", "m", "p", "f", "s")


def _judge_manual(step_id, facts, counts, gestures, value):
    """v0.1.55: judge a real-finger step per GESTURE (the phone cuts the timeline at each real touch.start outside
    the self-test banner). Repeating a swipe is fine; only ONE gesture that acts twice is a double execution.
    gestures: [{c: stepHistory calls, m: moved, p: popstate, f: flick accepted, s: stayed on the tab, nas: page}]."""
    def n(key):
        return counts.get(key, 0)
    start, after, seq = _swipe_pages(facts, step_id)
    before = facts.get("nas_before") or "?"
    label = facts.get("label") or ""
    hints, status = [], PASS

    def worse(new, hint):
        nonlocal status
        if hint:
            hints.append(hint)
        if new == FAIL or (new == WARN and status == PASS):
            status = new
    acting = [g for g in gestures if g.get("c") or g.get("p") or g.get("f")]
    total = facts.get("gesture_n") or len(gestures)
    value += " / ジェスチャー %d 回（動作 %d 回）" % (total, len(acting))
    if facts.get("route_left"):
        worse(FAIL, "アプリがタブ画面から離れました（戻る処理のフォールバック history.back が働いた可能性）")
    for i, g in enumerate(gestures, 1):
        if g.get("c", 0) >= 2 or g.get("m", 0) >= 2 or g.get("p", 0) >= 2 or (g.get("f", 0) and g.get("p", 0)):
            worse(FAIL, "1回の操作（%d回目）で二重実行（stepHistory %d / 移動 %d / popstate %d / フリック受理 %d）" % (
                i, g.get("c", 0), g.get("m", 0), g.get("p", 0), g.get("f", 0)))
    first = acting[0] if acting else None
    got = (first or {}).get("nas") or ""
    expect = {"edge_back": "B", "flick_right": "A", "flick_left": "B"}.get(step_id)
    if step_id == "edge_back":
        if n("popstate") == 0:
            worse(FAIL, "エッジスワイプでpopstateが来ない（iOSが戻る操作をしていない）")
        elif not any(g.get("p") and g.get("c") for g in gestures):
            worse(FAIL, "popstate は来たが stepHistory が呼ばれていません（onPopStateが戻る処理をしていない）")
    elif step_id in ("flick_right", "flick_left"):
        if not any(g.get("f") for g in gestures):
            worse(FAIL, "フリックが認識されませんでした（理由: %s）" % (facts.get("flick_why") or "判定ログなし"))
    elif step_id == "slow_move":
        if any(g.get("f") or g.get("c") for g in gestures):
            worse(FAIL, "表を動かす操作が戻る/進むとして実行されました（フリックとして受理）")
        elif n("flick.reject") == 0:
            worse(WARN, "判定ログがありません（入力がハンドラに届いたか不明）")
        expect = None
        if after != start:
            worse(FAIL, "ページが動きました（期待 %s、実際 %s）" % (start, after))
    elif step_id == "forward":
        if after not in (start, "C"):
            worse(FAIL, "進む操作で想定外のページ（%s）になりました" % after)
        value += "（進めるページ%s）" % ("あり" if after == "C" else "なし")
    elif step_id == "pull_cancel":
        if facts.get("hint_shown") is not True:
            worse(FAIL, "下へ引いてもヒントが出ませんでした（touchmove がツールバーに届いていない）")
        if facts.get("hint_hidden") is not True:
            worse(FAIL, "戻してもヒントが消えませんでした")
        if facts.get("close_calls", 0):
            worse(FAIL, "引いて戻しただけでタブを閉じる処理が呼ばれました")
        return status, "ヒント表示=%s 消去=%s 閉じる呼び出し=%d" % (
            facts.get("hint_shown") is True, facts.get("hint_hidden") is True, facts.get("close_calls", 0)), hints
    if expect:
        if first is None:
            pass                      # nothing acted: reported above as popstate / flick missing
        elif not got:
            worse(FAIL, "NASが動きませんでした（moved:false か、戻る処理が呼ばれていません）")
        elif got != expect:
            if "?" in seq or got == "?":
                worse(FAIL, "確認ページ（チャレンジ）や空白ページの履歴に止まりました（NASが %s と動きました）" % " → ".join(seq))
            else:
                worse(FAIL, "最初の操作の結果が想定と違います（期待 %s、実際 %s）" % (expect, got))
    if status != FAIL and step_id in ("edge_back", "flick_right", "flick_left") and label and label != after:
        worse(WARN, "NASは%sですが表示は%sのままです（画面が更新されていません）" % (after, label))
    if status != FAIL and step_id == "edge_back" and n("guard.push") == 0:
        worse(WARN, "戻ったあとにガード用の履歴が積み直されていません（次の戻るが効かない恐れ）")
    return status, value, hints


def _swipe_str(value, limit, name):
    if not isinstance(value, str):
        raise ValueError("%sが不正です" % name)
    text = re.sub(r"https?://(?!selftest\.example\.com(?:[/\s:]|\Z))\S*", "<url>", _short(value, limit * 2), flags=re.I)
    return text[:limit]


def validate_swipe(payload):
    """Validated copy of one swipe POST (one display mode), or ValueError. Strings cleaned, URLs reduced."""
    if not isinstance(payload, dict):
        raise ValueError("スワイプ結果が不正です")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise ValueError("run_idが不正です")
    out = {"run_id": run_id, "last": payload.get("last") is True}
    if payload.get("error") is not None:
        out["error"] = _swipe_str(payload["error"], 160, "error")
    if payload.get("device") is not None:
        out["device"] = _validate_device(payload["device"])
    mode = payload.get("mode")
    if mode is None:
        out["mode"] = None
        return out
    if not isinstance(mode, dict) or mode.get("name") not in SWIPE_MODES:
        raise ValueError("modeが不正です")
    display = mode.get("display")
    if display not in SWIPE_MODES + ("live", ""):
        raise ValueError("mode.displayが不正です")
    synthetic = mode.get("synthetic", "")
    if synthetic not in SWIPE_SYNTHETIC:
        raise ValueError("mode.syntheticが不正です")
    growth = mode.get("history_growth")
    if growth is not None and (not isinstance(growth, int) or isinstance(growth, bool) or not -50 <= growth <= 50):
        raise ValueError("mode.history_growthが不正です")
    steps_raw = mode.get("steps")
    if not isinstance(steps_raw, list) or len(steps_raw) > len(SWIPE_STEPS) + 1:
        raise ValueError("stepsが不正です")
    steps, seen = [], set()
    for raw in steps_raw:
        if not isinstance(raw, dict) or raw.get("id") not in SWIPE_STEP or raw["id"] in seen:
            raise ValueError("steps.idが不正です")
        seen.add(raw["id"])
        step = {"id": raw["id"]}
        facts_raw = raw.get("facts")
        if not isinstance(facts_raw, dict):
            raise ValueError("factsが不正です")
        facts = {}
        for key in ("nas_before", "nas_after", "label"):
            value = facts_raw.get(key, "")
            if value not in ("", "A", "B", "C", "?"):
                raise ValueError("facts.%sが不正です" % key)
            facts[key] = value
        seq = facts_raw.get("nas_seq", "")
        if not isinstance(seq, str) or not re.fullmatch(r"[ABC?]{0,16}", seq):
            raise ValueError("facts.nas_seqが不正です")
        facts["nas_seq"] = seq
        for key in ("route_left", "hint_shown", "hint_hidden"):
            value = facts_raw.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError("facts.%sが不正です" % key)
            if value is not None:
                facts[key] = value
        if facts_raw.get("close_calls") is not None:
            if not _num(facts_raw["close_calls"], 0, 100) or isinstance(facts_raw["close_calls"], float):
                raise ValueError("facts.close_callsが不正です")
            facts["close_calls"] = facts_raw["close_calls"]
        if facts_raw.get("answer", "") not in SWIPE_ANSWERS:
            raise ValueError("facts.answerが不正です")
        if facts_raw.get("answer"):
            facts["answer"] = facts_raw["answer"]
        if facts_raw.get("synthetic", "") not in SWIPE_SYNTHETIC:
            raise ValueError("facts.syntheticが不正です")
        facts["synthetic"] = facts_raw.get("synthetic", "")
        if facts_raw.get("gestures") is not None:       # v0.1.55: per-gesture results of a manual step
            gs = facts_raw["gestures"]
            if not isinstance(gs, list) or len(gs) > 12:
                raise ValueError("facts.gesturesが不正です")
            clean = []
            for g in gs:
                if not isinstance(g, dict) or g.get("nas", "") not in ("", "A", "B", "C", "?"):
                    raise ValueError("facts.gesturesが不正です")
                item = {"nas": g.get("nas", "")}
                for key in MANUAL_GESTURE_KEYS:
                    if not _num(g.get(key, 0), 0, 1000) or isinstance(g.get(key, 0), float):
                        raise ValueError("facts.gesturesが不正です")
                    item[key] = g.get(key, 0)
                clean.append(item)
            facts["gestures"] = clean
            gn = facts_raw.get("gesture_n", len(clean))
            if not _num(gn, 0, 1000) or isinstance(gn, float):
                raise ValueError("facts.gesture_nが不正です")
            facts["gesture_n"] = gn
        if facts_raw.get("flick_why") is not None:
            facts["flick_why"] = _swipe_str(facts_raw["flick_why"], 60, "facts.flick_why")
        if facts_raw.get("ms") is not None:
            if not _num(facts_raw["ms"], 0, 600000):
                raise ValueError("facts.msが不正です")
            facts["ms"] = facts_raw["ms"]
        step["facts"] = facts
        counts_raw = raw.get("counts")
        if not isinstance(counts_raw, dict) or len(counts_raw) > 48:
            raise ValueError("countsが不正です")
        counts = {}
        for key, value in counts_raw.items():
            if (not isinstance(key, str) or not SWIPE_EVENT_NAME.match(key) or isinstance(value, bool) or
                    not isinstance(value, int) or not 0 <= value <= 100000):
                raise ValueError("countsが不正です")
            counts[key] = value
        step["counts"] = counts
        events_raw = raw.get("events", [])
        if not isinstance(events_raw, list) or len(events_raw) > SWIPE_EVENTS_MAX:
            raise ValueError("eventsが不正です")
        events = []
        for ev in events_raw:
            if (not isinstance(ev, dict) or not _num(ev.get("t"), 0, 600000) or isinstance(ev.get("t"), float) or
                    not isinstance(ev.get("e"), str) or not SWIPE_EVENT_NAME.match(ev["e"])):
                raise ValueError("eventsの要素が不正です")
            detail = ev.get("d", "")
            events.append({"t": ev["t"], "e": ev["e"], "d": _swipe_str(detail, 120, "events.d")})
        step["events"] = events
        steps.append(step)
    out["mode"] = {"name": mode["name"], "display": display, "synthetic": synthetic, "steps": steps,
                   "history_growth": growth}
    return out


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
        only = options.get("only") if options.get("only") in ("swipe", "swipe_manual") else ""
        self.sections = [{"id": i, "label": l, "status": "pending", "n": 0} for i, l in SECTIONS
                         if (i in (only, "cleanup") if only else (i not in ("swipe", "swipe_manual") or
                                                      (i == "swipe" and options.get("swipe") is True)))]
        self.swipe = None               # v0.1.52: {"tab", "kind", "state", "modes": {mode: [step results]}}
        self.hard = MANUAL_HARD_TIMEOUT if options.get("only") == "swipe_manual" else HARD_TIMEOUT
        self.swipe_evt = threading.Event()
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
                 container_io=None, fixtures=None, thread=True, pt=None):
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
        self._pt = pt        # v0.1.57: callable -> [(id, name, status, value, threshold)] (the Hub's passthrough check)
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
                                                 "client": options.get("client", True) is not False,
                                                 "swipe": options.get("swipe") is True or options.get("only") == "swipe",
                                                 "only": options.get("only") if options.get("only") in (
                                                     "swipe", "swipe_manual") else ""},
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
            run.swipe_evt.set()
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
                if item["id"] == "client.live":
                    run.info["live_transport"] = live_transport(item.get("flags"))
                    run.info["live_fps"] = item.get("fps")
                self._add_locked(run, "response" if result["id"] in RESPONSE_IDS else "client", result["id"],
                                 result["name"], result["status"],
                                 result["value"], result["threshold"], hint_key=result["id"],
                                 variant=result.get("variant"))
            run.client = "received"
            run.client_evt.set()
            self._bump(run)
        return {"ok": True, "checks": len(data["checks"])}

    def submit_swipe(self, payload):
        """One display mode of the phone's swipe diagnostic (validated by validate_swipe); judged here."""
        data = validate_swipe(payload)
        with self._cv:
            self._expire_locked()
            run = self._run
            if run is None or run.id != data["run_id"] or run.state != "running":
                raise ValueError("このセルフテストは進行中ではありません")
            if run.phase != "swipe" or run.swipe is None or run.swipe["state"] != "waiting":
                raise ValueError("スワイプの結果を受け付けられる段階ではありません")
            if data.get("device") and not run.device:
                run.device = data["device"]
            mode = data.get("mode")
            if mode is not None:
                if mode["name"] in run.swipe["modes"]:
                    raise ValueError("このモードの結果はすでに受け取っています")
                self._swipe_judge_locked(run, mode)
            if data.get("error"):
                self._add_locked(run, "swipe", "swipe.error", "スワイプの自動確認（端末側のエラー）", FAIL,
                                 data["error"], hint_key="client.error")
            if data["last"] or data.get("error"):
                run.swipe["state"] = "received"
                run.swipe_evt.set()
            self._bump(run)
        return {"ok": True, "steps": 0 if mode is None else len(mode["steps"])}

    def swipe_goto(self, run_id, page):
        """Puts the fixture tab back on page A, B or C of its history (the step's starting point)."""
        if page not in SWIPE_LETTERS or not isinstance(run_id, str) or not RUN_ID.match(run_id):
            raise ValueError("パラメータが不正です")
        with self._cv:
            run = self._run
            if run is None or run.id != run_id or run.state != "running" or run.phase != "swipe" or not run.swipe:
                raise ValueError("スワイプの確認中ではありません")
            tab = run.swipe["tab"]
        return {"ok": bool(self.rb.selftest_history_to(tab, "/swipe/" + page.lower()))}

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
        if run is not None and run.state == "running" and self._clock() - run.t0 > run.hard + 15.0:
            # The worker is stuck in a blocking call: end the run for the viewer anyway.
            run.cancel.set()
            run.client_evt.set()
            run.swipe_evt.set()
            self._finish_locked(run, "timeout")

    def _bump(self, run):
        run.rev += 1
        self._cv.notify_all()

    def _guard(self, run):
        if run.cancel.is_set():
            raise Cancelled()
        if self._clock() - run.t0 > run.hard:
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
                 "value": str(value)[:600], "threshold": str(threshold)[:120], "hint": hint}
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

    def _swipe_report_lines(self, run):
        """The timelines of the swipe diagnostic (cut at SWIPE_REPORT_BYTES)."""
        dev = run.device or {}
        manual = run.swipe["kind"] == "manual"
        lines = ["", ("## スワイプ（手動）のタイムライン / swipe timelines (real fingers, answers by the user)" if manual else
                      "## スワイプ（自動）のタイムライン / swipe timelines (synthetic input fed to the app's own handlers)"),
                 "device: standalone=%s viewport=%sx%s dpr=%s touch=%s | ua=%s" % (
                     dev.get("standalone", "?"), dev.get("width", "?"), dev.get("height", "?"), dev.get("dpr", "?"),
                     dev.get("touch", "?"), dev.get("ua", "?"))]
        tag = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
        size = sum(len(x) + 1 for x in lines)
        cut = False
        for name in SWIPE_MODES:
            mode = run.swipe["modes"].get(name)
            if not mode:
                continue
            lines.append("### %s (display=%s, input=%s)" % (name, mode["display"], mode["synthetic"] or "?"))
            for step in mode["steps"]:
                limit = SWIPE_REPORT_PASS_LINES if step["status"] == PASS else SWIPE_REPORT_LINES
                f = step["facts"]
                block = ["  [%s] %s | %s" % (tag[step["status"]], step["id"], step["value"]),
                         "      facts: nas %s>%s seq=%s label=%s route_left=%s%s | counts: %s" % (
                             f.get("nas_before") or "?", f.get("nas_after") or "?", f.get("nas_seq") or "-",
                             f.get("label") or "-", f.get("route_left", "?"),
                             (" flick_why=%s" % f["flick_why"]) if f.get("flick_why") else "",
                             " ".join("%s=%d" % kv for kv in sorted(step["counts"].items())) or "-")]
                if step["hint"]:
                    block.append("      ヒント: %s" % step["hint"])
                block.extend(_swipe_timeline(step, limit))
                added = sum(len(x) + 1 for x in block)
                if size + added > SWIPE_REPORT_BYTES:
                    cut = True
                    break
                lines.extend(block)
                size += added
            if cut:
                lines.append("  … (レポートの大きさの上限でここまで)")
                break
        return lines

    # ------------------------------------------------------------------------------ the thread
    def _main(self, run):
        state = "done"
        fixtures = self._fixtures_factory()
        try:
            self._debug_on(run)
            run.io_before = self._io_totals()
            if run.options["only"]:
                self._swipe_only(run, fixtures)
                return
            usable = self._section_env(run)
            if usable:
                self._section_browser(run, fixtures)
            else:
                self._add(run, "browser", "browser.skipped", "NASブラウザの確認", SKIP,
                          "NASブラウザを使えないため省略しました")
                self._leave(run, "browser", skipped=True)
            self._section_pt(run)
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

    # v0.1.57: パススルー (server side only; SKIP while the setting is off). Independent of the NAS browser.
    def _section_pt(self, run):
        if self._pt is None:                # no passthrough hook (unit tests): the section is just skipped
            self._leave(run, "pt", skipped=True)
            return
        self._enter(run, "pt")
        try:
            rows = list(self._pt())
        except Exception as error:      # a bug here must not stop the other sections
            rows = [("pt.error", "パススルーの確認", FAIL, "%s: %s" % (type(error).__name__, _short(str(error), 100)), "")]
        if not rows:
            rows = [("pt.off", "パススルー", SKIP, "確認できません", "")]
        for cid, name, status, value, threshold in rows:
            self._guard(run)
            self._add(run, "pt", cid, name, status if status in STATUSES else FAIL, value, threshold)
        self._leave(run, "pt", skipped=all(r[2] == SKIP for r in rows))

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
        self._cgroup_row(run)
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

    def _cgroup_row(self, run):
        """env.cgroup: which source gives container memory / disk I/O here (cgroup layout or docker stats)."""
        runtime = getattr(self.rb, "runtime", None)
        info = run.info
        root = getattr(self.rb, "_cgroup_root", diskio.DEFAULT_CGROUP_ROOT)
        proc = getattr(self.rb, "_proc_root", "/proc")
        info["cgroup"] = diskio.cgroup_layout_summary(root, proc)
        if runtime is None:
            self._add(run, "env", "env.cgroup", "コンテナの計測元（cgroup）", SKIP, "取得できません")
            return
        rows, ok_any, run_any, sources = [], False, False, []
        for label, name in (("browser", cr.BROWSER_NAME), ("jd2", cr.JD2_NAME)):
            try:
                rep = diskio.cgroup_report(runtime, name, root, proc, read_memory=rb_resident.read_cgroup_memory)
            except Exception:
                rep = {"running": False}
            if not rep.get("running"):
                rows.append("%s: 停止中" % label)
                continue
            run_any = True
            mem, io = rep.get("mem"), rep.get("io")
            ok_any = ok_any or bool(mem or io)
            sources.append("%s mem=%s io=%s" % (label, mem or "docker stats", io or "docker stats"))
            where = ""
            if rep.get("dir"):
                where = " dir=%s(%s)" % (_short(rep["dir"].replace("/sys/fs/cgroup", "…"), 70), rep.get("how"))
            rows.append("%s: メモリ=%s I/O=%s%s" % (label, mem or "docker stats", io or "docker stats", where))
            if not (mem and io):
                rows.append("  pid-cgroup=%s mounts=%s" % (" ".join(rep.get("pid_cgroup") or []) or "?",
                                                           " ".join(rep.get("mounts") or []) or "?"))
        info["cgroup_sources"] = "; ".join(sources) if sources else "コンテナ停止中"
        value = "層=%s | %s" % (info["cgroup"], " | ".join(rows))
        if not run_any:
            self._add(run, "env", "env.cgroup", "コンテナの計測元（cgroup）", SKIP, value, "cgroupから読めること")
        else:
            self._add(run, "env", "env.cgroup", "コンテナの計測元（cgroup）", PASS if ok_any else WARN, value,
                      "cgroupから読めること（docker statsはCPUを使う）")

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
        if run.options["swipe"]:
            if run.client == "received":
                self._swipe_phase(run, fixtures)
            else:
                self._add(run, "swipe", "swipe.skipped", "スワイプの自動確認", SKIP,
                          "端末側の結果が届かなかったため省略しました")
                self._leave(run, "swipe", skipped=True)
        self._sites(run)
        self._capacity(run, fixtures)
        self._queue(run, fixtures)

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
        self._first_load_check(run, tab, sec, "builtin.first_load", "初回読み込みの内訳", "builtin.first_load")
        # JS dropdown
        self._menu_check(run, tab, fixtures)
        # page that changes itself
        self._dynamic_check(run, tab)
        # tiles
        self._tiles_check(run, tab)
        # meta long-poll + screencast
        self._meta_and_fps(run, tab)

    def _first_load_check(self, run, tab, section, cid, name, hint_key, extra=None):
        """v0.1.54: where the first load of this page spent its time (stages from rb_firstload, each with its
        own threshold and hint). Skipped quietly when the browser object has no timeline."""
        method = getattr(self.rb, "first_load", None)
        if method is None:
            return
        try:
            data = method(tab, stages=rb_firstload.SELFTEST_STAGES, capture_kind="mirror")
        except (Cancelled, RunTimeout):
            raise
        except Exception:
            return
        if not data:
            return
        rows = data["stages"]
        status = rb_firstload.worst(rows)
        bad = [r for r in rows if r["status"] in (WARN, FAIL)]
        variant = max(bad, key=lambda r: r["ms"])["id"] if bad else None
        threshold = " / ".join("%s <= %.1fs" % (r["label"], r["warn_ms"] / 1000.0) for r in rows)
        self._add(run, section, cid, name, status if status != "skip" else SKIP, rb_firstload.line(data), threshold,
                  hint_key=hint_key, variant=variant, extra=extra)

    def _menu_check(self, run, tab, fixtures):
        rb, sec = self.rb, "builtin"
        self._step(run, "JSメニューの操作を測っています")
        name = "JSメニューのトグル（再読み込みなし）"
        try:
            # v0.1.58: a reload seen once under load is measured again; only a repeated reload is a failure
            for attempt in (1, 2):
                opened, toggled, reloaded = self._menu_once(run, tab, fixtures)
                if not reloaded:
                    break
            ok = opened and toggled and not reloaded
            value = "メニュー項目が出現=%s / ページ側スクリプト実行=%s / 再読み込み=%s" % (
                "はい" if opened else "いいえ", "はい" if toggled else "いいえ", "あり" if reloaded else "なし")
            if attempt == 2:
                value += "（2回測定）"
            self._add(run, sec, "builtin.menu", name, PASS if ok else FAIL, value, "開く・再読み込みなし")
        except (Cancelled, RunTimeout):
            raise
        except Exception as error:
            self._add(run, sec, "builtin.menu", name, FAIL, _short(str(error), 160))

    def _menu_once(self, run, tab, fixtures):
        rb = self.rb
        if self._goto(run, tab, "/menu") is None:
            raise RuntimeError("メニューのページを読み込めません")
        loads = fixtures.loads("/menu")
        toggles = fixtures.toggles("open")
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
        return opened, fixtures.toggles("open") > toggles, fixtures.loads("/menu") != loads

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

    def _measure_stream(self, run, tab, seconds, scroll=False):
        """(fps, KB per frame, frames[, moved]) of the live stream of `tab` over `seconds` (the Hub's own stream).

        With `scroll` the NAS page is scrolled every SITE_SCROLL_STEP_S from an isolated world so that a still
        real site produces frames too; the result then has a fourth value, the number of scrolls that moved it."""
        counts = {"parts": 0, "bytes": 0}
        before = self.rb.selftest_info(tab)["seq"]
        stop = threading.Event()
        moved = [0]

        def scroller():
            last = 0              # the page starts at the top
            while not stop.is_set():
                try:
                    y = self.rb.selftest_scroll(tab, SITE_SCROLL_PX)
                except Exception:
                    y = None
                if y is not None and y != last:
                    moved[0] += 1
                last = y if y is not None else last
                stop.wait(SITE_SCROLL_STEP_S)

        def write(part):
            counts["parts"] += 1
            counts["bytes"] += len(part)
            if run.cancel.is_set():
                raise OSError("cancelled")
        worker = None
        if scroll:
            worker = threading.Thread(target=scroller, daemon=True, name="selftest-scroll")
            worker.start()
        started = self._clock()
        try:
            self.rb.stream(tab, write, max_seconds=seconds)
        except OSError:
            pass
        finally:
            stop.set()
            if worker is not None:
                worker.join(2.0)
        elapsed = max(self._clock() - started, 0.001)
        frames = counts["parts"]
        # a stream sends only fresh frames; seq shows how many the page really produced
        produced = max(0, self.rb.selftest_info(tab)["seq"] - before)
        frames = min(frames, produced) if produced else frames
        result = (frames / elapsed, (counts["bytes"] // max(counts["parts"], 1)) // 1024, frames)
        return result + (moved[0],) if scroll else result

    # ---------------------------------------------------------------- section 4: the phone
    def _client_phase(self, run, tab):
        sec = "client"
        if not run.options["client"]:
            with self._cv:
                run.client = "none"
            self._add(run, sec, "client.missing", "端末側のテスト", SKIP, "この実行では端末側を行いません")
            self._leave(run, sec, skipped=True)
            self._leave(run, "response", skipped=True)
            return
        try:
            self.rb.selftest_goto(tab, "/anim")
            self._wait_loaded(run, tab, "/anim")
        except Exception:
            pass
        try:
            # v0.1.46: the phone's measurements must not lose the screencast to the idle stop
            self.rb.selftest_keep(tab, CLIENT_WAIT + 15.0)
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
        self._leave(run, "response", skipped=not received)

    # ------------------------------------------------------------ swipe (automatic, on the phone)
    def _swipe_only(self, run, fixtures):
        reason = None
        try:
            reason = self.rb.memory_reason()
        except Exception as error:
            reason = _short(str(error), 100)
        if reason:
            self._add(run, self._swipe_section(run), "swipe.start", "スワイプの確認", FAIL, reason[:200],
                      hint_key="env.guard")
            self._leave(run, self._swipe_section(run))
            return
        self._swipe_phase(run, fixtures)

    @staticmethod
    def _swipe_section(run):
        return "swipe_manual" if run.options["only"] == "swipe_manual" else "swipe"

    def _swipe_phase(self, run, fixtures):
        """Opens the A -> (check page) -> A?token -> B -> C fixture tab, waits for the phone's diagnostic, always
        closes the tab. The same phase serves the automatic and the manual (real finger) flow."""
        rb = self.rb
        sec = self._swipe_section(run)
        manual = sec == "swipe_manual"
        self._enter(run, sec, "スワイプを確認しています")
        tab = None
        try:
            try:
                created = rb.open_tab(self._origin() + SWIPE_ROUTE[0], selftest={"fixture": fixtures})
            except Exception as error:
                self._add(run, sec, "swipe.start", "スワイプの確認（テスト用タブ）", FAIL, _short(str(error), 160),
                          hint_key="browser.start")
                return
            tab = created["id"]
            width, height, dpr = run.options["viewport"]
            try:
                rb.set_viewport(tab, width, height, dpr)
            except Exception:
                pass
            for path in SWIPE_ROUTE:
                if path != SWIPE_ROUTE[0]:
                    rb.selftest_goto(tab, path)
                if self._wait_loaded(run, tab, path) is None:
                    self._add(run, sec, "swipe.start", "スワイプの確認（テスト用ページ）", FAIL,
                              "%s を読み込めませんでした" % path.split("?")[0], hint_key="builtin.ready")
                    return
            with self._cv:
                run.tab = tab
                run.swipe = {"tab": tab, "kind": "manual" if manual else "auto", "state": "waiting", "modes": {}}
                run.phase = "swipe"
                run.current = "この端末でスワイプを確認しています"
                self._bump(run)
            end = self._clock() + (run.hard - 30.0 if manual else SWIPE_WAIT)
            while not run.swipe_evt.is_set():
                self._guard(run)
                if self._clock() >= end:
                    break
                run.swipe_evt.wait(0.25)
            self._guard(run)
            with self._cv:
                got = run.swipe["state"] == "received"
                run.swipe["state"] = "closed"
            if not got:
                self._add(run, sec, "swipe.missing", "スワイプの確認", WARN,
                          "待ちましたが結果が届きませんでした", "結果が届くこと", hint_key="swipe.missing")
        finally:
            if tab is not None:
                self._close_quiet(tab)
            run.tab = None
            self._leave(run, sec)

    def _swipe_judge_locked(self, run, mode):
        sec = "swipe_manual" if run.swipe["kind"] == "manual" else "swipe"
        name, label = mode["name"], SWIPE_MODE_LABEL[mode["name"]]
        shown = mode["display"] or "?"
        self._add_locked(run, sec, "swipe.%s.display" % name, "【%s】実際の表示" % label,
                         PASS if shown == name else WARN,
                         "表示: %s（入力: %s）" % (SWIPE_MODE_LABEL.get(shown, shown), mode["synthetic"] or "?"),
                         "要求した表示になること", hint_key="swipe.display")
        results = []
        for step in mode["steps"]:
            if (step["id"] in SWIPE_BASE) != (run.swipe["kind"] == "manual"):
                raise ValueError("steps.idがこの確認方法のものではありません")
            title = SWIPE_STEP[step["id"]][0]
            status, value, hints = judge_swipe_step(step["id"], step["facts"], step["counts"])
            hint = " / ".join(hints)
            self._add_locked(run, sec, "swipe.%s.%s" % (name, step["id"]), "【%s】%s" % (label, title),
                             status, value, extra={"hint": hint if status in (WARN, FAIL) else ""})
            results.append({"id": step["id"], "title": title, "status": status, "value": value, "hint": hint,
                            "facts": step["facts"], "counts": step["counts"], "events": step["events"]})
        growth = mode.get("history_growth")
        if growth is not None:
            self._add_locked(run, sec, "swipe.%s.history" % name, "【%s】履歴の増え方（表示切替・戻る）" % label,
                             PASS if growth == 0 else FAIL, "履歴 +%d" % growth, "増えないこと（ガードは1つだけ）",
                             hint_key="swipe.history")
        run.swipe["modes"][name] = {"display": shown, "synthetic": mode["synthetic"], "steps": results,
                                    "history_growth": growth}

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

    # ---------------------------------------------------------------- 「タブの容量」 (v0.1.65)
    def _mem_sample(self):
        """Fresh container memory {used_mib, anon_mib}: a cgroup read now (never the 5 s cache), else a new docker stats."""
        try:
            value = self.rb.container_memory(fresh=True)
        except Exception:
            return None
        if not value:
            return None
        anon = value.get("anon_bytes")
        return {"used": value["used_bytes"] / MIB, "anon": None if anon is None else anon / MIB,
                "source": value.get("source")}

    def _capacity(self, run, fixtures):
        """Opens pages one at a time and KEEPS them open so the browser's memory grows, reading the container's memory
        after each page has loaded and settled: the cost of one page, and from it the recommended number of tabs.
        Pages: the built-in virtual pages (a light one, a heavy one three times) and, when the user gave real sites
        for the 実サイト part, those (same consent). Every tab is closed again; nothing is written to disk."""
        rb = self.rb
        self._enter(run, "capacity", "タブの容量を測っています")
        opened = []
        try:
            t0 = self._clock()
            base = self._mem_sample()
            if base is None:
                self._add(run, "capacity", "capacity.skipped", "タブの容量", SKIP,
                          "コンテナのメモリを読めないため測れません")
                self._leave(run, "capacity", skipped=True)
                return
            try:
                status = rb.status()
            except Exception:
                status = {}
            user_tabs = len(status.get("tabs") or [])
            limit_mib = (status.get("memory") or {}).get("limit_mib")
            if not limit_mib:
                try:
                    limit_bytes = rb._browser_limit_bytes()
                    limit_mib = None if not limit_bytes else limit_bytes / MIB
                except Exception:
                    limit_mib = None
            available = status.get("available_mib")
            width, height, dpr = run.options["viewport"]
            self._add(run, "capacity", "capacity.base", "ブラウザ本体（開いているページなし）", PASS,
                      "コンテナ %d MiB%s / ユーザーのタブ %d 枚 / コンテナ上限 %s" % (
                          base["used"], "" if limit_mib is None else "（上限 %d MiB）" % limit_mib, user_tabs,
                          "-" if limit_mib is None else "%d MiB" % limit_mib))
            plan = [(path, label, True) for path, label in CAPACITY_VIRTUAL]
            for index, item in enumerate(run.options["urls"], 1):
                plan.append((item["url"], "実サイト%d（%s）" % (index, item["host"]), False))
            samples = []                  # (label, delta_mib, real)
            previous = base["used"]
            stopped = None
            for number, (target, label, virtual) in enumerate(plan, 1):
                self._guard(run)
                if self._clock() - t0 > CAPACITY_BUDGET_S:
                    stopped = "時間の上限（%d秒）のため %d 枚目以降は測りませんでした" % (CAPACITY_BUDGET_S, number)
                    break
                try:
                    reason = rb.memory_reason()
                except Exception as error:
                    reason = _short(str(error), 100)
                if reason:
                    stopped = "メモリ保護のため %d 枚目で止めました（%s）" % (number, _short(reason, 100))
                    break
                self._step(run, "%d枚目 %s を開いています" % (number, label))
                try:
                    created = rb.open_tab(
                        self._origin() + target if virtual else target,
                        selftest={"fixture": fixtures, "capacity": True} if virtual else {"capacity": True})
                except Exception as error:
                    stopped = "%d 枚目を開けませんでした（%s）" % (number, _short(str(error), 100))
                    break
                tab = created["id"]
                opened.append(tab)
                try:
                    rb.set_viewport(tab, width, height, dpr)
                except Exception:
                    pass
                loaded = self._capacity_wait(run, tab, target if virtual else None, virtual)
                self._wait(run, CAPACITY_SETTLE_S)
                sample = self._mem_sample()
                heap = None
                try:
                    heap = rb.selftest_heap(tab)
                except Exception:
                    heap = None
                if sample is None:
                    stopped = "%d 枚目のメモリを読めませんでした" % number
                    break
                delta = sample["used"] - previous
                previous = sample["used"]
                samples.append((label, delta, not virtual))
                anon = ""
                if sample["anon"] is not None:
                    anon = " / うち匿名メモリ %d MiB" % sample["anon"]
                self._add(run, "capacity", "capacity.page.%d" % number, "%d枚目 %s" % (number, label),
                          WARN if delta > CAPACITY_PAGE_WARN_MIB else PASS,
                          "%s%d MiB（コンテナ合計 %d MiB%s / JSヒープ %s / 読み込み %s）%s" % (
                              "+" if delta >= 0 else "", round(delta), round(sample["used"]), anon,
                              "-" if heap is None else "%.1f MiB" % (heap / MIB),
                              "-" if loaded is None else "%d ms" % loaded,
                              "（ブラウザの描画プロセスの起動を含む）" if number == 1 else ""),
                          "<= %d MiB" % CAPACITY_PAGE_WARN_MIB, hint_key="capacity.page",
                          extra={"site": label.split("（")[-1].rstrip("）")} if not virtual else None)
            stats = page_costs(samples)
            self._capacity_summary(run, stats, base["used"], limit_mib, available, user_tabs, stopped)
        finally:
            for tab in opened:
                self._close_quiet(tab)
            self._leave(run, "capacity")

    def _queue(self, run, fixtures):
        """v0.1.65: queues QUEUE_PROBES virtual long presses at once (RAM only, one tab, virtual pages) and checks what the
        real queue promises: they all finish, one after another (never two at a time), none fails for lack of memory,
        and each answer carries the links. Reports the time per job and the container's peak memory."""
        rb = self.rb
        self._enter(run, "queue", "リンク解析の待ち行列を確認しています")
        tab = None
        try:
            try:
                created = rb.open_tab(self._origin() + "/article", selftest={"fixture": fixtures})
            except Exception as error:
                self._add(run, "queue", "queue.run", "リンク解析キュー", SKIP, "確認用のタブを開けませんでした（%s）" % _short(str(error), 100))
                return
            tab = created["id"]
            t0 = self._clock()
            ids = []
            try:
                for n in range(1, QUEUE_PROBES + 1):
                    answer = rb.probe_enqueue(tab, None, None, href=self._origin() + "/go/%d" % n, label="確認%d" % n)
                    ids.append(answer["job"])
            except Exception as error:
                self._add(run, "queue", "queue.enqueue", "待ち行列への追加", FAIL, _short(str(error), 160),
                          "%d件とも追加できること" % QUEUE_PROBES)
                return
            self._add(run, "queue", "queue.enqueue", "待ち行列への追加", PASS,
                      "%d件を%d ms で追加（押してすぐ受け付けられる）" % (len(ids), int((self._clock() - t0) * 1000)))
            peak = 0.0
            jobs = []
            while True:
                self._guard(run)
                jobs = rb.probe_jobs(tab)["jobs"]
                sample = self._mem_sample()
                if sample is not None:
                    peak = max(peak, sample["used"])
                if len(jobs) >= len(ids) and all(j["state"] in ("done", "failed", "cancelled") for j in jobs):
                    break
                if self._clock() - t0 > QUEUE_BUDGET_S:
                    break
                self._wait(run, 0.3)
            total = self._clock() - t0
            done = [j for j in jobs if j["state"] == "done" and j.get("links")]
            failed = [j for j in jobs if j["state"] == "failed"]
            waiting = [j for j in jobs if j["state"] in ("queued", "running")]
            reasons = sorted({_short(j.get("error") or "", 60) for j in failed})
            status = PASS if len(done) == len(ids) else (WARN if done else FAIL)
            self._add(run, "queue", "queue.finish", "全件の完了", status,
                      "%d/%d件完了・失敗%d・未完了%d（全体%.1f秒%s）" % (
                          len(done), len(ids), len(failed), len(waiting), total, "・" + " / ".join(reasons) if reasons else ""),
                      "%d/%d件完了" % (len(ids), len(ids)), hint_key="queue.finish")
            spans = sorted((j["started"], j["finished"]) for j in jobs if j.get("started") and j.get("finished"))
            overlap = sum(1 for a, b in zip(spans, spans[1:]) if b[0] < a[1] - 0.05)
            per = [round(j["finished"] - j["started"], 2) for j in jobs if j.get("started") and j.get("finished")]
            self._add(run, "queue", "queue.sequential", "1件ずつ順に解析", PASS if spans and not overlap else (SKIP if not spans else FAIL),
                      "重なり%d件・1件あたり 平均 %.2f秒・最大 %.2f秒" % (overlap, sum(per) / len(per) if per else 0, max(per) if per else 0),
                      "重なり0件", hint_key="queue.sequential")
            self._add(run, "queue", "queue.memory", "解析中のメモリ（コンテナ最大）", PASS if peak else SKIP,
                      "%d MiB" % round(peak) if peak else "取得できません")
        finally:
            if tab is not None:
                try:
                    rb.probes.tab_closed(tab)
                except Exception:
                    pass
                self._close_quiet(tab)
            self._leave(run, "queue")

    def _capacity_wait(self, run, tab, path, virtual):
        """Wait for a capacity page to finish loading; milliseconds, or None when it did not in time."""
        started = self._clock()
        if virtual:
            return self._wait_loaded(run, tab, path, timeout=15.0, since=started)
        limit = self._clock() + CAPACITY_SITE_LOAD_S
        while self._clock() < limit:
            self._guard(run)
            info = self.rb.selftest_info(tab)
            if info.get("dcl") or (not info["loading"] and info["url"] and info["url"] != "about:blank"):
                return int((self._clock() - started) * 1000)
            self._wait(run, 0.1)
        return None

    def _capacity_summary(self, run, stats, base_mib, limit_mib, available, user_tabs, stopped):
        try:
            current = self.rb.max_tabs()
        except Exception:
            current = None
        if stats["per_tab"] is None:
            self._add(run, "capacity", "capacity.summary", "推奨タブ枚数", SKIP,
                      stopped or "1ページも測れませんでした")
            return
        rec = recommend_tabs(stats["per_tab"], base_mib, limit_mib, available, open_tabs=user_tabs,
                             limit_percent=self._guard_percent(), min_available_mib=self._guard_available_mib())
        tabs = rec["tabs"]
        text = "1ページあたり 平均 %d MiB・最大 %d MiB → 推奨タブ枚数 %d枚（現在の上限 %s枚）" % (
            round(stats["avg"]), round(stats["max"]), tabs, "?" if current is None else current)
        detail = "（算出: 1ページ %d MiB%s × 余裕 %.2f / コンテナ基準で %s枚・空きメモリ基準で %s枚）" % (
            round(stats["per_tab"]), "（実サイトの最大）" if stats["basis"] == "real" else "（重い仮想ページの最大）",
            CAPACITY_MARGIN, "-" if rec["by_container"] is None else rec["by_container"],
            "-" if rec["by_host"] is None else rec["by_host"])
        status = PASS
        if stopped:
            status = WARN
        elif current is not None and current > tabs:
            status = WARN
        hint_key = "capacity.stopped" if stopped else "capacity.recommend"
        self._add(run, "capacity", "capacity.summary", "推奨タブ枚数", status,
                  text + detail + ("。" + stopped if stopped else ""),
                  "現在の上限 <= 推奨枚数", hint_key=hint_key)
        run.info["tab_reco"] = {"tabs": tabs, "avg_mib": round(stats["avg"], 1), "max_mib": round(stats["max"], 1),
                                "per_tab_mib": round(stats["per_tab"], 1), "basis": stats["basis"],
                                "current": current, "pages": stats["n"], "partial": bool(stopped)}
        try:
            self.rb.tab_reco = dict(run.info["tab_reco"], at=int(self._wall()))
        except Exception:
            pass

    @staticmethod
    def _guard_percent():
        import remote_browser
        return remote_browser.MEM_LIMIT_PERCENT

    @staticmethod
    def _guard_available_mib():
        import remote_browser
        return remote_browser.MIN_AVAILABLE_KIB / 1024

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
            dcl = None
            loaded = None
            while self._clock() - t < SITE_LOAD_LIMIT_S:
                self._guard(run)
                info = rb.selftest_info(tab)
                elapsed = self._clock() - t
                if early is None and elapsed >= 3.0:
                    early = info["loading"] or not info.get("dcl")
                if dcl is None and info.get("dcl"):
                    dcl = int(elapsed * 1000)
                if not info["loading"] and info["url"] and info["url"] != "about:blank":
                    loaded = int(elapsed * 1000)
                    if dcl is None:
                        dcl = loaded           # no DOMContentLoaded event seen: the finished load implies it
                    break
                if dcl is not None and elapsed - dcl / 1000.0 >= SITE_LOAD_GRACE_S:
                    break                      # ads and trackers can keep a page "loading" for ever
                self._wait(run, 0.1)
            if early is None:
                early = False
            meta = rb.page_meta(tab, wait=0)
            challenge = meta.get("challenge") is True
            if dcl is not None:
                value = "DOMContentLoaded %d ms" % dcl
                value += (" / 完全な読み込み %d ms" % loaded) if loaded is not None else " / 完全な読み込みは未完了（広告など）"
            else:
                value = "%d秒でDOMContentLoadedが来ませんでした" % int(SITE_LOAD_LIMIT_S)
            value += "（3秒時点: %s）" % ("まだ読み込み中" if early else "読み込み済み")
            if challenge:
                self._add(run, sec, base + "open", label + " 開く", WARN, value + " / 確認ページ（チャレンジ）を検出",
                          "チャレンジなし", hint_key="site.challenge", extra=extra)
                return
            status = FAIL if dcl is None else rate(dcl, SITE_OPEN_WARN_MS)
            self._add(run, sec, base + "open", label + " 開く", status, value,
                      "DOMContentLoaded <= %d ms" % SITE_OPEN_WARN_MS,
                      hint_key="site.open", extra=extra)
            self._step(run, "%s のリーダー抽出を測っています" % item["host"])
            self._site_measure(run, base + "reader", label + " リーダー", "site.reader", extra,
                               lambda: self._m_reader(tab))
            self._site_measure(run, base + "mirror", label + " ミラー", "site.mirror", extra,
                               lambda: self._m_mirror(tab))
            self._first_load_check(run, tab, sec, base + "first_load", label + " 初回読み込みの内訳", "site.first_load", extra)
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
        fps, kb, frames, moved = self._measure_stream(run, tab, SITE_FPS_SECONDS, scroll=True)
        if moved == 0:
            # nothing could be scrolled (a page that fits the screen): a still page makes no frames, not a failure
            return PASS, "%.1f fps（%d フレーム）/ %d KB / スクロールできないページのため動きなし" % (fps, frames, kb), \
                ">= %g fps（動きがあるとき）" % FPS_WARN
        return rate(fps, FPS_WARN, low=True), "%.1f fps（%d フレーム）/ %d KB / スクロール %d 回" % (fps, frames, kb, moved), \
            ">= %g fps（スクロール中）" % FPS_WARN

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
                "summary": self._summary(run), "tab": run.tab if run.phase in ("client", "swipe") else None,
                "viewport": {"width": run.options["viewport"][0], "height": run.options["viewport"][1],
                             "dpr": run.options["viewport"][2]},
                "swipe_kind": run.swipe["kind"] if run.swipe else None,
                "client": run.client, "history": [self._summary(r) for r in reversed(self._history)]}
        if full:
            data["info"] = dict(run.info)
            data["device"] = dict(run.device) if run.device else None
            data["log"] = list(run.log)
            if run.swipe:
                data["swipe"] = {"modes": run.swipe["modes"]}
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
            "[Source] cgroup=%s | container-stats=%s" % (info.get("cgroup") or "?", info.get("cgroup_sources") or "?"),
            "[Live] transport=%s fps=%s" % (
                ({"poll": "polling"}.get(info.get("live_transport"), info.get("live_transport")) or "none")
                if run.client == "received" else "n/a",
                _fmt(info.get("live_fps")) if run.client == "received" else "-"),
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
        if run.swipe and run.swipe["modes"]:
            lines.extend(self._swipe_report_lines(run))
        lines.append("")
        lines.append("## debug log (last %d lines: %s; times are UTC)" % (LOG_TAIL, "/".join(LOG_CATEGORIES)))
        lines.extend(run.log or ["(none)"])
        lines.append("== end ==")
        return "\n".join(lines)


def _swipe_timeline(step, limit):
    events = step["events"]
    out = ["      +%dms %s %s" % (ev["t"], ev["e"], ev["d"]) for ev in events[:limit]]
    if len(events) > limit:
        out.append("      … (他 %d 件)" % (len(events) - limit))
    return out


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
