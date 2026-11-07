# テスト

## 単体・安全性テスト
`scripts/verify.sh`（構文チェック、`python3 -m unittest discover -s tests`、node構文チェック、安全性grep）。リリース前に必ず実行する。

## E2E（iPhone相当の実ブラウザ）
手で見つかった不具合（iPhone Safariでミラーのタップが届かない、`<a href="#">` のメニューが再読み込みになる、
ページ遷移後に古いスクロール位置が残る、アプリ再読み込みでNASタブが増える、確認画面からの復帰が遅い、など）を
出荷前に機械的に検出するためのスイート。`tests/e2e/` にあり、`verify.sh` の単体テスト探索には含まれない。

```
scripts/e2e.sh                                   # chromium + webkit。成果物は ./e2e-artifacts
scripts/e2e.sh --engines chromium --only mirror  # 名前に "mirror" を含むシナリオだけ
python3 tests/e2e/run.py --engines chromium,webkit --out /tmp/e2e
```

構成（すべてローカル。外部へは出ない）:

| 役割 | 実体 |
|---|---|
| 手元の「iPhone」 | Playwright の `chromium`（390x844 DPR3 タッチ）と `webkit`（`devices["iPhone 13"]`）。入力は常にタッチ（`touchscreen.tap` / CDP タッチ） |
| Hub | 本物の `webapp.Handler`（同一プロセス）。コンテナ層だけ `tests/fake_docker.py` |
| NASのブラウザ | 本物のヘッドレス Chrome。`RemoteBrowser` 自身の CDP クライアントで操作。`env.blocked`（`{"mjpeg","sse"}`）に入れた経路は、リクエストを受けても応答を始めない（溜め込むプロキシの再現） |
| 閲覧先 | `tests/e2e/fixtures/*.html` を配る自己署名 https サーバー。`a.example.com` などは **NAS側Chromeだけ** `--host-resolver-rules` と `--ignore-certificate-errors` でこのサーバーへ向ける（アプリのURL検証は本番と同じ） |

- WebKit が入っていない環境（この開発環境など）では webkit は「SKIP（browser not installed）」になり、終了コードは変わらない。
  `python -m playwright install --with-deps webkit` で入れられる。CI（`.github/workflows/e2e.yml`）では chromium と webkit の両方を入れて実行する。
- NAS側Chromeは `--nas-chrome` / `$E2E_NAS_CHROME` / PATH上の `google-chrome` の順で探し、無ければ Playwright の chromium を使う。
- 失敗したシナリオは `--out` に `<engine>-<name>-fail.png`（スクリーンショット）と `-fail.txt`（例外・コンソールエラー・NASタブ・フィクスチャへのアクセス数）を残す。
- シナリオごとにブラウザの `console.error` / `pageerror` を集め、許可リスト（`harness.CONSOLE_ALLOW`）以外があれば失敗にする。
  わざと起こすエラー（CSPのブロック等）は `ph.take_errors(...)` で取り除く。

### シナリオ一覧
pt-settings-ping・pt-open-page・pt-back-forward・pt-flick（Chromiumのみ）・pt-redirect・pt-cookie-first-party・pt-download-page・pt-off-refuses・pt-menu-switch（v0.1.57 パススルー: アプリを http://localhost:P、待ち受け（オリジンB）を http://localhost:Q で動かし、上流はフィクスチャのhttpsサーバー。テスト専用の上流指定は `server.pt_test_*` 属性で、本番の起動経路では設定されない。WebKitは未検証）/
settings-routes / display-reader・mirror・tiles・live / mirror-tap-link（クリックを止めたiOS相当のタップ・遷移後スクロール0）/
mirror-menu-toggle（`href="#"` と自ページ宛の2種類のトグルが再読み込みにならない）/ mirror-dynamic / mirror-security（CSP・スクリプト不実行）/
reader-cards（ラベル・サイズあり/なし、ページ送りチップ）/ gesture-flick-mirror・tiles、gesture-close-tab（Chromiumのみ: CDPタッチが必要）/
reload-reattach / challenge-wait-fast・mirror（確認ページはライブに切り替えず「安全性の確認中」を表示、リダイレクト後 6 秒以内に本来のページを選んだ表示で出す・履歴は増えない）/ history-skip-and-first-page（確認ページ・トークン付き重複の履歴を1回で飛ばして戻る、最初のページでは案内のみでアプリの画面は変わらない）/ swipe-diagnostic・swipe-manual（Chromiumのみ: 設定›診断›自己テストの「スワイプだけ確認」「手動で確認」）/
live-ws（v0.1.68: 動くページ `fixtures/anim.html` でWebSocket経路が使われ3 fps以上・フレームは blob 画像で object URL が増え続けない・速度表示に「ライブ(WebSocket)」・画面を隠すとソケットとHubの配信が止まり、戻すと開き直す）/ live-sse-fallback（WebSocketのアップグレードに誰も答えない状態: 2.5秒後にSSEへ切替わりフレームが届く・失敗を記憶して再読み込み後はWebSocketを触らない）/ live-ws-refused（アップグレードが即エラー: 2回続けて失敗した時点でSSEへ）/ live-mjpeg-fallback（WebSocketもSSEも届かない: MJPEG）/ live-polling-fallback（どれも届かない: ポーリングで表示し理由を出す）/ live-ws-proxy（v0.1.68: ハーネスの `BufferingProxy` ＝普通の応答を終わるまで溜め込み、WebSocketのアップグレードだけ素通しするテスト専用のリバースプロキシ越しにアプリを開く。MJPEG/SSEは届かない（SSEが1.5秒で何も返さないことも確認）のに、WebSocketで3 fps以上のライブ表示が動く＝実機の症状の再現）/ ws-security（Python製のRFC 6455クライアントで直接叩く: 別サイトのOrigin 403・Origin無し403・セッション無し401・存在しないタブ404、正しいOriginならJPEGが届き、マスク無しフレームは1002で閉じられる）/ copy-fallback（`navigator.clipboard` が無くても隠しtextarea＋execCommandでコピー）/
selftest（設定›診断›自己テストを実行して、全項目が終わり、レポートに NAS・端末・ログが入り、URLがホスト名だけになり、テスト用タブが残らない） / first-load（v0.1.54: 読み込みが終わらない重いページでも本文がミラーに先に出る・段階別の所要時間（作成/遷移/DOM構築/コピー作成と端末側の作成応答〜全体）が揃う・広告等のブロック数が数えられる・最初のコピーが部分コピーで後から全体に置き換わる）。

### v0.1.68 WebSocket（ライブ表示）の単体テスト
`tests/test_ws_hub.py`（ハンドシェイク: RFC例 `dGhlIHNhbXBsZSBub25jZQ==` → `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`・フレームの符号化（7/16/64ビット長）・マスク付きクライアントフレームの復号（RFC 5.7の `Hello`）・マスク無し=1002・4KiB超=1009（本体を読む前）・予約ビット/不正な制御フレーム・ping/pong・close往復・分割/バイナリ/その他の文字=1002/1003/1008・読まない端末で送信が止まり切断・無音の端末の切断・同時接続の上限と最古の切断）、`tests/test_remote_browser.py` の `WsStreamTests`（生のJPEGを1書き込みずつ・遅い書き込み側には最新の1枚だけ・ping・`alive()` で終了・`on_start` はタブが無ければ呼ばれない）、`tests/test_webapp.py`（101の前の拒否: 401/403/404/400、フレーム・ping・close・プロトコル違反の符号、タブを閉じると終わる、`/` のCSPだけにwss/wsが入る）。
E2Eの `live-ws-proxy` は、実機の症状（プロキシが長い応答を溜め込む）をそのまま再現した回帰テストです。

### 要求数の予算と楽観的UI（v0.1.59）
budget-open-mirror / budget-open-reader（アドレスバーからページを開くと、本文が端末に出るまでの `/api/` 要求が3件以内。`page.on("request")` で数え、ページ内の10msポーラーが本文を見つけた瞬間の件数で切る。長いポーリング・画像・firstloadの報告は数えない。ミラーはリンクのタップも検証し、metaの長いポーリングを除いて3件以内）/ budget-idle-state（Downloads画面で何も起きない10秒間の `/api/state` 応答が1件以内）/ optimistic-switch（スイッチを押して200ms後、`page.route` で1.5秒止めた保存の返事を待たずに見た目と `state.settings` が変わり保存中の印が付く・500で失敗させると元に戻りエラーが出る）。selftest シナリオは新しい「操作の応答」セクション（画面切り替え・スイッチの反応・待機30秒の通信量・ページを開く通信回数）が全部合格することも確認する。

### v0.1.60〜v0.1.62 のシナリオ
- **v0.1.61**: bar-single-row（Webタブの上部が1段・⋮は1つ・右下の再読み込みボタン無し・旧2段メニューの項目がすべて1つのメニューにある）/ omnibox-domain（入力していないときはドメイン、フォーカスでURL全体）/ bar-collapse-mirror・bar-collapse-reader（スクロールで上部バーと下のタブバーが隠れ、戻すと再表示・隠れても表示サイズは変わらない）/ link-tap-feedback・reader-link-feedback（リンクをタップした瞬間の強調と進行線）/ app-shell-cache（Chromiumのみ: Service Workerがアプリ本体だけをキャッシュし、版が変わると捨てて1回だけ再読み込み、「アプリのキャッシュを消す」で解除）。
- **v0.1.62**: home-tabs-recent（ブラウズホームに開いているタブのカードと最近見たページ・タップで切り替え・×でNASのタブごと閉じる・履歴の消去・クリップボードはタップするまで読まない・拒否時はアドレスバーへフォーカス）/ links-paste（貼り付けボタン＋件数＋「追加してJD2へ送る」・各リンクの送信中/失敗の表示と再送信）/ downloads-summary（`summarizeDownloads` の数値・ページの集計・失敗の絞り込み・左スワイプで一時停止/再開と消す・追加の要求が無い。JD2が動かないharnessなので一覧は注入。スワイプはChromiumのみ）/ settings-jd2-layout（セクション順・折りたたみは「詳細・ログ」1つで閉じている・旧ページの全コントロールのIDが残っている・エラーは1行で「詳しく」で展開・状態ごとに主ボタンは1つ）/ site-display-memory（登録可能ドメイン単位の記憶: 別サブドメインのページが最初からリーダーで開き、tabsリクエストに `first: reader` が載る・別ドメインは既定・「忘れる」・旧データの互換）/ site-display-pt（パススルーの記憶: 次のページは枠に直接開きNASのタブは作らない・使えないときは既定・忘れる。WebKitは未検証）/ toast-above-bar（430x873で通知が下のタブバーに重ならない・アプリの進行線は300ms未満では出ず、遅い取得で出て終わると消える）。

### iPad・PC のシナリオ（v0.1.63）
`@scenario(..., device="phone"|"ipad"|"desktop")` で端末を選ぶ（`tests/e2e/run.py` の `device_options`）。`phone` = iPhone（従来）、`ipad` = タッチ 820x1180（Chromium は `is_mobile`・`has_touch`・DPR2、WebKit は `devices["iPad Pro 11"]` の寸法だけ 820x1180 に）、`desktop` = マウス＋キーボード 1280x800・タッチなし。レイアウトは入力方法（`(pointer: coarse)` / `(pointer: fine)`）で決まるので、エンジンのタッチ模擬が coarse を返さないとき（Linux の WebKit など）は `ph.require_touch()` / `ph.require_mouse()` が SKIP にする（偽の合否を出さない）。`ph.click(x, y, button)`・`ph.press("Alt+ArrowLeft")` はマウス・キーボード入力。WebKit は未検証。
- **ipad-layout**（820x1180・1180x820・Split View 500/600/700）: 上部が1行・全ボタンあり・指の大きさ（44px以上）・横スクロール無し・メニューの行・ダウンロードの絞り込み・設定の2列・スクロールでバー（タブ列ごと）が隠れる・NASの表示サイズが変わらない・Split View はスマホ型。**ipad-keys**: ハードウェアキーボード（下の共通手順）。
- **desktop-keys**: Alt+←/→・⌘[ / ⌘]・枠の中にフォーカスがあるときの Alt+←・最初のページでは通知だけでタブから出ない・Ctrl/⌘+L・`/`・Esc・アドレス入力中は奪わない・Ctrl+R / F5 が NAS ページだけを再読み込みしてアプリは再読み込みしない。**desktop-mouse-nav**: マウスのボタン4/5（`mousedown`/`mouseup`/`auxclick` をページへ配る。Chromium の CDP の back ボタンはDOMイベント無しでブラウザ履歴へ行くので使えない）がキャンセルされ1歩進む（アプリ文書とミラー文書の両方）・ホイールの横スワイプで戻る/進む・縦に少し斜めは何もしない・横スクロールする表の上と横に長いページでは動かさない。**desktop-link-menu**: ミラーのリンクの右クリックメニュー（本文では出ない・3項目・Escで閉じる・コピー/リンクに追加/新しいタブで開く）。**desktop-layout**: 1280・1920 でバー1行・ツールチップ・ミラーが1600pxで中央・ホイールでスクロール・マウスではバーが隠れない・ダウンロード/リンク/設定の2列（1000pxでは1列）。**reader-wide**: 1920 でリーダーの列が文字サイズ13〜26で780px以下・中央・ヘッダーが列に揃う。**downloads-offline-waiting**: 「Offline」だけは待機・失敗の語は失敗（「File not found (Offline)」も）・「待機」「失敗」の絞り込み。**recent-cleared-on-logout**: ログアウトで最近見たページが消え、次のログインでも空。
- 既存の **bar-collapse-mirror** の最後（広い画面でバーを残す）は、このコンテキストがタッチなので「広いタッチ画面も隠れる」に変えた（マウスで隠れないことは desktop-layout）。**downloads-summary** は「Offline」を待機に数える新しい判定に合わせた。

### 初回読み込みの計測（v0.1.54）
`python3 tests/e2e/bench_firstload.py --runs 3 [--display mirror|reader] [--cdp-delay-ms 120] [--stats-delay 1.8] [--setting rb_spare_tab=false]` は、タップから本文・先頭画像・全体が端末に出るまでをページ内の20msポーラーで測り、中央値を出します（最初の1回は捨てます）。`--cdp-delay-ms` はNASのChromiumの応答遅延、`--stats-delay` は `docker stats` の所要時間を模擬します（実機のネットワークは再現しません）。このスクリプトとharness.py・fake_docker.pyを古い版に置けば「変更前」も同じ方法で測れます。

### v0.1.65 のシナリオと計測
- `probe-queue-burst`: ミラーで12回の長押し（`startProbe`）を続けて行う。各押下は2秒未満で受け付けられ、「リンク解析 n/12」のピルが出て、12件すべてが `done` になり、プローブ用ページは同時に1つ以下（`RemoteBrowser._probe_targets` を50msごとに見る）、ジョブの実行区間が重ならず、シートに12行ある。エラートーストなし。
- `probe-queue-cancel`: 同じリンクの重複追加（`existing`）、`/api/rb/probe/cancel` で待ちが残らない、タブを閉じるとそのタブのジョブが消える。
- `tab-limit-setting`: 設定›ブラウザ›「NASブラウザのタブの上限」を5にして再読み込み後も保たれ、5枚まで開けて6枚目は「NASブラウザのタブは最大5枚です（設定で変更できます）。不要なタブを閉じてください」で断られる。推奨値（自己テストが保存した `tab_reco`）を「推奨値にする」で適用できる。
- `selftest` に「タブの容量」（5ページ分の行・「1ページあたり 平均 … 推奨タブ枚数 …」・`rb.tab_reco`）と「リンク解析キュー」（10件が1件ずつ完了）の確認を追加。ハーネスは `Env.model_container_memory(True, limit_mib=2048)` で実Chromeの使用メモリ（PSS）を偽の `docker stats` に反映する。
- `python3 tests/e2e/bench_probe.py --mode old|new --count 12 [--base-percent 55 --per-target-percent 8.5 --hold-ms 700]` は、長押し12回の所要時間・成功数・失敗理由・同時に開いたプローブ数・無関係な `/api/session` 要求の待ち時間を測る。`old` は v0.1.63 のコード（保持型リクエスト）用で、その版にこのスクリプトと `fixtures/probe.html`・harness の `/go/N` を置いて実行する。コンテナ使用率は `基準% + 1件あたり% × 同時プローブ数` で模擬する。

### 自己テスト「操作の応答」（v0.1.59）
端末側の測定の最後に、アプリ自身の応答を測る（約40秒）。`client.route`（`#/downloads`・`#/links`・`#/home`・`#/settings/…` へ hash を変えて対象の画面が見えるまで。通信不要のはずなので 250ms 超で注意）/ `client.toggle`（設定のスイッチ `rb_link_boxes` を押して見た目と状態が変わるまで 100ms 以内が合格、Hubの確定までの時間も併記、測定後に元へ戻す）/ `client.idle`（Downloads画面で30秒何もせず、`/api/state` の応答が3回以内で合格・6回超で失敗、全通信が15回超で注意）/ `client.open_rtt`（内蔵テストページを「ビューと最初のコピー付きの navigate」で開き、文書まで取得するのに要した要求数が3以内で合格）。判定は `selftest.client_evaluate`、レポートの「## 操作の応答」に出る。
`tests/e2e/bench_firstload.py` は要求数ではなくミリ秒を測る（`--cdp-delay-ms` でNASの応答遅延を模擬）。

### 実機の自己テスト（v0.1.44）
GitHubのE2Eでは実機のNAS・iPhone・実サイトを測れないため、アプリ自身に「設定›診断›自己テスト」を持たせている。
ボタン1つでHub（`selftest.py`）がNASの環境・NASブラウザの起動・内蔵テストページ（`https://selftest.example.com/` をRAMから返す仮想サイト）・任意の実サイトを測り、
続けて端末（iPhone等）がリーダー描画・ミラー読み込み・`wireMirrorDoc` のpointer/click到達・ミラー差し替え・タイル・ライブ受信・Hubとの往復時間を測って `POST /api/selftest/client` で返す。
判定は常にHub側。「結果をコピー」の本文（日本語＋English、秘密なし、URLはホスト名のみ、デバッグログ末尾50行つき）を開発者へ貼ると、実機でしか出ない不具合を直せる。
結果はHubのRAMに直近5件だけ残り、ディスクへは書かない。上限は6分（v0.1.65: 「タブの容量」「リンク解析キュー」を足したため）で、中止・エラー時もテスト用タブと一時的に入れたデバッグログを元に戻す。

v0.1.68: 動くページ（`/anim`）の動く箱は、以前は記事の末尾（800x400の画像3枚の下）にあり、スマホ大の表示範囲の外だったため、読み込み中の約21フレームが出たあとスクリーンキャストは1枚も送らなかった。「ライブ表示（スクリーンキャスト） 7.0 fps（21フレーム / 3秒）」はその21フレームの数字で、端末側のライブ受信の測定（ポーリング 0.3 fps ＝ 最初の1枚だけ）も止まったページを測っていた。今は箱が表示範囲の上端に固定され（`position:fixed`）、40ms間隔の変化が続くので、どの経路のfpsも「動くページの実際の速度」になる（このE2E環境で約25 fps。NASの実機では下がる可能性が高い＝それが本来の測定値）。

長押しのhosterプローブそのもの（実ドメイン）は未対応だが、v0.1.65 から「リンク解析キュー」の確認が仮想ページ（`selftest.example.com/go/N` が偽の Rapidgator ファイルページへ転送する）で行われる。

### スワイプの確認（v0.1.52）
「スワイプがおかしい」を実機で再現するために、自己テストにスワイプの確認を持たせている（`selftest.py` が判定、`web/app.js` の `swipeAuto` / `swipeManual` が端末側）。
iOSの端スワイプそのものは合成できないが、アプリ側の処理は合成入力で試せる。**自動**（通常の自己テストの「スワイプ（自動）」、または「スワイプだけ確認」）は、
Hubが内部テスト用タブに A → 確認ページ → A?__cf_chl_tk → B → C の履歴を作り、端末がそのタブを実際のタブ画面で開いて、ミラー・リーダー・高速の各表示で
`history.back()`（端スワイプ相当）・最初のページでの戻る・中央の左右フリック（`new Touch`/`TouchEvent`、作れなければ同じ内部ハンドラを直接呼ぶ）・ゆっくりした動き・画面端からの動き・
`history.forward()`・ツールバーを下へ引く/離す（タブを閉じる処理は記録だけで実行しない）を流し、NASのページ・route・popstate・stepHistory・履歴の長さを記録する。
**手動**（「手動で確認」）は同じ記録・判定・レポートで、バナーの指示に従って実際の指でスワイプし「できた／できない・違う動き／スキップ」を答える。
結果は通常の自己テストと同じレポート（`## スワイプ（自動）` / `## スワイプ（手動）` とタイムライン）に入り、「結果をコピー」で貼れる。`gestureTrace(event, data)` は記録中以外は何もしない。

### シナリオの追加方法
1. 必要なら `tests/e2e/fixtures/<name>.html` を追加する（`/<name>.html` で配信される。ページ内スクリプトは
   `fetch("/__beacon?e=<event>&p=<detail>")` でサーバーに合図でき、`env.fx.beacon_count(event, detail)` / `env.fx.count(path)` で確認できる）。
   画像は `/img/<幅>/<高さ>.png?c=rrggbb` が使える。
2. `tests/e2e/scenarios.py` に関数を足す:

```python
@scenario("my-case", engines=("chromium", "webkit"), display="mirror")   # display = 設定のRB既定表示
def my_case(ph):
    tab = ph.open("/article.html")                       # オムニボックスから開く
    ph.wait(lambda: "E2E" in ph.mirror_text(), 15, "mirror content")
    pt = ph.mirror_point("doc=>doc.querySelector('a[data-href]')")
    ph.tap(pt["x"], pt["y"])                              # タッチ。clickは使わない
    ph.wait(lambda: ph.env.nas_url(tab).endswith("/article2.html"), 10, "navigated")
```
   WebKit で再現できない操作は `raise harness.Skip("理由")`、または `engines=("chromium",)` にする。
3. `scripts/e2e.sh --only my-case --engines chromium` で確認し、わざとアプリを壊して（例: `web/app.js` の該当処理を消して）失敗することも確かめる。

### リリースワークフローへ
リリースブランチのワークフローでは、`scripts/verify.sh` の直後に次の1行を足す:

```
      - run: python -m pip install playwright==1.63.0 && python -m playwright install --with-deps chromium webkit && bash scripts/e2e.sh --engines chromium,webkit --require-engines --out "$GITHUB_WORKSPACE/e2e-artifacts"
```
