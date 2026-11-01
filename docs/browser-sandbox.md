# 軽量安全ブラウザ（v0.1.6実験的プレビュー）

本段階はサーバー常駐Chromiumを導入しない軽量実装です。QNAP TS-253BeのRAMとHDD I/Oを消費しないよう、端末（iPhone Safari/PCブラウザ）のWebエンジン内でのみ閲覧します。完全なブラウザエンジンではありません。

## 機能と隔離

- 管理者ログイン後に「安全ブラウザ」から公開DNSホストのHTTPS URLを手動入力して閲覧
- 初期は厳格なiframe sandbox。外部サイトJavaScript・フォームを許可しない。必要時のみユーザー操作でallow-scripts/allow-formsに変更
- いかなるモードでもallow-top-navigation、allow-popups、allow-downloads、allow-modalsを付与しない。allow-same-originは、v0.1.10の明示確認付き「互換」モードだけに限り付与する（根拠は下記「v0.1.10 互換モード」）。強い制限・限定操作では付与せず、外部サイトは管理画面のオリジン権限にアクセスできない
- Content-Security-Policyはframe-src https: のみに拡張。管理アプリ自体は自サイトのみのJavaScript・CSS、frame-ancestors 'none'を維持
- NASサーバーはURL形式を検証するだけで外部ページをHTTP GETしない。DNS参照やCookie共有、HTML転送・プロキシ・保存は一切行わない
- ローカルIP（IPv4/IPv6）、localhost、内部向けTLD、ユーザー名/パスワード付きURL、非標準TLSポート、HTTP/javascript/data/fileスキームは拒否
- 戻る/進むはHubで明示的に開いたURLのみ管理。外部iframe内部遷移は別オリジンなので読取不可
- Safariで開く場合は通常ブラウザを利用。Cloudflare等の正規操作はユーザー自身がそこで実施。保留キューへの登録はアドレス欄URLを手動承認

## 制約・今後の段階

X-Frame-Options/CSP frame-ancestorsを設定するサイトはiframeを拒否します。サイト側の保護はプロキシやCookie偽装で迂回しません。Safari/WebKitのCookie制限やopaque originのため、JSを許可しても動かないサイトがあります。

広告・トラッキング通信を完全遮断する機能、HTML DOM全件抽出、ダウンロードリンク自動取得、Click'n'Load受信、Chromium隔離プロセス、ページ内の独立したダウンロードは禁止または未実装です。ブラウザ標準のネットワーク通信・広告読み込みが発生し得るため、広告や追跡を完全に遮断できると保証しません。

今後、実機RAMとHDD I/Oを測り、本格的な隔離ブラウザプロセスが必要かを判断します。明示的な同意なしにPython Webサーバーからリモートサイトへアクセスする方式は追加しません。

## セキュリティ受入れ

- 不正URL拒否、/api/browser/validateの管理者認証必須、サーバー側ネットワーク呼び出しゼロ
- iframe DOMの同一オリジン権限は互換モード以外で禁止（互換でも外部ページ自身のオリジンのみ）、ポップアップ・ダウンロード・親画面遷移は全モードで禁止
- JD2への自動投入/実行なし。DB書込は既存の明示リンク保留操作だけ

## v0.1.9 の更新（マルチタブ）

プレビューはブラウザ型シェルの「Webタブ」（最大6枚）として動作します。タブごとに独立したiframe・履歴・隔離モードを持ち、sandbox方針と上記の禁止事項は変わりません。詳細は [browser-shell-ui.md](browser-shell-ui.md) を参照してください。

## v0.1.10 互換モード

同一オリジン権限のないsandbox（opaque origin）ではCookie・ストレージが使えず、Cloudflareなど正規の確認が完了しないことがある。そこで、タブごとに明示確認（confirm）した場合に限り `allow-scripts allow-forms allow-same-origin` の「互換」モードを使える。

- 安全性: 外部ページは常に公開DNS名のhttps、Hubは `http://<LAN/VPN IP>:17864` またはリバースプロキシのホストであり、両者のオリジンは一致しない。そのため `allow-same-origin` はそのページ自身のオリジンのCookie/ストレージを使えるようにするだけで、Hubのセッション・ストレージには届かない。
- 担保: `/api/browser/validate` が、Hub自身のホスト名（Hostヘッダー）と `*.ts.net` を全モードで拒否する。フロントも互換iframe生成前に同じ検査をする。
- 互換モードはCloudflareを回避する機能ではない。iPhone Safariのサードパーティ Cookie ブロックで動かない場合は「Safariで開く」を使う。popups/downloads/modals/top-navigationは互換でも付与しない。
