# QnapDownloader

QNAP NAS 上で JDownloader 2 を動かし、iPhone・iPad・PC のブラウザから操作するための QPKG アプリです。リンクの受け取り、NAS 上のブラウザでのページ閲覧と長押しによるリンク解析、ダウンロードの管理を 1 つの画面にまとめています。

- 対象: QNAP TS-253Be（x86_64）/ QTS 5.x
- 状態: 実験版（RC）。各版の内容は [CHANGELOG.md](CHANGELOG.md) を参照してください。

## 主な機能

- **ダウンロード管理**: JD2 の進行状況、速度・残り時間、一時停止・再開、完了やスキップの一覧からの消去（任意で自動）。
- **リンク**: URL・テキスト・HTML を貼り付けると、ファイルリンクを取り出して JD2 へ送ります。iPhone の共有メニューからも送れます。
- **NAS ブラウザ**: NAS 上の Chromium でページを開き、手元の端末には次のいずれかの方法で表示します。
  - リーダー: 本文と画像を読みやすく整えて表示
  - ミラー: ページの複製を端末側で表示
  - 高速: 画像のタイル表示
  - ライブ: 画面の動画配信
  - パススルー: 端末のブラウザで表示し、通信だけを NAS 経由にする
- **長押しのリンク解析**: リンクを長押しすると、転送先をたどってファイルリンクを見つけ、JD2 へ送ります（順番待ちで 1 件ずつ処理）。
- **操作**: スワイプで戻る・進む、引き下げでタブを閉じる。PC はキーボード・マウス・トラックパッドでも同じ操作ができます。
- **ログイン**: パスワードのほか、HTTPS 接続時は Face ID / Touch ID（パスキー）。
- **自己テスト**: 表示・通信・メモリ・スワイプなどを端末と NAS の両方で測り、レポートを出します（設定 › 診断）。

## 動作環境

| 項目 | 内容 |
|---|---|
| NAS | QNAP x86_64、QTS 5.x（TS-253Be で確認） |
| Container Station | 推奨（JD2 と NAS ブラウザをコンテナで隔離して動かします）。入れない場合は JD2 を QTS 上で直接動かせます |
| 端末 | iOS / iPadOS の Safari（ホーム画面に追加して使用）、PC の Chrome・Edge・Safari |
| 外出先から使う場合 | Tailscale（HTTPS は `tailscale serve` を使用） |

## インストール

1. [Releases](https://github.com/souten-yd/QnapDownloader/releases) から最新の `QnapDownloader_<版>_x86_64.qpkg` を取得します（`SHA256SUMS` で照合できます）。
2. QTS の App Center で「手動インストール」から QPKG を選びます。
3. ブラウザで `http://<NASのIPアドレス>:17864/` を開きます。

以後の更新は、管理画面の「設定 › システム › 更新」から行えます（正式版のみ／RC 版を含む を選べます）。

## 初期設定

1. 最初に開いた画面で管理者パスワード（12〜256 文字）を登録します。最初に登録した端末が管理者になるため、信頼できる家庭内 LAN で行ってください。
2. ブラウズのホームにある「はじめに」で、ダウンロードの保存先を選びます（既定の選択範囲は `/share/Download`）。
3. 「初期設定を開始」で JD2 エンジンを準備します。JD2 と Java は QPKG に同梱しておらず、このとき公式配布物を取得し、ハッシュを検証してから配置します。
4. JD2 でプレミアムアカウントを使う場合は「設定 › JD2 › アカウント」で登録します。

## HTTPS と外出先からの利用

LAN の HTTP 接続は暗号化されません。外出先から使う場合や、パスキー・パススルーを使う場合は Tailscale で HTTPS にします。

1. Tailscale の管理画面で MagicDNS と HTTPS 証明書を有効にします。
2. NAS で次を実行します。

   ```sh
   tailscale serve --bg --https=443 http://127.0.0.1:17864
   ```

3. `https://<NAS名>.<tailnet名>.ts.net/` を開き、iPhone ではホーム画面に追加し直します。
4. パススルー表示を使う場合は、次も実行してから「設定 › ブラウザ › パススルー」をオンにします。

   ```sh
   tailscale serve --bg --https=8443 http://127.0.0.1:17865
   ```

ポート 17864 をインターネットへ公開しないでください。Hub はプライベートアドレス・ループバック・tailnet 以外からの接続を拒否します。

## 使い方

画面下のタブで切り替えます。

| タブ | 内容 |
|---|---|
| ダウンロード | JD2 の一覧と操作 |
| リンク | 貼り付けたリンクの確認と JD2 への送信 |
| ブラウズ | NAS ブラウザのタブ、最近見たページ、ブックマーク |
| 設定 | JD2・ブラウザ・接続・システム・診断 |

ページを開いたあとは、アドレスバー右の表示ボタンでリーダー・ミラーなどを切り替えます。選んだ表示はサイトごとに記憶されます。操作の一覧（スワイプ、キーボード・マウス）は [docs/browser-shell-ui.md](docs/browser-shell-ui.md) にあります。

## データと更新

- 設定・ブックマーク・リンク・アカウント情報は、QPKG のインストール先とは別の `QnapDownloader-data`（インストール先の親ディレクトリ）に保存され、更新しても消えません。
- 保存先は環境変数 `DOWNLOADER_DATA_DIR` で変更できます（初回起動前に設定してください）。
- 更新時は QPKG の SHA-256 を検証し、データベースをバックアップしてから入れ替え、起動を確認します。
- JD2 は JD2 自身のアップデーターで更新します。

## トラブルシューティング

- **画面が開かない**: NAS に SSH で入り、読み取り専用の診断を実行して結果を確認します。

  ```sh
  ROOT=$(/sbin/getcfg QnapDownloader Install_Path -d '' -f /etc/config/qpkg.conf)
  sh "$ROOT/downloader-diagnose"
  ```

- **動作がおかしい・遅い**: 「設定 › 診断 › 自己テスト」を実行し、レポートを確認します。デバッグログは同じ画面でオンにできます。
- **更新後に表示が崩れる**: 「設定 › システム」の「アプリのキャッシュを消す」を押します。

## 開発

- 開発の進め方: [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md)
- テスト: `bash scripts/verify.sh`（単体テストと安全チェック）、`bash scripts/e2e.sh --engines chromium,webkit`（端末の E2E）。詳細は [docs/testing.md](docs/testing.md)。
- ビルド: `bash scripts/build-qpkg.sh x86_64 dist`（QDK が必要）
- 開発用の起動: `python3 -B webapp.py --data-dir ./runtime --port 17864`

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/product-spec.md](docs/product-spec.md) | 製品仕様 |
| [docs/security.md](docs/security.md) | セキュリティ設計 |
| [docs/engine-setup.md](docs/engine-setup.md) | JD2 エンジンの初期設定 |
| [docs/container-runtime.md](docs/container-runtime.md) | コンテナでの実行 |
| [docs/browser-shell-ui.md](docs/browser-shell-ui.md) | 画面と操作 |
| [docs/https-and-share.md](docs/https-and-share.md) | HTTPS と iPhone の共有 |
| [docs/passthrough.md](docs/passthrough.md) | パススルー表示 |
| [docs/storage-and-presets.md](docs/storage-and-presets.md) | 保存先と推奨設定 |
| [docs/testing.md](docs/testing.md) | テスト |
| [docs/roadmap.md](docs/roadmap.md) | 今後の予定 |
