# ダウンロード管理画面（v0.1.7 読み取り専用）

## 目的
iPhone/SafariとQNAP App Centerの両方で、QnapDownloaderの画面だけからJD2実際のダウンロード状態を確認しやすくする。将来の内蔵JD2・Java同梱後にも同じUIを使える表示モデルを先に作る。現時点はMyJDownloader経由の外部JD2のみ対応し、内蔵JD2未実装を誤認させない。

## 画面
- 「概要」に3つのショートカット（ダウンロード状況・リンク登録・接続設定）。
- 「ダウンロード」に手動更新ボタン、読み取り専用説明、パッケージ/ファイル一覧、転送済容量/総容量、速度、ETA、進捗バー、30件単位のページ移動。
- 未接続は理由と設定への案内。リンク送信は既存の明示操作、開始・停止・削除操作は未実装（ボタンを表示しない）。
- モバイルの44px以上のタッチターゲット、横スクロールのタブ、フォーカスの可視化、aria-live/status、prefers-reduced-motionを尊重。

## Backend契約
- GET /api/jd2/downloads?page=0..20（管理者認証必須）。明示的なブラウザ操作の時のみ呼び、サービス起動時やバックグラウンドでのポーリングを行わない。
- Hub管理のローカルエンジン（v0.1.11、旧MyJD経由は削除）の `downloadsV2/queryPackages`/`queryLinks` に、必要なread-onlyキーだけのquery dictを渡す。maxResults=30、startAt=page*30。
- `jd2_downloads.py` で受け取った値の型・サイズ・長さを制限し、未知のJSONフィールドを出力しない。保存先・URL・password・Cookie・生アカウント情報は返さない。
- 任意JD2メソッド名をWebへ渡す機構は追加しない。POST APIやJD2データ変更はこのPRに含めない。
- 実機JD2のqueryLinks/queryPackagesの戻り形式が想定外ならHTTP 400エラーと状態案内にし、架空の結果やダウンロード操作を返さない。

## 将来のPR
1. 内蔵JD2の認証済みローカルIPC PoC、権利・JRE配布ゲート。
2. ダウンロード一覧の状態精度/ページングの実機確認、未接続ステータスの例外区別。
3. 安全な一時停止/再開/削除はrevision付き対象検証・二段階確認・readback・通信不明時非再試行で別PR。
4. プレミアムアカウント一覧のマスク済み表示とHTTPS限定の登録/管理。仕様: docs/premium-accounts-design.md。

## 受け入れ
- APIの認証/非公開Host制限が維持される。
- URL/password/saveToなどJD2のextraフィールドが返らないことを回帰テストで検証。
- QPKG内にjd2_downloads.pyが入ることをbuild/contractで検証。
- iPhone縦画面で一覧・スクロール・ボタン操作ができ、全行のファイル名がtextContentでXSSとして解釈されないことを確認。
- 実機TS-253Beで正確な件数・進捗・速度を確認してからstable候補にする。

## v0.1.24 追加: 待機中リンクの開始と全体の開始／停止
- `GET /api/jd2/linkgrabber`（管理者認証）: `linkgrabberv2/queryLinks` に `name/host/bytesTotal/uuid/packageUUID` と `maxResults=50` だけを渡し、名前・ホスト・サイズ・ID（文字列）のみ返す（URL・保存先・パスワードは返さない）。ダウンロード画面の「JD2で待機中のリンク」に件数と名前を表示する。
- `POST /api/jd2/linkgrabber/start {confirmed:true}`: 一覧のリンクと所属パッケージを `moveToDownloadlist(linkIds, packageIds)` でダウンロードリストへ移し、`downloadcontroller/start`。`POST /api/jd2/downloads/start`、`POST /api/jd2/downloads/stop {confirmed:true}`。個別の削除・一時停止は引き続き未実装。
- 設定 `send_autostart`（既定ON）: Hubから送るリンクは `addLinks` を `autostart:true` にして送り、続けて `downloadcontroller/start` を呼ぶ。JD2側の自動確定・自動開始の設定は常にOFFを確認して変更しない。

## v0.1.29 追加: 項目ごとの開始・停止・削除（実機未検証）
- JD2呼び出し（許可リスト）: `downloadsV2/setEnabled(enabled, linkIds, packageIds)`、`downloadsV2/removeLinks(linkIds, packageIds)`（一覧からのみ削除）、`downloadcontroller/forceDownload(linkIds, packageIds)`、`linkgrabberv2/removeLinks`、ファイルも消す `downloadsV2/cleanup(linkIds, packageIds, "DELETE_ALL", "REMOVE_LINKS_AND_DELETE_FILES", "SELECTED")`（`delete_with_files` からのみ）。IDは64bit。ブラウザへは10進数の文字列（19桁以内・2^63-1以下）で渡し、Hubが整数のまま（浮動小数にせず）JD2へ送る。
- Hub API（管理者・CSRF・Origin必須、`kind`=`package|link`、`id` または `ids`（最大100件））: `POST /api/jd2/items/start`（setEnabled(true)＋forceDownload）、`/items/stop`（setEnabled(false)。再開は開始）、`/items/remove {delete_files, confirm_name}`、`POST /api/jd2/waiting/remove`（待機中の一覧から削除）、`/waiting/start`（ダウンロードリストへ移して開始）。操作の直前にHubがその `page` の一覧をJD2から取り直し、含まれないIDは拒否する。
- `delete_files: true` は1件のみ。`confirm_name` が項目名と完全一致し、かつ項目（ファイルは所属パッケージ）の保存先が、選択中のNAS保存先フォルダ（コンテナ時は `/downloads` へ読み替え）の内側にあるときだけ実行する（`..`・外側・取得不能は拒否）。既定は `delete_files: false`（ファイルは残す）。
- UI: パッケージ行・ファイル行・待機中リンク行を左スワイプで「削除」、右スワイプで「開始」／「停止」（実行中→停止、完了済みは右スワイプなし）。10px動いたところで横方向に固定し縦スクロールは妨げない（`touch-action: pan-y`）。幅の60%超の全スワイプは安全側の既定動作（開始／停止、またはファイルを残す一覧削除）を即実行。「削除」タップで「一覧から削除（ファイルは残す）」（既定）／「ファイルも削除」（名前を表示する確認ダイアログ、元に戻せない）／キャンセルのシート。各行の「⋯」ボタンに同じ操作（キーボード・VoiceOver用）。行の長押しで複数選択（下部バーの開始・停止・削除。複数選択の削除は一覧からのみ）。処理中の行は薄く表示し、完了後に一覧を更新。操作中は5秒ごとの自動更新で行を作り直さない。`prefers-reduced-motion` ではアニメーションなし。

## v0.1.29 追加: 確認画面からの自動復帰
- チャレンジ判定: タイトル／URLに加え、ページ内で `window._cf_chl_opt` や `#challenge-form` 等の有無を評価（読み込みイベントごとに無効化、要求時は最大1秒に1回）。`GET /api/rb/meta` とフレームヘッダの `challenge` に反映。
- クライアント: 自動でライブにしたタブ（`autoLive`）は、1.5秒ごとに `/api/rb/meta`（表示中のみ）を確認し、チャレンジなし＆読み込み完了が2回続いたら高速表示へ戻して再取得する。ホスト別の記憶には書かない。90秒たってもチャレンジなら「高速表示に戻す」チップを表示。⋯からの手動ライブは自動では戻さない。
- ファイルも削除する前の安全確認: JD2の `GeneralSettings` の「削除後に空フォルダも削除」（`DeleteEmptySubFoldersAfterDeletingDownloadedFilesEnabled`）を推奨設定（OFF）に追加。`delete_with_files` は実行前にこの値を読み、OFFでなければOFFへ設定して読み戻し、確認できなければ `cleanup` を呼ばずに中止する（保存先フォルダ自体が消えるのを防ぐ）。
