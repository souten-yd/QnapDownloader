# パススルー表示（v0.1.57）

ページの描画・スクロール・タップは **iPhone自身のSafari** が行い、そのページが出すすべてのリクエストは **NASのHub経由** で外へ出ます。サイトから見えるIPアドレスはNASのものになるため、IPに紐づくファイル共有サイトのダウンロードリンクを、同じIPのJD2で落とせます。NASブラウザ（Chromiumを画像で見る方式）より軽く、本物のDOM/JS/ネイティブスクロールを使えます。**実機（iPhone）は未検証です。**

## 仕組み

```
iPhone Safari（ホーム画面アプリ）
 ├─ オリジンA  https://<nas>.<tailnet>.ts.net/        管理画面（17864）。iframeを置く
 └─ オリジンB  https://<nas>.<tailnet>.ts.net:8443/   パススルー専用（17865）。Service Workerが全リクエストを書き換え
                    └─ /p/<s|h>/<host>[:port]/<path>?<query>  → Hub → 公開サイト
```

1. アプリが `POST /api/pt/ticket`（ログイン済み・CSRF）で使い捨てチケット（32バイト、60秒、RAM）を受け取り、iframe を `B/pt/enter?t=…&u=…` で開く。
2. `/pt/enter` がチケットを使い切り、専用Cookie `qd_pt`（HttpOnly・Secure・SameSite=Lax・RAMのみのセッション。アイドル12時間／最大7日）を設定して Service Worker を登録、`/p/…` へ移る。
3. Service Worker（`web/pt-sw.js`）が、同一オリジンの `/p/` 以外のパスと、別オリジンへの全リクエストを `/p/<s|h>/<host>/…` に書き換え、`X-QD-PT-Top`（いま開いているサイト）を付ける。
4. Hub（`passthrough.py` / `pt_proxy.py`）が公開サイトへ取りに行き、レスポンスヘッダ（`Location`、`Set-Cookie`、CSP/XFO など）を書き換えて返す。HTMLには小さなヘルパー（`web/pt-helper.js`）を先頭に注入する。
5. ヘルパーがアドレス・タイトル・フリックを `postMessage`（アプリのオリジンを固定）でアプリに報告し、アプリからの戻る／進む／再読込／移動を受ける。履歴の一覧はアプリが持つ。

URLの対応は Python（`pt_core.py`）と Service Worker で同じ規則で、テストで一致を確認しています。

## 安全設計

- **別オリジン**: 待ち受けは 127.0.0.1:17865 だけ（Tailscale Serve 経由）。パススルーのルート（`/pt/*`、`/p/*`、静的ファイル）だけを返し、管理画面・API・ログインは一切返さない。Hostは `*.ts.net` または localhost/127.0.0.1 のみ。設定OFFの間は待ち受け自体を起動しません。
- **認証**: 上記のチケット→専用Cookie。管理画面のCookie（`qdl_session`）はポートを区別せず送られますが、パススルー側は読まず、無視します。「全端末ログアウト」とHub再起動でセッションは消えます。
- **SSRFガード**: 公開のhttp(s)ホストだけ（ポートは80/443）。ループバック・RFC1918・リンクローカル・CGNAT・ULA・マルチキャスト・NAS自身のアドレス・メタデータIP・`*.ts.net`・Hub自身の名前は拒否。DNSで解決したIPを検査し、そのIPへ接続（SNI/Hostはホスト名）。リダイレクトは1ホップごとにガードを通り直します。本文は16 MiBまで、同時上流接続は12まで、タイムアウトあり。
- **Cookie**: サイトのCookieは **セッションごと・サイトごと** にHubのRAMへ保持（iPhone側のCookieには入れない）。送るのは、Service Workerが付けた `X-QD-PT-Top` が同じサイトのときだけ（ファーストパーティのみ）。「パススルーのCookieを消す」で全消去。
- **アプリ側**: iframeは `sandbox`（トップ遷移・ポップアップなし）、`referrerpolicy=no-referrer`、アプリのCSPの `frame-src`/`connect-src` にオリジンBを入れるのは設定ONのときだけ。ダウンロードのJD2引き渡しは、アプリ自身の確認ダイアログを経由します（サイトのJSが勝手にリンクを追加することはできません）。
- **デバッグログ**: カテゴリ「pt」（URLのクエリは載せない）。

## 設定

設定 › ブラウザ › パススルー（既定OFF）。ONにするとHubが 17865 を待ち受け、アプリを読み込み直します。そのうえで、NASに **2本目の** コマンドを1回実行します。

```
tailscale serve --bg --https=8443 http://127.0.0.1:17865
```

「接続を確認」でアプリからオリジンBの `/pt/ping` を呼べます。使えるのは HTTPS の `*.ts.net` で開いたアプリだけです（それ以外では表示メニューのパススルーが無効になり、理由が出ます）。

## 制限（正直に）

- **すべてのサイトが1つのオリジン（B）を共有します。** 悪意のあるサイトは、同じオリジンの localStorage/IndexedDB や JavaScript が設定したCookieを、別のサイトのものも含めて読み書きできます（NASブラウザほどは隔離されません）。信用できないサイトには使わないでください。サイト側のJSは管理画面のオリジンでは動かず、管理APIやログインCookieには触れません。
- **ファーストパーティ判定は努力目標です。** `X-QD-PT-Top` は Service Worker が付けますが、悪意のあるページが Service Worker を外すなどして偽装し得ます。その場合、別サイトのCookieが当該サイトへ送られる可能性があります（そのCookieはパススルーのセッション内のものだけです）。
- **JSが設定したCookie（`document.cookie`）は送られません**（ブラウザのCookieに入れないため）。JSでCookieを設定してから使うサイトは動かないことがあります。
- **別オリジンへの遷移**（`location.href=` や meta refresh）は Service Worker を通らず、外へ出る可能性があります。ヘルパーのクリック／送信の捕捉、HTMLに付けるCSP（`form-action 'self'`、`frame-src 'self'`）、アプリ側の `frame-src` で防ぎますが、WebKitでの実効性は未検証です。
- 別オリジンのiframe（動画埋め込み・CAPTCHA）は、CSPの `frame-src 'self'` により表示されません。
- **WebSocket・WebRTC・サイト独自のService Worker** は使えません（端末のIPが見える、またはこの仕組みと衝突するため）。Cloudflare等のボット確認、`location.origin` を検査するサイト、ログイン後にWebSocketを使うサイトは動かないことがあります。その場合はNASブラウザに切り替えてください。
- HTMLのbrotli（`br`）は復号しないため、`Accept-Encoding` から外します（それでも `br` で返ってきたHTMLは502）。上流の `Link` と `Referrer-Policy` ヘッダは捨て、メタタグで `same-origin` を入れます。
- ダウンロードは、添付扱い（`Content-Disposition: attachment`）か20 MiB超を確認ページに切り替えます。JD2へ渡すのは **URLだけ** です（JD2のリンク追加APIはCookieを受け取れないため、ログインが必要なリンクは落とせません）。
- リクエストはNASのCeleron上で中継します。大きな動画の連続再生のような用途には向きません。

## 自己テスト・テスト

- 設定 › 診断 › 自己テストに「パススルー」の項目（設定OFF／待ち受け不可のときは SKIP）。ループバックでHubの待ち受けへ、使い捨てセッションと内蔵の疑似サイト（`pt-selftest.invalid`、外部通信なし）で、ページ読み込み＋ヘルパー注入、サブリソース、Cookieのファーストパーティ規則、リダイレクト書き換え、ダウンロード確認ページを確認します。Service Workerの制御はE2E側で確認します。
- 単体テスト `tests/test_passthrough.py`（URL対応、SSRF、Cookie、ヘッダ、HTML注入、ダウンロード判定、チケット／セッション、リスナー、TLS上流、設定・認証・CSP）。
- E2E `pt-*`（docs/testing.md）。上流の差し替え（`server.pt_test_*`）はテスト専用の属性で、`main()` や起動スクリプトは設定しません（QPKGの契約テストで確認）。
