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
settings-routes / display-reader・mirror・tiles・live / mirror-tap-link（クリックを止めたiOS相当のタップ・遷移後スクロール0）/
mirror-menu-toggle（`href="#"` と自ページ宛の2種類のトグルが再読み込みにならない）/ mirror-dynamic / mirror-security（CSP・スクリプト不実行）/
reader-cards（ラベル・サイズあり/なし、ページ送りチップ）/ gesture-flick-mirror・tiles、gesture-close-tab（Chromiumのみ: CDPタッチが必要）/
reload-reattach / challenge-wait-fast・mirror（確認ページはライブに切り替えず「安全性の確認中」を表示、リダイレクト後 6 秒以内に本来のページを選んだ表示で出す・履歴は増えない）/ history-skip-and-first-page（確認ページ・トークン付き重複の履歴を1回で飛ばして戻る、最初のページでは案内のみでアプリの画面は変わらない）/ swipe-diagnostic・swipe-manual（Chromiumのみ: 設定›診断›自己テストの「スワイプだけ確認」「手動で確認」）/
live-sse-fallback（MJPEGが1バイトも届かない状態を再現: 2.5秒後にSSEへ切替わりフレームが届く・経路を記憶して再読み込み後はMJPEGを触らない）/ live-polling-fallback（MJPEGもSSEも届かない: ポーリングで表示し理由を出す）/ copy-fallback（`navigator.clipboard` が無くても隠しtextarea＋execCommandでコピー）/
selftest（設定›診断›自己テストを実行して、全項目が終わり、レポートに NAS・端末・ログが入り、URLがホスト名だけになり、テスト用タブが残らない）。

### 実機の自己テスト（v0.1.44）
GitHubのE2Eでは実機のNAS・iPhone・実サイトを測れないため、アプリ自身に「設定›診断›自己テスト」を持たせている。
ボタン1つでHub（`selftest.py`）がNASの環境・NASブラウザの起動・内蔵テストページ（`https://selftest.example.com/` をRAMから返す仮想サイト）・任意の実サイトを測り、
続けて端末（iPhone等）がリーダー描画・ミラー読み込み・`wireMirrorDoc` のpointer/click到達・ミラー差し替え・タイル・ライブ受信・Hubとの往復時間を測って `POST /api/selftest/client` で返す。
判定は常にHub側。「結果をコピー」の本文（日本語＋English、秘密なし、URLはホスト名のみ、デバッグログ末尾50行つき）を開発者へ貼ると、実機でしか出ない不具合を直せる。
結果はHubのRAMに直近5件だけ残り、ディスクへは書かない。上限は4分で、中止・エラー時もテスト用タブと一時的に入れたデバッグログを元に戻す。

未対応: 長押しのhosterプローブ（`rapidgator.net` 等の実ドメインが必要でフィクスチャに向けられないため）。

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
