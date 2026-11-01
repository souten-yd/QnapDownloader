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
- JD2 myjdapi 1.1.11 のdownloads.query_packages/query_linksに、必要なread-onlyキーだけのquery dictを渡す。maxResults=30、startAt=page*30。
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