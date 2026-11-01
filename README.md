# QnapDownloader

QNAP TS-253Be (QTS 5.x, x86_64)向け **JDownloader 2 + 安全なWebブラウザ／リンク収集／ダウンロード管理** 統合QPKGの設計リポジトリ。Rapidgatorリンク検出・ボタン化、Click'n'Load互換取り込み、サイト別の明示許可付き自動収集、ブックマーク、NAS側保存先選択を目指します。

**現在の状態: v0.1.7 iPhone対応のダウンロード状況画面（JD2連携は読取専用）を追加**。管理者ログイン、NAS共有フォルダ選択/永続化、手動リンク保留、ブックマーク、更新確認とQPKG自己更新ワーカー、Web管理画面、QDKビルド定義を追加しました。**JD2接続アダプタと手動LinkGrabber転送は実装済みですが、JD2実機接続・実ダウンロードは未検証です。ブラウザは端末側の隔離iframeプレビューのみ実装で、独立したChromiumブラウザ、サイト自動巡回、広告遮断、ページDOMの自動収集は未実装**であり、完了したとは表示しません。QDKでのx86_64 QPKG自動ビルド・テストは実施済みですが、QNAP実機でのインストール・起動・JD2接続は未検証です。既存Docker版JDownloader 2や別QPKGは変更しません。

## 仕様・開発ルール

- [詳細製品仕様 v1.1](docs/product-spec.md)
- [推奨設定・フラット保存・NASフォルダ選択](docs/storage-and-presets.md)
- [広告・悪意ある遷移・NAS保護の安全設計](docs/security.md)
- [実装計画と受け入れ試験](docs/implementation-plan.md)
- [内蔵JD2/Java・単一Web UI・独立更新と低I/Oアーキテクチャ](docs/embedded-jd2-architecture.md)
- [プレミアムアカウント管理・認証情報保護・公式JD2 API対応](docs/premium-accounts-design.md)
- [QnapSelfCare準拠の開発ワークフロー](DEVELOPMENT_WORKFLOW.md)
- [GitHub main保護・PR作成制限の適用方法](docs/github-protection.md)

## 主要な初期設定

1. **余計なサブフォルダなし**（JD2一般設定・Packagizerを両方確認）。
2. **自動開始、自動解凍、元ファイル削除、自動実行なし**。サイトごとの明示許可だけ例外。
3. NAS側フォルダ選択ダイアログから保存場所を設定し、ブックマーク・キュー・設定はアップデート後も永続化。
4. ZIP/CBZ/RAR/7zは元形式のまま保存し、QnapExtractor等で後処理しやすくする（外部処理アプリのAPI統合は未確認）。
5. 広告・トラッキング・危険なポップアップ・勝手な遷移とダウンロードをブロック。Cloudflare等の正規認証は対応可能な標準ブラウザで手動実施する。
6. 確定データは耐障害性を確保し、進捗表示・遮断件数・一時キャッシュはRAM主体でNAS HDDへの無駄な読み書きを抑える。
7. GitHub mainへの変更はPR経由、PR作成は書き込み権限あるコラボレーター/管理者のみ。GitHub側の保護設定は管理者による別途適用が必要。

## 内蔵JD2・Javaとプレミアムアカウント管理の今後の方針

**推奨設計はQPKG内のヘッドレスJD2 + フルJavaランタイム + QnapDownloader Web UIを唯一の操作画面とする構成**です。進捗・開始/停止/再開・リンク収集・ホスト別プレミアムアカウント（追加、期限/残容量、再認証、無効化、削除）・Hub/JD2/Javaの独立更新を一画面にまとめます。

現行v0.1.6にはJD2とJavaのバイナリは同梱されておらず、アカウント管理や実ダウンロードUIも**未実装**です。配布ライセンス、公式JD2署名、ローカル認証ブリッジ、実RAM（TS-253Be 2/4/8GB）を先に検証します。RAMのtmpfs展開はオプションで、パスワード/キュー/設定・JD2更新内容は永続保存し、ディスク書込ゼロは約束しません。既存Docker版JD2は変更しません。

## 開発前の調査

TS-253Beの実RAM/QTS、既存JD2の設定とAPI、Chromiumサンドボックスの稼働可否、Cloudflareのブラウザ互換性、QnapExtractor連携手段、QDKビルド環境とNASへの保存先権限は実装時に検証します。


## 初期実装の起動・注意事項

- Python 3.8+、標準ライブラリ。NASが起動してボリュームをマウントしてから、開発環境では `python3 -B webapp.py --data-dir ./runtime --port 17864` でループバック起動します（ルート権限と公開範囲に注意）。外部ネットワークへポート転送しないでください。
- 初回のWebアクセスでは**SSH認証トークンは不要**です。管理画面で12〜256文字の管理者パスワードを1回だけ登録します。最初に登録した端末が管理者を確定できるため、インストール直後に信頼できる家庭内LAN上で設定し、公開Wi-Fi・外部公開ポートを使用しないでください。登録済みアカウントはアップデート後も維持され、初回登録画面を再表示しません。
- **v0.1.3以降のQPKGは家庭内LANからのWebアクセスを初期状態で許可**します。例: http://192.168.68.57:17864/ 。サーバーはRFC1918/ループバック/プライベートVPN以外の接続元を拒否し、Host検証・ログイン・CSRF保護を維持します。ただしLAN HTTPは暗号化されません。信頼できるLANでのみ利用し、17864をインターネットにポート開放しないでください。遠隔操作にはHTTPSリバースプロキシやVPNを使ってください。保存データのconfig/network-modeにloopbackを書いて再起動すればNAS自身からのアクセスだけに戻せます（lanでLAN待受）。DOWNLOADER_LAN_ENABLED=0|1で一時上書きできます。
- 保存先ピッカーの表示ルートは既定`/share/Download`。許可対象を変える場合は`DOWNLOADER_ALLOWED_ROOTS`で絶対パスをコロン区切り指定します（管理者のみ編集）。
- ブックマーク、設定、保留キューはDBに保存。**保留キューはJD2へは送信されない**ため、まだ実際にダウンロードしません。
- GitHub Releaseの更新は**RC版を含む／正式版のみ**を管理画面で選べます（初期値RC版を含む）。公開された正規タグ`vX.Y.Z`または`vX.Y.Z-rcN`のQPKGのみ対象です。QPKGのSHA-256検証、更新前SQLiteバックアップ、更新後ヘルスチェックは従来通り必須。最初にRCチャンネル対応版へ上げる際は旧版がRCを認識できないため、手動でv0.1.6-rc1 QPKGを上書きしてください。
- QDK導入済みの開発マシンで `bash scripts/build-qpkg.sh x86_64 dist`。起動時にnpm build等は実行しません。

テスト: `python3 -m unittest discover -s tests -v`、`python3 -m py_compile webapp.py storage.py updater.py self_update.py durability.py qpkg/shared/downloader_pid.py`、`node --check web/app.js`、`sh -n qpkg/shared/downloader.sh qpkg/shared/python3-path`。追加のセキュリティ/ダウンロード本体機能は後続PRで検証します。

## JDownloader 2の独立更新と推奨設定の再適用

**JDownloader 2はJD2標準のアップデーターで単独更新**します。HubのQPKG自己更新はJD2のファイルやプラグインを入れ替えません。JD2更新後に一般設定・Packagizer・LinkGrabber・自動解凍が変化した場合はHubの「推奨設定を確認して再適用」で差分確認・復旧できる設計です。**現在は認証済みMyJDアダプタが実装されていますが、全必須設定キーの実機検証が終わるまで推奨設定一括再適用ボタンは無効**です。正式なJD2接続/設定読書きができるまで有効化しません。手順: [JD2独立更新と再適用](docs/jd2-independent-updates.md)。


### Docker不要の永続データ保存

QPKGの標準保存先は **QPKGインストール場所の親ディレクトリにある `QnapDownloader-data`** です（例: `/share/CACHEDEV1_DATA/.qpkg/QnapDownloader-data`）。本体インストール先とは別なのでQPKG更新時にデータを上書きせず、`/share/Container`共有フォルダ・Container Station・Dockerを必須としません。起動時に利用可能な別の共有フォルダへ自動フォールバックしたり、既存データを勝手に移動しません。

旧版のbootstrap-tokenファイルはv0.1.4への更新後、起動時に参照せず削除します。トークンの再発行やSSHでの確認は不要です。

別のデータ領域を使う場合は、初回起動前にQPKGサービスへ管理者が`DOWNLOADER_DATA_DIR`を明示設定してください。後から変更する場合は既存データの移行を計画し、サービス停止中にバックアップ・復旧確認してから変更してください。

## JD2への認証済み接続（任意）

MyJDownloader認証済みアダプタを追加しました。QPKGへライブラリを同梱した場合、管理者がconfig/myjd.jsonを0600で設定することでJD2更新状態を照会できます。JD2本体の更新は明示的opt-inです。推奨設定の全項目再適用は、実機で設定キーと既存Packagizerルールの安全性を確認するまで無効です。詳細: [MyJD接続設定](docs/myjd-setup.md)。

## 手動LinkGrabber転送（開発段階）

保留リンクを1件ずつJD2へ手動送信するAPIと画面を追加しました。既存Dockerの保存先とNASのパスの対応は自動推測しません。管理者による明示設定と、自動確定/自動開始OFFの検証が必須です。Packagizerルール未検証時は追加opt-inを要求します。送信結果不明時は自動再送しません。詳細は[MyJD接続設定](docs/myjd-setup.md)。

## Rapidgator URLのオフライン抽出（v0.1.2）

コピーしたサイトのテキスト/HTMLを貼り付け、最大20件のHTTPSファイルリンク候補をプレビューできます。利用者が明示的に承認した場合だけ保留キューに登録し、**ページ取得・スクリプト実行・JD2への自動送信は行いません**。詳細: [Rapidgatorオフライン抽出](docs/rapidgator-manual-extract.md)。

## Web画面が開かないとき

最初のv0.1.0-rc1 QPKGにもWeb画面は含まれます。ただしNASの127.0.0.1だけで待受する初期設定のため、iPhoneからNASのLAN-IP:17864へアクセスできません。v0.1.3でLAN待受を初期値としました。v0.1.5以前の更新画面はRC版を検出しません。v0.1.6への更新だけはApp Centerで手動上書きし、以後はRCチャンネルから検出・明示的に適用できます。

更新後、SSHの読取専用診断を実行できます（サービスは停止・更新しません）。

```sh
ROOT=$(/sbin/getcfg QnapDownloader Install_Path -d '' -f /etc/config/qpkg.conf)
sh "$ROOT/downloader-diagnose"
```

最初のQPKGには診断スクリプトがないので、代わりに以下で切り分けます。

```sh
curl --noproxy '*' -sS --max-time 4 http://127.0.0.1:17864/api/health
netstat -tnl | grep 17864
```

- 127.0.0.1が応答するのにLAN-IPだけ失敗した場合は、旧版のlocalhost限定待受の可能性が高いです。
- localhostも失敗する場合はQPKG起動、Python3、マウント、起動ログを確認してください。

## iPhoneホーム画面・QNAP App Centerのアイコン

共通の図案（シールド・下向き矢印・NAS）を使用。Safariホーム画面用は/apple-touch-icon.png（180px）、QNAP App Center用はQDK icons/のQnapDownloader.png・QnapDownloader_80.png・QnapDownloader_gray.pngです。開発時のSVGからQPKGビルド中に生成するため、NAS起動時のpip/npmビルドは不要です。

## 初回登録のトークン廃止 (v0.1.4)

`http://<NAS-IP>:17864/`で初回のみ管理者パスワードを登録します。既存の管理者がある環境では、従来のパスワードでログインしてください。再インストール/更新時にデータベースを初期化したり、既存パスワードを上書きしたりしません。

初回登録API `POST /api/setup` は、管理者未登録の場合だけパスワードを受け付けます。登録済みなら403で拒否します。Host/送信元private-IP/Originチェックを維持し、一般APIの認証・CSRFは変更しません。**トークンをなくす代わりに、最初の管理者登録をLAN上の別端末が先取りするリスクがあります**。初回設定は信頼できるネットワークで速やかに実施してください。

## QNAP App Centerのアイコンを押してもWeb画面が開かない場合 (v0.1.5)

QDKの`QPKG_DESKTOP_APP=0`を明示しました。App Centerからは**新しいブラウザタブ**で`http://<NASのIP>:17864/`を開く設計です。Webサーバーは安全のため`X-Frame-Options: DENY` / `frame-ancestors 'none'`を維持しており、QTSデスクトップのiframe内には表示しません。既定の埋め込みモードと両立しない可能性があったため、ブラウザタブを指定しています。

QPKGマニフェストは`QPKG_WEBUI=/`、`QPKG_WEB_PORT=17864`、`QPKG_WEB_SSL_PORT=-2`、`QPKG_SERVICE_PORT=17864`。QDKのアイコン/共有資産フォルダも明示しました。ブラウザのポップアップ拒否、QTSの古いアプリショートカット情報が残るケースもあります。更新後にQTSのページを再読み込みし、アプリアイコンをもう一度開いてください。

診断はSSHの`sh "$ROOT/downloader-diagnose"`で、QTSに**実際に登録されている**WebUI、Web_Port、Web_SSL_Port、Desktop_App、Service_Portと起動状態を確認できます（読取専用）。それでも開かない場合は診断結果と、QTSを開いているURLのスキーム（HTTP/HTTPS）を確認してください。

## JD2接続の管理画面表示

「設定」画面に、MyJDownloaderの接続設定有無・対象デバイス名・更新/リンク送信の許可状況を表示します。「JD2接続をテストする」は明示的な認証済み読取操作のみで、JD2の更新・ダウンロードや既存Dockerには手を触れません。接続用メール・パスワードはWebレスポンスに含まず、LANの平文HTTPを通して入力させないため、現状はNAS SSHから`config/myjd.json`（0600）を設定してください。詳細は[docs/myjd-setup.md](docs/myjd-setup.md)。

Web画面左上のアプリアイコンが表示されない問題も、厳格なCSP設定に`img-src 'self'`が欠けていたため修正しました。外部画像やスクリプトを許可する変更ではありません。

## RCプレリリース更新と安全な軽量ブラウザ (v0.1.6)

「アップデート」ではGitHubの最新正式版のみを参照していたため、これまでのv0.1.x-rc1リリースを見逃していました。今後はRCチャンネルで`/releases?per_page=30`を調べ、**新しい**`vX.Y.Z-rcN`と正式版のうち対応x86_64 QPKGを選びます。GitHub APIの返却値を無条件には信頼せず、厳密なタグ/asset URL/サイズ/ SHA-256ダイジェスト検証を維持します。公開されていないdraftやnightlyには更新しません。更新は手動確認・明示承認であり、自動インストールしません。QPKG内のバージョンは`X.Y.Z`なので、同じ内部バージョン間のRC番号だけの差は自動更新対象にしません。

「安全ブラウザ」タブは**iPhone/SafariのWebKit上で動く限定プレビュー**です。初期モードは隔離iframe内のスクリプト・フォーム実行を禁止し、管理画面のCookie/DOMとは異なる権限で開きます。利用者の承認でスクリプト/フォームのみを限定許可できますが、`allow-same-origin`、ダウンロード、ポップアップ、親画面操作は許可しません。NASのPython側は外部サイトを取得しません。閲覧先は公開HTTPSドメインのみ、IP/ローカル名/非標準ポート/認証情報付きURLは拒否します。

**制限**: 外部サイトの`X-Frame-Options`や`frame-ancestors`、Cookie・Cloudflareの制限で表示できないことが多くあります。その場合は「Safariで開く」を使用してください。iframe内で遷移したURLやDOMは取り出せず、保留キューにはアドレス欄で明示したURLのみを登録します。ダウンロードやJD2への自動送信、Webコンテンツ・Cookieのサーバ保存はありません。詳細: [隔離ブラウザ仕様](docs/browser-sandbox.md)。

## 使いやすさとダウンロード管理画面（v0.1.7）

iPhone・PC共通Web UIに「ダウンロード」タブと、概要からの「ダウンロード状況」「リンクを登録」「接続設定」へのショートカットを追加。実際のJD2にMyJDownloaderで接続済みの場合に限り、JD2の`downloadsV2/queryPackages`と`downloadsV2/queryLinks`を**明示的な更新ボタン操作で読取**します。パッケージ・ファイルの名称/進捗/転送量/速度/残り時間/状態を30件ずつ表示します。自動ポーリングや自動ダウンロードは行いません。

JD2から返却される複雑なレスポンスをWebへそのまま返さず、取得時に`url`・`password`・`saveTo`・Cookie等の秘密フィールドを要求しないほか、バックエンドの許可済みフィールドだけ表示します。未接続・設定未完了の場合は失敗理由と「接続設定」への導線を表示し、偽の進捗を表示しません。操作性改善のためモバイルのタブ/ボタンのタッチ領域、プログレス表示、キーボードフォーカスと結果通知を改善しています。

**注意**: 読取専用の初期段階です。実ダウンロードの開始/停止/再開/削除、プレミアムアカウント追加・編集、内蔵JD2/Java同梱は依然として未実装です。削除等の操作は実機でのJD2 API・ID整合と安全性検証を済ませた後の別PRで追加します。詳細は [ダウンロード管理画面の仕様](docs/download-dashboard.md)。
