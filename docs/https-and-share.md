# HTTPS接続とiPhone共有（v0.1.13）

## 背景

Hubが平文HTTPのページだと、その中のhttps iframeは「安全なコンテキスト」になりません（`crypto.subtle`が使えない）。また`cf_clearance`は`Partitioned; SameSite=None; Secure`のCookieです。そのためCloudflare確認がiframe内で失敗します。HubをHTTPSで提供すると改善する見込みがあります（保証はありません。サイトが埋め込みを許可していること、互換モードであることが前提です）。

## A. Tailscale ServeでHTTPS

Hubに新しい待ち受けやコマンド実行は追加していません。TLS終端はNAS上のTailscaleが行います。

1. Tailscale管理画面でMagicDNSとHTTPS証明書を有効にする。
2. NASにSSHで入り、`tailscale serve --bg --https=443 http://127.0.0.1:17864` を実行する（QNAPのTailscaleアプリではコマンドの場所が異なることがあります。`which tailscale`またはApp Centerのインストール先を確認）。
3. `https://<NAS名>.<tailnet名>.ts.net/` を開き、ホーム画面に追加し直す。

設定 › HTTPS接続 に現在の状態、手順、コマンドのコピーボタンがあります。Webタブの互換モードヒントには、HTTP接続のときだけ「HTTPSで開くと通過しやすくなります」が出ます。

### Hub側の判定

- `request_is_https(peer, headers)`: TCPの接続元がloopback（127.0.0.0/8, ::1）で、かつ `X-Forwarded-Proto: https` または Hostが`.ts.net`のとき、そのリクエストはHTTPS。loopback以外の接続元では`X-Forwarded-Proto`を信用しません。
- Hostの許可: 従来のloopback/プライベートIP/`DOWNLOADER_ALLOWED_HOSTS`に加え、接続元がloopbackまたはtailnet（100.64.0.0/10, fd7a:115c:a1e0::/48）のときだけ (a) 有効な`*.ts.net`名（ポート任意）と (b) ドットなしのMagicDNS短縮名（例 `nas`、ポート任意）を許可。ドットを含む`ts.net`以外の名前（`evil.example.com`等）はtailnet接続元でも拒否します（DNSリバインディング対策）。他のLAN接続元からの`*.ts.net`・短縮名も拒否。
- CookieはHttpOnly; SameSite=Strictのまま、起動フラグまたは上記のHTTPS判定のとき`Secure`を付加。`/api/session`は`https`を返し、HTTP警告を隠します。
- **HSTSは意図的に送りません。** 同じホスト名のプレーンHTTP（LAN/tailnetのhttp://…:17864）が使えなくなるためです。

### 注意

- Serve経由の接続元は常にloopbackなので、ログイン／共有の失敗回数制限は「全端末で1つ」の枠になります。
- `*.ts.net`は埋め込み表示先として引き続き拒否されます（Hub自身のオリジン保護）。

## B. iPhoneの共有メニューから送る

管理画面（設定 › iPhoneから送る）で端末名を付けてトークンを発行します。トークンは作成時に一度だけ表示され、DBにはSHA-256のみ保存。最大10台、削除（失効）は即時。

### API

- `GET /api/share/tokens`、`POST /api/share/tokens {name}`、`POST /api/share/tokens/delete {id}`: ログイン＋CSRF＋Origin必須。一覧にトークン／ハッシュは含まれません。
- `POST /api/share`: `Authorization: Bearer <token>`、`{"text": "..."}`（または`{"url": "..."}`、64KiB以下）。Rapidgatorリンクを最大50件抽出（なければ、空白なしの単一のhttps URLをそのまま1件）し、重複除外のうえ「追加したリンク」へ**未送信で**登録します。JD2への送信・ダウンロード開始はしません。応答は`{"added": n, "found": m}`。認証失敗は常に同じ401。Cookieは参照せず、自サイト以外の`Origin`付きは403。OPTIONS/CORSなし。トークンごとに10分30回、失敗は接続元ごとに5分20回で429。`last_used_at`は1トークンあたり最大1時間に1回だけ更新（HDD書込を抑制）。

### ショートカット

画面の手順どおりに作成します。JavaScriptは、Safariのページ内リンクのうちRapidgator系だけにフィルタして本文を小さく保ちます:

```
completion(Array.from(document.querySelectorAll('a[href]'),a=>a.href).filter(h=>/rapidgator\.net|rg\.to/.test(h)).join('\n')+'\n'+location.href)
```

エンドポイントはiPhoneから届く必要があります（Tailscale）。トークンを平文で送らないようHTTPS推奨。HTTPかつtailnetでない場合は警告を表示します。

### ブックマークレット

`javascript:location.href='<origin>/#/links?paste='+encodeURIComponent(location.href)` をSafariのブックマークに登録すると、リンク画面の貼り付け欄にURLが入ります（自動保存なし。表示直後にURLはアドレスから消えます）。

## C. パスキー（顔認証）ログインとセッション（v0.1.53）

iPhoneのFace ID／Touch IDで入れるようにするWebAuthn（パスキー）と、ログインを保つセッションの設計メモ。セキュリティレビューを兼ねます。**実機（iPhone＋Tailscale経由のhttps）では未検証**です。

### 使い方

1. `https://<NAS名>.<tailnet名>.ts.net/` で開き、パスワードでログイン。
2. 設定 › システム › ログイン › 「この端末で顔認証を登録」（初回ログイン直後にiPhoneだと「次回から顔認証でログインしますか？」の提案も一度だけ出ます）。
3. ログイン画面の「顔認証でログイン」、またはパスワード欄のパスキー自動入力。パスワードは常に残ります（フォールバック）。

### 脅威モデル

- 守るもの: 管理画面（JD2操作、プレミアムアカウント登録、保存先）への不正ログイン。攻撃者は (a) tailnet内／LANの別端末、(b) 偽サイト（フィッシング）、(c) DBのバックアップや読み取り専用の漏えい、(d) Cookie窃取（XSS・盗聴）を想定。
- 想定外: NASのroot奪取、iPhone本体の侵害、Tailscaleアカウント乗っ取り（Serveの証明書・名前を握られる）。
- 偽サイト対策: パスキーは登録時のRP ID（ホスト名）に暗号的に結び付き、ブラウザは別ホストの認証を出しません。Hubも`clientDataJSON.origin`が`https://<Hostヘッダ>`と完全一致、`rpIdHash`がRP IDのSHA-256と一致、`crossOrigin`なし、を検証します。
- ユーザー検証必須: `userVerification: required`、フラグUP（操作）とUV（顔認証・指紋）の両方が立っていなければ拒否。端末の画面ロック解除だけの成功やUV無しの認証器は通りません。
- 複製の検知: 署名カウンタが両方0でないとき、増えなければ拒否してログに記録（`passkey_counter_rejected`、診断イベントとデバッグログ）。iCloudキーチェーン（同期パスキー）はカウンタ0を返すためこの検査は働きません。

### RP IDとオリジンの縛り（なぜhttpでは使えないか）

- RP IDは「リクエストのHostからポートを除いた名前」で、次をすべて満たすときだけ有効です: `request_is_https`が真（接続元がloopbackで`X-Forwarded-Proto: https`または`.ts.net`のHost）、既存のHost許可規則を通る、名前が有効な`*.ts.net`（または`DOWNLOADER_ALLOWED_HOSTS`に載せたドット付きDNS名）。IPアドレス、短縮名（`nas`）、`localhost`は拒否します。オリジンは`https://` + Host（非標準ポートで公開した場合はポート付き）です。
- WebAuthnはブラウザが「安全なコンテキスト」（https、またはlocalhost）でしか提供しません。tailnet上のhttp（`http://100.x.y.z:17864`、`http://nas:17864`）はWireGuardで暗号化されていても安全なコンテキストではないため、`navigator.credentials`が使えません。設定画面には「顔認証は https://…ts.net で開いたときだけ使えます」と表示し、Hubが覚えたhttps名があればそこへのリンクを出します（名前は認証済みのhttpsリクエストからだけ学習し、DBに保存）。
- 注意: `*.ts.net`名を変える（マシン名・tailnet名の変更）とパスキーは使えなくなります。パスワードで入り、古いパスキーを削除して登録し直してください。
- E2Eテスト専用: 隠しフラグ`--test-passkey-host localhost`（`server.passkey_test_hosts`）だけが`http://localhost`でのパスキーを許します。通常起動・QPKGのスクリプトでは指定しません。

### 鍵の保存

- DB（`passkeys`表）には資格情報ID、**公開鍵**（COSE）、アルゴリズム、署名カウンタ、名前、RP ID、作成日、最終使用日だけを保存。秘密鍵はiPhoneのSecure Enclave／キーチェーンから出ません。Hubに署名コードはなく、検証のみ（`webauthn.py`、標準ライブラリのみ。ES256=P-256のECDSA、RS256=RSASSA-PKCS1-v1_5。RSAは2048〜4096bitのみ）。
- 認証器の証明（attestation）は`none`を要求し、他の形式が来ても検証・信頼せず無視します。管理者が既にログイン済みの状態でしか登録できないため、機種の証明は使いません。
- ユーザーハンドル（ランダム16バイト）は初回に作って固定。最大10個。

### レート制限と単回チャレンジ

- チャレンジは32バイト乱数、RAMのみ、2分で失効、1回で破棄（検証が失敗しても破棄）、種別（登録/認証）・セッション・RP ID・オリジンに束縛。同時保持は最大256。
- `login/begin`は接続元ごとに5分30回。`login/finish`と登録は検証に失敗するたびにパスワードログインと**同じ失敗枠**（5分10回）へ加算し、超えるとパスワード・パスキーとも429。失敗の応答は常に同じ文言（資格情報の有無や原因を区別しません）。Serve経由は接続元が常にloopbackなので、枠は全端末で1つです。
- すべてのPOSTは従来どおりHost許可・Origin一致が必須。登録・改名・削除・全端末ログアウトはさらにログイン＋CSRF。リクエストは32KiB以下、CBORは16KiB・深さ8・要素1024まで。

### セッション

- セッションはSQLite（`sessions`表）に保存し、Hub再起動・更新後も有効。保存するのはセッションIDのSHA-256だけで、生のIDはブラウザのCookie（HttpOnly; SameSite=Strict; httpsではSecure）にしかありません。CSRFトークンはセッションIDからHMACで導出するためDBに存在しません（セッションごとに異なる）。セッションIDとCSRFはログ・デバッグログ・自己診断に出しません。
- 寿命: パスキーでのログイン、およびパスワードログインで「この端末を記憶する」（httpsでは既定オン）を選んだものは、30日操作がなければ失効（アイドル）、作成から最長90日（絶対）。Cookieの`Max-Age`は残り期間と一致し、利用すると最大1時間に1回だけ延長（HDD書込みを抑えるため）しCookieを再発行します。それ以外のパスワードログインは従来どおり固定8時間（延長・書込みなし）。最大128セッション（超えると最も古いものから削除）。
- 失効: 「ログアウト」は現在のセッション、設定 › システム › ログイン › 「すべての端末からログアウト」は全セッションを即時削除します。パスキーを削除しても既存セッションは残るので、紛失・盗難時は両方行ってください。

### 未検証・既知の限界

- 実際のiPhone（iOS 18、ホーム画面アプリ）のFace ID、Tailscale Serve経由のオリジン、iCloudキーチェーン同期、パスキー自動入力のUIは未検証です（Chromeの仮想認証器によるE2Eと、固定鍵によるテストのみ）。
- ES256検証は純Pythonで、1回数ミリ秒〜十数ミリ秒です（NASでは遅くなり得ます）。レート制限で乱用は抑えられます。
- 「記憶」したセッションはCookieが盗まれると最長30日（アイドル）使えます。共有端末では記憶をオフに、紛失時は全端末ログアウトを。


## D. パススルー表示用の2本目の公開（v0.1.57・任意）

パススルー表示（設定 › ブラウザ › パススルー）を使うときだけ、アプリ用の `--https=443` とは別に、NASで次を1回実行します。

```
tailscale serve --bg --https=8443 http://127.0.0.1:17865
```

- 17865 はHubのパススルー専用の待ち受け（ループバックのみ・設定がONのときだけ起動）です。管理画面（17864）とは別のポート＝別オリジンで、管理APIやログインCookieには届きません。
- 公開ポートを変えるときは、設定 › ブラウザ › パススルーの値（`pt_public_port`）も合わせます（既定 8443）。
- 詳細・制限は docs/passthrough.md を参照してください。
