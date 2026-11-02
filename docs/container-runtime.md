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

## 3. JD2コンテナ（v0.1.15、実装済み・実機未検証）
- 実行: `eclipse-temurin:21-jre@…` に `engine/jd2` を `/jd2` へ、選択済み保存先を `/downloads` へバインドマウント（それ以外のNASパスはマウントしない）。`/jd2/logs` は `--tmpfs`（64MB、RAMのみ）。
- メモリ: `--memory 1g`、Javaヒープ `-Xmx512m`。起動条件 `MemAvailable ≥ 1.5GiB`。
- ローカルAPI: コンテナ内JD2の旧API(3128)を `0.0.0.0` で待受（コンテナ内のみ。ポート公開なし）。設定ファイルの `deprecatedapilocalhostonly` を false に変更（コンテナ外＝ホストから届かせるため）。ホストからは専用ネットワークのコンテナIPでのみ到達。
- 保存先はJD2から見ると `/downloads`。Hub→JD2のパス変換を `jd2_transfer` に追加（NASパス ↔ `/downloads` 配下）。
- 移行: 既存の内蔵（QTS直実行）エンジンがあれば停止し、同じ `engine/jd2` データをそのまま使う。単一ライター保証（ネイティブとコンテナを同時に起動しない）。ネイティブ方式のコードは当面フォールバックとして残すが既定はコンテナ。
- 「初期設定を開始」の段階: Container Station検出 → メモリ確認 → イメージ取得（ダイジェスト検証）→ ネットワーク作成 → JD2.jar取得・検証（既存なら再利用）→ 設定作成 → 初回起動・アップデート → 常駐 → 接続確認 → 推奨設定。

## 4. リモートブラウザコンテナ（v0.1.16、実装済み・実機未検証）

> **v0.1.16 実装メモ（設計からの差分・判断）**
> - 実装: `remote_browser.py`（stdlib製の最小RFC 6455クライアント＋CDPセッション管理）、`container_runtime.py` の `browser_*`/`run_browser`/`stats`、`container/browser/{Dockerfile,entrypoint.sh}`（QPKGに同梱、`scripts/build-qpkg.sh` が `container/` をコピー）。APIは `/api/rb/*`（status/setup/stop/tabs/tabs/close/frame/input/links/navigate）。CDPの生の中継は無く、固定のメソッドだけをHubが送る。
> - イメージ: `qnapdl-browser:<Dockerfile+entrypoint.sh のSHA-256先頭12桁>`。ビルド文脈は同梱の固定フォルダのみ（2ファイル以外が混じると拒否）。内容が変われば別タグになり、古いHub製イメージは再ビルド時に削除。
> - 起動引数（完全一致をテスト）: `--read-only --log-driver none --memory 2g --memory-swap 2g --shm-size 512m --pids-limit 1024 --cap-drop ALL --cap-add SYS_ADMIN --security-opt no-new-privileges --user 1000:1000 --tmpfs /profile:size=512m --tmpfs /tmp:size=256m`、`-p`・マウントなし、専用ネットワーク `qnapdl-browser-net`。seccompは既定のまま。
> - **サンドボックスと `--cap-add SYS_ADMIN`（最大の未検証事項、ブラウザのみ・JD2には付けない）**: サンドボックス無効化オプションは使わない。DebianのChromiumはユーザー名前空間のサンドボックスを使う。Dockerの既定seccompは、名前空間フラグ付きの `clone`/`unshare` を、コンテナ作成時のcapabilityセットに `CAP_SYS_ADMIN` が含まれる場合にだけ許可する。そこで `--cap-drop ALL` のあとに `--cap-add SYS_ADMIN` だけを足す（seccompは既定のまま）。
>   - **なぜ実権限にならないか**: プロセスは `--user 1000:1000`（非root）＋`no-new-privileges` で動く。非rootユーザーではSYS_ADMINはbounding setにだけ入り、effective/permitted/ambientは空のまま、`no-new-privileges` により後から引き上げることもできない。作れるのは**非特権のユーザー名前空間**だけで、これがChromiumの名前空間サンドボックスの必要条件。
>   - **実行時の自己検査**: `entrypoint.sh` は最初に `/proc/self/status` の `CapEff` が `0000000000000000` であることを確認し、違えば終了コード79で終了する（selftestも同じ）。Dockerの挙動が想定と違ってもChromiumが実権限で動くことはない。続いて `unshare --user …` で名前空間を試し、使えなければ78で終了。初期設定の「サンドボックスの確認」段階（`docker run --rm … selftest`）は78・79を区別せず、どちらも設定を中止し、カーネルのユーザー名前空間とこの検査の両方に触れた日本語メッセージを出す（サンドボックスは無効にしない）。
>   - **残余リスク**: bounding setにSYS_ADMINが入っているため、万一カーネルの脆弱性などでプロセスがcapabilityを得た場合、空の場合より大きな操作が可能になる。緩和: 非root・`no-new-privileges`・読み取り専用ルート・既定seccomp/AppArmor・ポート非公開・NASパスのマウントなし・メモリ/PID上限・タブを使わなければコンテナごと停止。それでもコンテナ脱出の攻撃面は `--cap-drop ALL` のみの場合より広い、という点は受け入れた判断として記録する。
>   - **実機で効かなかった場合の次の手**: ユーザー名前空間だけを許可する専用seccompプロファイルの同梱（`--security-opt seccomp=<固定パス>`）で、未実装。
> - ウィンドウ: 各タブは別ウィンドウ（`newWindow:true`）で作る。同一ウィンドウの背面タブは描画されずスクリーンキャストが止まるため。起動時の `about:blank` は「錨」として残し（最後のタブを閉じてもChromiumが終了しない）、タブ数には数えない。ポップアップ（`openerId` 付きの新規ターゲット）は即閉じる。
> - https以外（`http:`・`file:`・`chrome:` など）への遷移は、コミット後に `about:blank` へ戻す事後ブロック（一瞬表示され得る）。JSの `alert` 等のダイアログは常に拒否して閉じる。ダウンロードは `Browser.setDownloadBehavior deny`（失敗したら起動を中止）。
> - 画面転送は設計のSSEではなく、`GET /api/rb/frame?tab=&since=` のロングポーリング（最長10秒、最新フレームのみRAM）。フレームのackは次のポーリング時（最大10fps）に送り、閲覧者がいなければChromiumの送信が止まる。閲覧者のポーリングが5秒途絶えたら `Page.stopScreencast`。
> - 自動停止: タブ0で5分、または閲覧者の操作が10分ない、またはCDP接続が切れた、いずれかで全ターゲットを閉じて `docker stop -t 5` → `rm`。`/api/rb/status` のポーリングは操作に数えない。Hub終了時も停止。Hub起動時に残っていたHub製コンテナは削除（クラッシュ後のメモリ解放）。
> - メモリ: タブを開く前に `MemAvailable ≥ 2GiB`（読めなければ開かない）と、稼働中なら `docker stats --no-stream`（自コンテナのみ、5秒キャッシュ）で使用率80%未満を確認。確認できないときも開かない。
> - リンク: `Runtime.evaluate` で `a[href]` を最大5000件取得し、60KB以下に分割して `rapidgator_extract` へ。結果は最大100件（既存の抽出commitの上限）。

（以下は元の設計）
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
