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
- **ディスク書込を最小化**: JD2は `--log-driver none`（ブラウザは出力をtmpfsに書き、失敗時のみ末尾を出す小さな `local` ログ。v0.1.18）、`--read-only` ルートFS、書込が必要な一時領域は `--tmpfs`（サイズ上限付き）。永続化するのはJD2のデータ（設定・キュー・アカウント・部分ファイル）とダウンロード成果物だけ。
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
> - 起動引数（v0.1.16時点。v0.1.18で下記の通り変更）: `--read-only --log-driver none --memory 2g --memory-swap 2g --shm-size 512m --pids-limit 1024 --cap-drop ALL --cap-add SYS_ADMIN --security-opt no-new-privileges --user 1000:1000 --tmpfs /profile:size=512m --tmpfs /tmp:size=256m`、`-p`・マウントなし、専用ネットワーク `qnapdl-browser-net`。seccompは既定のまま。
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

> **v0.1.18 実機修正メモ（起動直後に終了する問題の調査性と原因対策）**
> - 実機で初期設定のselftestは通るのにタブを開くと「ブラウザコンテナが起動直後に終了しました（終了コード 1）」となり、`--log-driver none` のため理由が不明だった。
> - **診断性**: `entrypoint.sh` はXvfbの出力を `/tmp/xvfb.log`、Chromiumの出力を `/tmp/chromium.log`（どちらもtmpfs）へ書く。通常運転では何も出力しない（ディスク書込は最小）。子プロセスが落ちたときだけ、`qnapdl-browser: <xvfb|chromium|socat> exited` の見出しと該当ログの `tail -n 60` をstderrへ出す。終了コード: 70 Xvfbのソケット待ち時間切れ、71 Xvfb終了、72 Chromium終了、73 socat終了、78 ユーザー名前空間なし、79 実効capabilityあり。
> - **ログドライバ**: ブラウザのみ `--log-driver local --log-opt max-size=256k --log-opt max-file=1`（上限付き・1ファイル）。JD2は従来どおり `none`、selftest（`--rm`）も `none`。
> - **Hub側**: 起動直後の終了を検出すると、コンテナを削除する前に固定操作 `logs_tail("qnapdl-browser")`（`docker logs --tail 80`。名前固定・Hubラベル確認・制御文字除去・末尾2000文字）で末尾を読み、RAMログ（設定 › NASブラウザ › ログ、`GET /api/rb/log`）へ全文、エラー表示（トースト）へ先頭300文字相当（末尾300文字）を載せる。終了コードは日本語に変換（70 画面サーバーの起動待ちが時間切れ／71 画面サーバー(Xvfb)が終了／72 Chromiumが終了／73 中継(socat)が終了／78・79 従来文言）。ログ取得に失敗しても元のメッセージだけで続行する。
> - **原因対策（推定）**: `--tmpfs /profile:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700`（非rootのChromiumが書けるように所有者を指定）、`--tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777`。Chromiumに `--disable-gpu --password-store=basic` を追加。`HOME=/tmp` と `/tmp/.config`・`/tmp/.cache` を起動前に作成。サンドボックスは無効にしない。
> - **イメージ更新**: `entrypoint.sh` が変わるためタグ（内容ハッシュ）が変わる。新タグが無く、古いHub製イメージだけがある場合、`/api/rb/status` は `update_needed: true`、理由「NASブラウザの更新が必要です（初期設定を再実行）」を返し、タブを開こうとしても同文言で拒否する（存在しないタグでの `docker run` は行わない）。設定画面のボタンは「初期設定を再実行」になる。

> **v0.1.20 実機修正メモ（SYS_CHROOT）**: 実機で Chromium が `Check failed: sys_chroot("/proc/self/fdinfo/") == 0` → "Zygote process exited prematurely" で終了した。名前空間サンドボックスは自前のユーザー名前空間内で chroot するが、Dockerの既定seccompは `chroot` を、コンテナのcapabilityセットに `CAP_SYS_CHROOT` が含まれる場合にだけ許可する。そこでブラウザの起動引数を `--cap-drop ALL --cap-add SYS_ADMIN --cap-add SYS_CHROOT` とした（JD2は変更なし、selftestも同じ引数）。
>   - **許容できる理由**: SYS_ADMINと同様、uid 1000＋`no-new-privileges` ではSYS_CHROOTもbounding setにだけ入り、effective/permitted/ambientは空のまま（`entrypoint.sh` の `CapEff==0` 検査＝終了コード79は不変）。SYS_CHROOT単体の権限はユーザー名前空間内のchrootに限られ、ホストのルートを変更できない。
>   - **selftest拡張**: `unshare --user --map-root-user --pid --fork --net chroot / /bin/true` でサンドボックスの必要条件を再現し、失敗なら終了コード77（「サンドボックスに必要なchrootが許可されていません」）。
>   - エントリポイントが変わるためイメージタグが変わり、旧イメージでは「NASブラウザの更新が必要です（初期設定を再実行）」と表示される。

> **v0.1.19 実機修正メモ**: 実機で `docker: ... failed to initialize logging driver: compression cannot be enabled when max file count is 1` となった。`local` ドライバは既定で圧縮が有効なため、ブラウザのログ引数に `--log-opt compress=false` を追加（`--log-driver local --log-opt max-size=256k --log-opt max-file=1 --log-opt compress=false`）。また `docker run -d` 自体が失敗して「created」状態のコンテナが残る場合に備え、`run_browser` は失敗時にHubラベルを確認したうえで `qnapdl-browser` を削除する（次回の名前衝突を防ぐ。Hub管理外の同名コンテナは削除しない）。UIの同一エラートーストは5秒以内の重複を1つにまとめる。

（以下は元の設計）
- イメージ: 初期設定時に `debian:bookworm-slim@…` から `chromium`・`xvfb`・`socat`・日本語フォントを入れたイメージを `docker build` で作る（Debianのセキュリティ更新付きChromium。ビルドは利用者のボタン操作時のみ）。「更新」で再ビルド。
- 起動: 非root、Chromiumサンドボックス有効（`--no-sandbox` 禁止）、Xvfb上のヘッドフル（Cloudflareに通常のブラウザとして扱われやすい。UA偽装はしない）。プロフィールは `--tmpfs`（RAM、512MB）で毎回使い捨て、`--disk-cache-size=1`、クラッシュレポート無効。CDPは内部で `socat` によりコンテナIP:9223 に中継（ポート公開なし）。
- メモリ: `--memory 2g`。新しいタブを開く前に `MemAvailable ≥ 2GiB` かつコンテナ使用量が上限の80%未満を確認し、満たさなければ開かない（理由を表示）。同時タブ数上限 3。
- **解放**: Hubのタブを閉じたらCDP `Target.closeTarget` で該当レンダラを終了。ブラウザタブが0になって5分経過、または全利用者が10分操作しなければコンテナごと停止（`docker stop` → `rm`）。次回は数秒で再起動。
- 画面転送: CDP `Page.startScreencast`（JPEG、端末幅×DPR、品質60、最大10fps、画面非表示中は停止）。Hub→ブラウザはSSE、入力（タップ・スクロール・文字）はPOSTで受けCDP `Input.*` に変換。iPhoneへ外部サイトの通信・スクリプトは一切届かない。
- リンク抽出: CDP `Runtime.evaluate` で表示中ページのリンク（Rapidgator等）を取得し「N件を追加 / JD2へ送る」。CAPTCHAの自動解答・自動クリックは行わない（利用者が画面越しに操作）。
- ダウンロード: ブラウザ内のファイルダウンロードは無効（`Browser.setDownloadBehavior deny`）。ファイル取得はJD2経由のみ。

> **v0.1.22 実機フィードバック対応（速度とリンク検出・実機未検証）**
> - **画面サイズを閲覧端末に合わせる**: `POST /api/rb/viewport {tab,width,height,dpr}`（幅320〜1600・高さ320〜2400のCSSピクセル整数、dpr 1〜3）。Hubは `Emulation.setDeviceMetricsOverride {width,height,deviceScaleFactor:min(dpr,2),mobile:width<760}` を送り、モバイルのときだけ `Emulation.setTouchEmulationEnabled`。**ブラウザの識別文字列は変更しない（プロジェクト規則）**。スクリーンキャストの `maxWidth/maxHeight` は CSSサイズ×min(dpr,2)、JPEG品質はモバイル50・デスクトップ60。未送信のタブは従来の1280×2000/品質60。実行中に寸法が変わると `Page.stopScreencast`→`startScreencast` で再開する。Xvfbは1600x2400x24、Chromiumの `--window-size=1600,2400`（上限の寸法でも収まる）。
> - **ackは到着の即時**: `Page.screencastFrame` をCDP読み取りスレッドで受けた瞬間に `Page.screencastFrameAck` を返し（応答は待たない）、タブごとに最新フレームだけを保持する。閲覧者の次のポーリングを待たないのでChromiumは止まらない（閲覧者が5秒いなくなれば従来どおり `stopScreencast`）。
> - **バイナリフレーム**: `GET /api/rb/frame.jpg?tab=&since=&wait=[&rev=]` は `image/jpeg` を返し、ヘッダ `X-Frame-Seq`/`X-Frame-Width`/`X-Frame-Height`（CDPメタデータのCSSサイズ。タップ座標の換算に使う）/`X-Frame-Url`（パーセントエンコード）/`X-Frame-Title`（同、200文字まで）/`X-Frame-Loading`(0|1)/`X-Frame-Intercepts`（このタブの検出イベントの累計数）/`X-Frame-Rev`/`X-Frame-Blocked` を付ける。新しいフレームが `wait` 秒内に無ければ **204**（同じヘッダ付き）。`rev` を付けると、フレームが無くてもURL・タイトル・読み込み状態・検出の変化で即座に返る（検出イベントが画面更新を伴わないため）。JSON版 `/api/rb/frame` は互換のため残し、`intercepts`/`rev` が増えた。`Cache-Control: no-store` のまま。CSPは `img-src 'self' data: blob:`（`blob:` はこの表示のためだけ）。
> - **ホスター検出（Fetch）**: タブごとに `Fetch.enable` を、resourceType `Document` かつ `*://rapidgator.net/file/*`・`*://*.rapidgator.net/file/*`・`*://rg.to/file/*` の3パターンだけで有効にする（他のリクエストは一切止めない。有効化に失敗してもタブは開く＝ログのみ）。`Fetch.requestPaused` で `rapidgator_extract.normalize` できるURLは `Fetch.failRequest (Aborted)` して `{kind:"hoster", url:正規化済み}` をRAMに記録（タブごと最大20件、直近2秒の同一イベントは1件にまとめる）。正規化できないURLやDocument以外は `Fetch.continueRequest` で素通し。
> - **ポップアップ**: 自タブ由来（`openerId` が自タブ）の新規ターゲットは、最初のURL（`targetInfoChanged`、最長3秒）を待って閉じ、ホスターURLなら検出イベント `hoster`、`browser_safety` を通るhttpsなら検出イベント `popup`（**開いた元のタブは自動では動かさない**。広告ポップアップでタブを乗っ取られないため。閲覧者が「開く」を選んだときだけ `intercepts/open` で遷移）、それ以外は「ポップアップをブロックしました」を通知。自タブ以外由来は従来どおり即閉じ。
> - **ダウンロード**: `Browser.setDownloadBehavior {behavior:"deny", eventsEnabled:true}`。保存は拒否のまま、`Browser.downloadWillBegin` のhttps URLを `{kind:"download"}` として記録（`frameId` がタブのターゲットIDと一致すればそのタブ、不明なら直近でポーリングされたタブ）。
> - **検出API**（要ログイン、POSTはCSRF/Origin）: `GET /api/rb/intercepts?tab=&since=` → `{items:[{id,kind,url}]}`。`POST /api/rb/intercepts/open {tab,id}`（`hoster`/`popup` のみ。そのURLを `Page.navigate`。ホスターはそのURL1回だけ30秒間Fetchを素通しにし、ポップアップ（非ホスター）は素通し設定なし）、`/add`（`store.add_link`）、`/send`（`can_send` を確認→JD2ロック内で追加→既存の `/api/links/send` と同じ `prepare_transfer`→`begin_send`→`transfer` の経路。確認ダイアログはクライアント側）。
> - 変更したChromium引数: `--disable-features=Translate,MediaRouter,OptimizationHints --renderer-process-limit=2 --disable-extensions --disable-component-update --mute-audio`（サンドボックスは有効のまま、識別文字列系の引数は無し）。イメージのタグはentrypointのハッシュなので、更新後は「初期設定を再実行」が必要。

> **v0.1.24 実機フィードバック対応（速度・広告・長押し確認・サイトごとの設定。実機未検証）**
> - **Chromium引数の追加**: `--num-raster-threads=4 --disable-smooth-scrolling --autoplay-policy=user-gesture-required`。`--enable-low-end-device-mode`（画質が落ちる）と `--disable-background-timer-throttling`（既定のまま）は**使わない**。コンテナに `--cpus` / CPU上限は付けない（`--memory 2g`・`--pids-limit`・capabilityの制限だけ。テストで確認）。entrypointのハッシュがイメージタグなので、更新後は「初期設定を再実行」が必要。
> - **描画品質（セッションごと）**: `POST /api/rb/viewport` に任意の `quality`（`speed`/`standard`/`high`、省略時はそのタブの現在値＝初期値 `speed`）。`deviceScaleFactor = min(dpr, 1 / 1.5 / 2)`、スクリーンキャストの `maxWidth/maxHeight` は CSSサイズ×その倍率、JPEG品質は 40 / 55 / 70。変更時は実行中のスクリーンキャストを再起動する。v0.1.22の「品質50/60・倍率上限2」は置き換わった。ブラウザの識別文字列は変更しない。
> - **モーション**: 各タブに `Emulation.setEmulatedMedia {features:[{name:"prefers-reduced-motion",value:"reduce"}]}`（失敗してもタブは開く）。
> - **広告・トラッカー遮断**: Hub内の `rb_blocklist.py`（約160ドメインの固定リスト。コンテナイメージには入れない）から、各ドメインについて `*://<domain>/*` と `*://*.<domain>/*` を生成し、タブごと・長押し確認用ターゲットごとに `Network.enable` → `Network.setBlockedURLs {urls}` を送る。ChromiumがマッチしたリクエストをキャンセルするだけでHubは何も取得しない。**ホスター（rapidgator.net / rg.to とそのサブドメイン）は `NEVER_BLOCK` で常に除外**（リストに書いても出力されない。テストで確認）。短縮サービス／リダイレクタ（adf.ly等）は長押し確認が辿れなくなるため載せない。設定 › NASブラウザ「広告をブロック」（既定ON、`settings.block_ads`）とサイトごとの `block_ads` の両方がONのときだけ適用。ページのホストが変わるたびに判定し直し、必要ならパターンを入れ替える（設定を変えたときは次に開くタブから）。
> - **サイトごとの設定**: SQLite `site_rules(host PRIMARY KEY, auto_send INTEGER, popup TEXT CHECK(popup IN ('ask','allow','block')), block_ads INTEGER)`（最大200件）。キーは**NASブラウザのタブが今開いているページのホスト**（最後に表示したhttpsページ。ホスト自身→親ドメインの順に照合、TLDは除く）。`CREATE TABLE IF NOT EXISTS`＋`PRAGMA table_info` で `block_ads` が無い古い表だけ `ALTER TABLE ADD COLUMN`。同じ値の保存は書き込まない。
>   - `auto_send=1` のサイトでホスター検出（`hoster`）が起きたら、シートを出さず**既存の送信経路**（`send_link`＝`prepare_transfer`→`begin_send`→`transfer`、JD2ロック内、リンク意図の重複排除つき）で即送信し、検出イベント `auto_sent`（既に送信済みなら `dup:true`）を記録する。エンジンが送れない状態なら通常のシート、送信が失敗したらシート＋理由の通知に戻る。ダウンロード検出・ポップアップは自動送信しない。
>   - `popup`: `ask`（既定。従来のトースト）／`allow`（非ホスターの安全なhttps URLを、タブ数とメモリの上限内なら**新しいNASブラウザのタブ**で開くようビューアに通知＝イベント `popup_opened {where:"new"}`、上限に達していれば開いたタブ自身で開いて `where:"current"`）／`block`（何も通知せず閉じ、タブごとの件数 `popups` を増やす＝画面に「ポップアップ N件ブロック」）。ホスターのポップアップはポリシーに関わらず検出になる。
> - **長押し確認（プローブ）**: `POST /api/rb/probe {tab,x,y}`（CSSピクセル）。Hubは対象タブで `Runtime.evaluate`（`document.elementFromPoint(x,y)` の `closest('a').href`）→ `browser_safety.validate`（https・公開DNS名のみ。Hub自身のオリジンも拒否）。URLがホスターならそのまま返す。そうでなければ `Target.createTarget {url:"about:blank", background:true}`（同じブラウザコンテキスト、**スクリーンキャストはしない**）を作り、同じ `Fetch` ホスターパターン・広告遮断（元タブのサイトルール）を入れて遷移する。`Fetch.requestPaused` のホスター、`Network.requestWillBeSent`（Document。`redirectResponse` 付きの数を `redirects` に）、そのページが開いたポップアップ（ホスターURLなら収集して閉じる）を集め、**ホスターURLが取れるまで最大15秒**（ロード完了後に4秒ナビゲーションが無ければ確定）。取れなければページ上のアンカーを `rapidgator_extract` で抽出し、`location.href` を最終URLとする。**どの経路でも `finally` でプローブのターゲットとポップアップを閉じる**。結果は `{start_url, final_url, links:[…50件まで・重複なし], redirects}`。1タブ同時1本、全体で同時2本まで、`memory_reason()`（空きメモリ2GiB・コンテナ使用率80%）を通らなければ作らない。JSダイアログは常に拒否、プローブ由来のダウンロードはどのタブにも通知しない。

> **v0.1.28 高速表示（全体表示）のサーバ側（実機未検証）**
> - **スナップショット**: `Page.getLayoutMetrics` の `cssContentSize.height`（切り上げ、最大30000 CSSピクセル、最小1）と、閲覧端末に合わせた幅（`set_viewport` の幅。未設定ならレイアウトビューポート）を測り、`Runtime.evaluate` 1回で `a[href]` の位置表（`getBoundingClientRect`＋スクロール量、httpまたはhttpsのみ、可視のみ、最大3000件）と編集可能要素（最大300件、座標のみ）を取る。Python側でも数値・http(s)・長さ・件数を再検証してから返す。ページには `MutationObserver` を1つ入れ、「DOM変更あり」フラグを `if_changed` の確認にだけ使う（変更なしならスナップショットを再利用）。
> - **タイル**: 要求されたタイルだけ `Page.captureScreenshot {format:"jpeg", quality:品質モード(40/55/70), clip:{x:0,y:i*1600,width:幅,height:min(1600,残り),scale:1}, captureBeyondViewport:true, fromSurface:true}`。**`clip.scale` は1**にした: 端末のスケール（品質モードの 1/1.5/2 倍）は `Emulation.setDeviceMetricsOverride` ですでに掛かっており、ここにも掛けると二重（dsf²）になると判断したため。定数 `SNAP_CLIP_SCALE`（実機で寸法が合わなければ変更）。
> - **RAMだけ**: タイルは `Tab.tiles`（LRU、1タブ40枚・24MBまで、超えたら古い順に破棄）。スナップショットは1タブ2つまで（新しい表示の最初のタイルが描画されるまで前のものを残し、描画後に古いものを破棄）。タブを閉じる・ブラウザ停止・ライブへ切替（`snapshot/release`）・ビューポート変更（幅・倍率・画質が変わる）で全部解放。ディスクには何も書かない。
> - **排他とメモリ**: スナップショットとタイルの描画は1タブにつき `snap_lock` で直列化。スナップショット前に既存の長押し確認と同じメモリ判定（空き1GiB以上・コンテナ使用率85%未満）を通す。
> - **タップ**: `POST /api/rb/tap-page` は `window.scrollTo({top, behavior:'instant'})` で目的のy座標が画面中央付近に来るようスクロールし、返ってきた実際のスクロール量でビューポート座標へ換算（`x - scrollX`, `y - scrollY`、ビューポート外ならクリックしない）、既存と同じ `Input.dispatchMouseEvent` を送り、ナビゲーションが始まれば最大10秒、完了を待つ。使うCDPメソッドは固定（`Page.getLayoutMetrics`・`Page.captureScreenshot`・`Runtime.evaluate`・`Input.dispatchMouseEvent` と既存のもの）。
> - **Cloudflare**: `looks_like_challenge(url, title)`（タイトルに `just a moment`／`しばらくお待ちください`、またはURLに `/cdn-cgi/challenge`）。クライアントはこの結果でライブへ自動切替する。


## 5. 安全上の残余リスク
- Hubは管理者権限でDocker CLIを使う。操作は固定関数のみ、引数はコード内定数とHubが検証した値だけ。
- ブラウザコンテナはインターネットとLANへ出られる（ブラウザとして必要）。悪意あるページがLAN内機器へ要求を出す可能性はある（通常のブラウザと同等）。将来、LAN宛て通信の遮断を検討。
- イメージ取得時のダイジェスト照合はDocker自体が行う。ビルドで入るDebianパッケージは署名検証付きのapt。

## 6. NASブラウザの常駐とウォッチドッグ（v0.1.38、実機未検証）
- **設定 `rb_resident`**（既定オフ）。オンの間、Hub起動時と切替直後に、イメージがあり `MemAvailable ≥ 2GiB` のときだけコンテナを起動し、タブ0枚でも止めない（5分/10分のアイドル停止を無効化。操作が10分ないタブは閉じてメモリを返すがコンテナは残す）。最後のタブを閉じると `Memory.simulatePressureNotification {level:"critical"}` と、`about:blank` の錨ターゲットでの `HeapProfiler.collectGarbage` を試す（失敗は無視）。オフは従来どおり。
- **待機時メモリ**: 常駐・タブ0枚のまま、トリムから60秒後に1回記録（以後5分ごとに更新、最小値・最新値・時刻をRAMに保持）。cgroupは `diskio.container_cgroup_paths` を再利用: v2 は `memory.current`（`memory.stat` の anon/file を別表示）、v1 は `memory.usage_in_bytes − total_inactive_file`。読めなければ `docker stats`（5秒キャッシュ）。Hub本体は `/proc/self/status` の VmRSS。表示は `/api/rb/status` の `resident`、`/api/state` の `rb.res/rs/idle_mib/cur_mib/restarts/lost`。
- **ウォッチドッグ**: コンテナが走っているはずの間（常駐オン、またはタブあり）に、DevTools接続が閉じた／30秒ごとの `docker inspect` で停止していたら「予期せぬ停止」とみなす。`State.ExitCode/OOMKilled/Error/FinishedAt` を読み、コンテナを片付けて再起動を予約。間隔 5秒→15秒→60秒、15分に5回まで。超えたら停止して「NASブラウザが繰り返し停止したため自動再起動を止めました（理由: …）」と「再試行」（`POST /api/rb/retry`）。各再起動の前に既存のメモリ確認（空き2GiB）を行い、足りなければ試行回数を消費せず「メモリ不足のため待機中」。Hub／利用者による停止、初期設定中、イメージ無しでは再起動しない。接続断で再接続は試みず、コンテナを作り直す（タブはどのみち失われる）。再起動後、クライアントは `lost` カウンタの変化でタブを静かに初期化し、表示時に autoReopen が現在のURLを開き直す。トーストは `restarts` の増加で1回。履歴は直近20件をRAMに保持。
- **Docker の `--restart` を使わない理由**: Dockerが自動で再起動すると、Hubのメモリ確認（NAS全体が逼迫しているときに2GiBのブラウザを立ち上げ直してしまう）と回数上限・理由表示を迂回し、再起動したコンテナにHubが接続し直す保証もない。OOMで落ちたコンテナが再び即座にOOMを繰り返す危険もあるため、再起動の判断はHubが持つ。
