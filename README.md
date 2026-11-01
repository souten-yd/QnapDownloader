# QnapDownloader

QNAP TS-253Be (QTS 5.x, x86_64)向け **JDownloader 2 + 安全なWebブラウザ／リンク収集／ダウンロード管理** 統合QPKGの設計リポジトリ。Rapidgatorリンク検出・ボタン化、Click'n'Load互換取り込み、サイト別の明示許可付き自動収集、ブックマーク、NAS側保存先選択を目指します。

**現在の状態: v0.1.4 初回トークン不要の管理者登録・LAN Web UI・iOS/QNAPアイコン対応**。管理者ログイン、NAS共有フォルダ選択/永続化、手動リンク保留、ブックマーク、更新確認とQPKG自己更新ワーカー、Web管理画面、QDKビルド定義を追加しました。**JD2接続アダプタと手動LinkGrabber転送は実装済みですが、JD2実機接続・実ダウンロードは未検証です。内蔵ブラウザ、Rapidgator自動検出、広告遮断は未実装**であり、完了したとは表示しません。QDKでのx86_64 QPKG自動ビルド・テストは実施済みですが、QNAP実機でのインストール・起動・JD2接続は未検証です。既存Docker版JDownloader 2や別QPKGは変更しません。

## 仕様・開発ルール

- [詳細製品仕様 v1.1](docs/product-spec.md)
- [推奨設定・フラット保存・NASフォルダ選択](docs/storage-and-presets.md)
- [広告・悪意ある遷移・NAS保護の安全設計](docs/security.md)
- [実装計画と受け入れ試験](docs/implementation-plan.md)
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

## 開発前の調査

TS-253Beの実RAM/QTS、既存JD2の設定とAPI、Chromiumサンドボックスの稼働可否、Cloudflareのブラウザ互換性、QnapExtractor連携手段、QDKビルド環境とNASへの保存先権限は実装時に検証します。


## 初期実装の起動・注意事項

- Python 3.8+、標準ライブラリ。NASが起動してボリュームをマウントしてから、開発環境では `python3 -B webapp.py --data-dir ./runtime --port 17864` でループバック起動します（ルート権限と公開範囲に注意）。外部ネットワークへポート転送しないでください。
- 初回のWebアクセスでは**SSH認証トークンは不要**です。管理画面で12〜256文字の管理者パスワードを1回だけ登録します。最初に登録した端末が管理者を確定できるため、インストール直後に信頼できる家庭内LAN上で設定し、公開Wi-Fi・外部公開ポートを使用しないでください。登録済みアカウントはアップデート後も維持され、初回登録画面を再表示しません。
- **v0.1.3以降のQPKGは家庭内LANからのWebアクセスを初期状態で許可**します。例: http://192.168.68.57:17864/ 。サーバーはRFC1918/ループバック/プライベートVPN以外の接続元を拒否し、Host検証・ログイン・CSRF保護を維持します。ただしLAN HTTPは暗号化されません。信頼できるLANでのみ利用し、17864をインターネットにポート開放しないでください。遠隔操作にはHTTPSリバースプロキシやVPNを使ってください。保存データのconfig/network-modeにloopbackを書いて再起動すればNAS自身からのアクセスだけに戻せます（lanでLAN待受）。DOWNLOADER_LAN_ENABLED=0|1で一時上書きできます。
- 保存先ピッカーの表示ルートは既定`/share/Download`。許可対象を変える場合は`DOWNLOADER_ALLOWED_ROOTS`で絶対パスをコロン区切り指定します（管理者のみ編集）。
- ブックマーク、設定、保留キューはDBに保存。**保留キューはJD2へは送信されない**ため、まだ実際にダウンロードしません。
- GitHub Releaseの更新確認は既存の正式Releaseが存在する場合のみ成功します。更新適用は正規QPKGインストール状態でのみサポート。ダウンロードしたQPKGのSHA-256、更新前SQLiteバックアップ、更新後ヘルスチェックを要求します。
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

最初のv0.1.0-rc1 QPKGにもWeb画面は含まれます。ただしNASの127.0.0.1だけで待受する初期設定のため、iPhoneからNASのLAN-IP:17864へアクセスできません。v0.1.3でLAN待受を初期値としました。プレリリースは自動更新対象外なのでQPKGをApp Centerで手動上書きしてください。

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
