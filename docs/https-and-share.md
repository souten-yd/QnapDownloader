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
