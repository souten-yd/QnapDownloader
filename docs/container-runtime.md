# Container Station 実行基盤（JD2コンテナ化・リモートブラウザ）設計

設計日: 2026-11-02 / 対象: TS-253Be 16GB, QTS 5.x, Container Station
利用者の決定: QTS上で直接動かすと危険なものはContainer Stationで動かす。JD2もコンテナへ移す。導入は「初期設定」ボタン1回。

## 1. なぜコンテナか（判断）
- **リモートブラウザ（Chromium）はQTS上で直接動かさない。** QPKGのサービスは管理者権限で動くため、Chromiumは `--no-sandbox` が必要になり（本プロジェクトの安全grepで禁止）、悪意あるページの侵害がNAS全体に及ぶ。QTSにはChromium用ライブラリも無い。コンテナなら非root利用者・Chromiumサンドボックス有効・ファイル隔離・メモリ上限(cgroup)を実現できる。
- **JD2もコンテナへ移す。** 外部サイトを解析・取得する常駐Javaプロセスであり、隔離・メモリ上限・NASファイルへの最小限のアクセス（保存先と自身のデータだけ）を得られる。Javaは公式イメージに含まれるため、別途のJava取得が不要になる。

## 2. 共通方針
- Hubは**固定の操作だけ**を行う `container_runtime.py` を通してDocker CLIを使う（QTSの `getcfg container-station Install_Path` から解決）。任意引数・任意イメージ・任意コマンドは受け付けない。
- 操作対象はHubが作成した、ラベル `org.qnapdownloader.managed=1` と名前接頭辞 `qnapdl-` を持つコンテナ・ネットワーク・イメージだけ。それ以外の既存コンテナ（既存のDocker版JD2を含む）は一覧も変更もしない。
- ベースイメージはダイジェスト固定（2026-11-02 取得）:
  - `eclipse-temurin:21-jre@sha256:0b99b3376fbb50dbf9c0af8535fdd5b8eb0d3fdfc8b1e4967f383b6919f6901a`
  - `debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8`
- **ポートは一切公開しない（`-p` なし）。** 各コンテナ専用のユーザー定義ブリッジネットワーク（`qnapdl-jd2-net`, `qnapdl-browser-net`）に1台ずつ置き、HubはホストからコンテナIPへ直接接続する。LANからは経路がなく、ブラウザコンテナからJD2 APIにも届かない（別ネットワーク）。接続前に毎回 `docker inspect` で、宛先IPがラベル付きの自コンテナのものであることを確認する。
- **ディスク書込を最小化**: `--log-driver none`、`--read-only` ルートFS、書込が必要な一時領域は `--tmpfs`（サイズ上限付き）。永続化するのはJD2のデータ（設定・キュー・アカウント・部分ファイル）とダウンロード成果物だけ。
- **メモリ**: 各コンテナに `--memory` と `--memory-swap`（同値＝swap不使用）を設定。起動前に `/proc/meminfo` の `MemAvailable` を確認し、閾値未満なら起動しない（他アプリを止めない）。
- 非root利用者（`--user 1000:1000`）、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit`。

## 3. JD2コンテナ（v0.1.15）
- 実行: `eclipse-temurin:21-jre@…` に `engine/jd2` を `/jd2` へ、選択済み保存先を `/downloads` へバインドマウント（それ以外のNASパスはマウントしない）。`/jd2/logs` は `--tmpfs`（64MB、RAMのみ）。
- メモリ: `--memory 1g`、Javaヒープ `-Xmx512m`。起動条件 `MemAvailable ≥ 1.5GiB`。
- ローカルAPI: コンテナ内JD2の旧API(3128)を `0.0.0.0` で待受（コンテナ内のみ。ポート公開なし）。設定ファイルの `deprecatedapilocalhostonly` を false に変更（コンテナ外＝ホストから届かせるため）。ホストからは専用ネットワークのコンテナIPでのみ到達。
- 保存先はJD2から見ると `/downloads`。Hub→JD2のパス変換を `jd2_transfer` に追加（NASパス ↔ `/downloads` 配下）。
- 移行: 既存の内蔵（QTS直実行）エンジンがあれば停止し、同じ `engine/jd2` データをそのまま使う。単一ライター保証（ネイティブとコンテナを同時に起動しない）。ネイティブ方式のコードは当面フォールバックとして残すが既定はコンテナ。
- 「初期設定を開始」の段階: Container Station検出 → メモリ確認 → イメージ取得（ダイジェスト検証）→ ネットワーク作成 → JD2.jar取得・検証（既存なら再利用）→ 設定作成 → 初回起動・アップデート → 常駐 → 接続確認 → 推奨設定。

## 4. リモートブラウザコンテナ（v0.1.16）
- イメージ: 初期設定時に `debian:bookworm-slim@…` から `chromium`・`xvfb`・`socat`・日本語フォントを入れたイメージを `docker build` で作る（Debianのセキュリティ更新付きChromium。ビルドは利用者のボタン操作時のみ）。「更新」で再ビルド。
- 起動: 非root、Chromiumサンドボックス有効（`--no-sandbox` 禁止）、Xvfb上のヘッドフル（Cloudflareに通常のブラウザとして扱われやすい。UA偽装はしない）。プロフィールは `--tmpfs`（RAM、512MB）で毎回使い捨て、`--disk-cache-size=1`、クラッシュレポート無効。CDPは内部で `socat` によりコンテナIP:9223 に中継（ポート公開なし）。
- メモリ: `--memory 2g`。新しいタブを開く前に `MemAvailable ≥ 2GiB` かつコンテナ使用量が上限の80%未満を確認し、満たさなければ開かない（理由を表示）。同時タブ数上限 3。
- **解放**: Hubのタブを閉じたらCDP `Target.closeTarget` で該当レンダラを終了。ブラウザタブが0になって5分経過、または全利用者が10分操作しなければコンテナごと停止（`docker stop` → `rm`）。次回は数秒で再起動。
- 画面転送: CDP `Page.startScreencast`（JPEG、端末幅×DPR、品質60、最大10fps、画面非表示中は停止）。Hub→ブラウザはSSE、入力（タップ・スクロール・文字）はPOSTで受けCDP `Input.*` に変換。iPhoneへ外部サイトの通信・スクリプトは一切届かない。
- リンク抽出: CDP `Runtime.evaluate` で表示中ページのリンク（Rapidgator等）を取得し「N件を追加 / JD2へ送る」。CAPTCHAの自動解答・自動クリックは行わない（利用者が画面越しに操作）。
- ダウンロード: ブラウザ内のファイルダウンロードは無効（`Browser.setDownloadBehavior deny`）。ファイル取得はJD2経由のみ。

## 5. 安全上の残余リスク
- Hubは管理者権限でDocker CLIを使う。操作は固定関数のみ、引数はコード内定数とHubが検証した値だけ。
- ブラウザコンテナはインターネットとLANへ出られる（ブラウザとして必要）。悪意あるページがLAN内機器へ要求を出す可能性はある（通常のブラウザと同等）。将来、LAN宛て通信の遮断を検討。
- イメージ取得時のダイジェスト照合はDocker自体が行う。ビルドで入るDebianパッケージは署名検証付きのapt。
