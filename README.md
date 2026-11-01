# QnapDownloader

QNAP TS-253Be (QTS 5.x, x86_64)向け **JDownloader 2 + 安全なWebブラウザ／リンク収集／ダウンロード管理** 統合QPKGの設計リポジトリ。Rapidgatorリンク検出・ボタン化、Click'n'Load互換取り込み、サイト別の明示許可付き自動収集、ブックマーク、NAS側保存先選択を目指します。

**現在の状態: 設計文書のみ。アプリとQPKGは未実装、QNAP実機試験も未実施です。** 既存のDocker版JDownloader 2やQNAPの別QPKGは変更しません。

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
