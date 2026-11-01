# QnapDownloader

QNAP TS-253Be (QTS 5.x, x86_64)向け **JDownloader 2 + 安全なWebブラウザ／リンク収集／ダウンロード管理** 統合QPKGの設計リポジトリ。Rapidgatorリンク検出・ボタン化、Click'n'Load互換取り込み、サイト別の明示許可付き自動収集、ブックマーク、NAS側保存先選択を目指します。

**現在の状態: v0.1.0 初期実装（PRレビュー中）**。管理者ログイン、NAS共有フォルダ選択/永続化、手動リンク保留、ブックマーク、更新確認とQPKG自己更新ワーカー、Web管理画面、QDKビルド定義を追加しました。**JD2の実際の接続・ダウンロード、内蔵ブラウザ、Rapidgator自動検出、広告遮断は未実装**であり、完了したとは表示しません。QPKGビルド・QNAP実機試験も未実施です。既存Docker版JDownloader 2や別QPKGは変更しません。

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

- Python 3.8+、標準ライブラリ。NASが起動して共有フォルダをマウントしてから、`python3 -B webapp.py --data-dir /share/Container/QnapDownloader --lan --port 17864` で起動します（ルート権限と公開範囲に注意）。外部ネットワークへポート転送しないでください。
- 最初のアクセスで、NASの管理者が `/share/Container/QnapDownloader/config/bootstrap-token` をSSH等で読んでWebに入力し、管理者パスワード（12文字以上）を登録します。トークンは完了後に削除されます。
- **QPKGの既定待受は127.0.0.1:17864のみ**で、`--secure-cookie`を使用します。QTSのHTTPSリバースプロキシ（ループバックポート17864を転送）から利用してください。明示的にLAN公開が必要なら`DOWNLOADER_LAN_ENABLED=1`の管理者設定を使用し、Tailscale等の暗号化された接続に限定してください。HTTPポートをインターネットへ公開しないでください。プロキシ用ホスト名を使う場合は`DOWNLOADER_ALLOWED_HOSTS`で明示的に許可します。
- 保存先ピッカーの表示ルートは既定`/share/Download`。許可対象を変える場合は`DOWNLOADER_ALLOWED_ROOTS`で絶対パスをコロン区切り指定します（管理者のみ編集）。
- ブックマーク、設定、保留キューはDBに保存。**保留キューはJD2へは送信されない**ため、まだ実際にダウンロードしません。
- GitHub Releaseの更新確認は既存の正式Releaseが存在する場合のみ成功します。更新適用は正規QPKGインストール状態でのみサポート。ダウンロードしたQPKGのSHA-256、更新前SQLiteバックアップ、更新後ヘルスチェックを要求します。
- QDK導入済みの開発マシンで `bash scripts/build-qpkg.sh x86_64 dist`。起動時にnpm build等は実行しません。

テスト: `python3 -m unittest discover -s tests -v`、`python3 -m py_compile webapp.py storage.py updater.py self_update.py durability.py qpkg/shared/downloader_pid.py`、`node --check web/app.js`、`sh -n qpkg/shared/downloader.sh qpkg/shared/python3-path`。追加のセキュリティ/ダウンロード本体機能は後続PRで検証します。

## JDownloader 2の独立更新と推奨設定の再適用

**JDownloader 2はJD2標準のアップデーターで単独更新**します。HubのQPKG自己更新はJD2のファイルやプラグインを入れ替えません。JD2更新後に一般設定・Packagizer・LinkGrabber・自動解凍が変化した場合はHubの「推奨設定を確認して再適用」で差分確認・復旧できる設計です。**現v0.1.0ではJD2の認証済みAPIアダプタがまだなく、JD2更新ボタンと再適用ボタンは無効**です。正式なJD2接続/設定読書きができるまで有効化しません。手順: [JD2独立更新と再適用](docs/jd2-independent-updates.md)。
