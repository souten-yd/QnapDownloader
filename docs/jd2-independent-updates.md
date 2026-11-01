# JDownloader 2 の単独更新と推奨設定の再適用

更新日: 2026-11-01
状態: **設計 + 設定差分判定の基礎モジュール実装済み。実JD2への接続/更新操作は未実装。**

## 更新系統を厳密に分ける

| 対象 | 更新システム | 保存・起動への影響 |
| --- | --- | --- |
| **JDownloader 2 Core / host plugins** | JD2の標準アップデーター | HubのQPKGを再インストールしない。JD2の更新/再起動はJD2だけに限定 |
| **QnapDownloader Hub** | GitHub Release のQPKG更新（QnapSelfCare方式） | JD2のCoreやホストプラグインを更新/再インストールしない |
| **Hub推奨設定** | 管理画面「推奨設定を確認して適用」 | JD2の実設定を照会し、必要な差分だけ適用。アップデート後に手動再適用可能 |

JD2は公式アップデート機構によって小さなホストプラグイン変更を稼働中に反映でき、Core更新は再起動が必要になる。Hubはこの動作を妨げず、更新後は再接続してバージョンや設定のドリフトを検査する。

公式API: `/update/runUpdateCheck`、`/update/isUpdateAvailable`、`/update/restartAndUpdate`、`/toolbar/triggerUpdate`。接続はMyJDownloaderの認証済みAPI／確認済みの安全な経路のみ。公開LANへの無認証legacy API公開は禁止。

### 重要なDocker版の例外

既存Dockerイメージがアプリ領域を書き込み禁止にしている、またはコンテナ起動方式がJD2 Core再起動を禁止する場合、通常のJD2内更新ができないことがある。この場合は**既存コンテナをHubが無断改変しない**。更新方法がコンテナの再作成なのか、JD2内更新なのかを診断で明示する。既存のJDownloader環境に依存せず「必ず単独更新可能」とは主張しない。

## Hub UI

アップデート画面を**Hub / JDownloader 2**の2領域に分離。

- Hub領域: GitHub正式リリースの確認、ダウンロードサイズ、SHA-256検証、DBオンラインバックアップ、QPKGインストール、health/ver照合、エラー表示。
- JD2領域: 接続状態、Core revision、JD2更新有無、公式更新チェック、更新ボタン、更新による一時中断の確認、JD2更新のみを実行。**Hubはアップデート後に接続を再確立**する。
- 推奨設定領域: 現在値・推奨値・差分・適用不能設定・ユーザーが作成したルールへの影響を一覧表示。「推奨設定を確認して適用」「元に戻す（検証済みsnapshot）」。
- JD2未接続/設定値を取得できない場合、更新・推奨設定適用ボタンは無効にし理由を表示する。「適用しました」という誤表示をしない。

## 推奨設定: 本体更新後に再適用

必須: LinkGrabber自動確定OFF、自動開始OFF、解凍OFF、パッケージ別フォルダ作成OFF（一般設定・Packagizerの両方）、保存先の既定値はNASフォルダピッカーで選んだパス。

確認のための公式参照:
- https://support.jdownloader.org/en/knowledgebase/article/updates-update-behavior
- https://my.jdownloader.org/developers/ （/config/list、/config/get、/config/set 等）
- https://support.jdownloader.org/en/knowledgebase/article/enabledisable-the-jdownloader-create-a-subfolder-for-each-package-feature
- https://support.jdownloader.org/en/knowledgebase/article/linkgrabber-how-to-add-links

設定名はJD2実バージョンでinspectし、型とキーが一致する場合のみ書き換える。例として`LinkgrabberSettings.linkgrabberautoconfirmenabled`、`LinkgrabberSettings.linkgrabberautostartenabled`は公式に記載されている。PackagizerのRule Listを丸ごと削除/初期化することは禁止。

## 正しい再適用トランザクション

1. 認証済みJD2 APIへ接続しrevisionと実設定を取得。
2. **設定の変更候補をpreview**し、変更しない項目・不明な項目も表示。
3. 利用者が明示して適用を指示した場合にだけ実行。
4. 更新直後のrevision変更や値変更を検出したらpreviewを破棄して再取得。
5. 設定のsnapshotを安全なHub DB/バックアップへ保存。
6. 項目ごとに適用、読戻し検証し、途中失敗時は既知の元値に復旧。復旧不完全の場合は明示し、手動確認を求める。
7. 既存キュー、完了ファイル、ダウンロード途中ファイル、個別保存先は一切書換えない。変更は原則これから追加する新規リンクに適用。
8. JD2 Core更新とプリセット適用の同時実行は禁止。更新が終わり接続が安定してからのみ設定差分判定を許可。

## 現時点の実装の境界

`jd2_policy.py` はプレビューの必要条件と、値のドリフト/キー不足によるfail-closed判定を担う。しかし正式なMyJDownloaderアダプタとJDownloader接続/更新UIを有効化するまでは**実設定の更新は実行できない**。次のPRで実際のAPI接続と設定名を検証してから有効化する。

必要な試験:
- JD2側だけ更新してもHub QPKGと設定DBは保持。
- Hub QPKG側だけ更新してもJD2本体/プラグインを更新しない。
- JD2アップデート前/後で設定が維持・変更された場合、previewに差分が正しく出る。
- 既存Packagizerカスタムルール、既存の保存先、稼働中ジョブに影響がない。
- JD2更新中はapply拒否、更新失敗・タイムアウト時に状態を誤表示しない。
- 不明キーと不完全な型は黙って上書きせず適用停止。
