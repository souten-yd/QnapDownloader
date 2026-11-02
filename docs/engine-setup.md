# 内蔵JD2エンジンの初期設定（v0.1.11 / ロードマップM1、v0.1.15でコンテナ方式を追加）

> **v0.1.15:** 既定は「コンテナ方式」（下の「コンテナ方式」節）。Container Stationが無い環境では、以下に書く従来の「QTS直接実行（ネイティブ）方式」が詳細設定からのフォールバックとして使える。両方式は同時に動かさない（単一ライター）。

## 決定事項（2026-11-02）
- **MyJDownloader連携は削除する**（利用者の指示）。JD2との通信はNAS内ローカル接続のみ。
- **JD2本体とJavaはQPKGに同梱しない。** 管理画面の「初期設定を開始」を管理者が押したときだけ、固定URLの公式配布物を取得し、固定SHA-256で検証して永続データ領域へ配置する。これにより再配布許諾の問題を避ける。
- 既存の外部Docker版JD2には一切触れない（停止・設定変更・ファイル参照なし）。

## 取得するコンポーネント（`engine_components.py` に固定）
| 名前 | 取得元 | 検証 |
|---|---|---|
| Java 21 JRE（Eclipse Temurin 21.0.13+9, x64 Linux） | `https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jre_x64_linux_hotspot_21.0.13_9.tar.gz` | SHA-256 `a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd`, 52,181,297 bytes（2026-11-02に取得・照合済み） |
| JDownloader.jar（公式インストーラ） | `https://installer.jdownloader.org/JDownloader.jar` | SHA-256 `388a1d028e65cbc63933a778ddbc9267c14e7e7858389ce31a10be7308adeb40`, 5,189,769 bytes（公式NAS導入手順の掲載値） |

- ハッシュ不一致・サイズ超過・HTTPS以外へのリダイレクトは失敗とし、配置しない。公式側が更新されて不一致になった場合は「公式の値と一致しません。QnapDownloaderの更新を待ってください」と表示する（ピン値はHubのリリースで更新）。
- JD2本体のその後の更新はJD2自身の公式アップデータ（署名検証つき）に任せる。Hub QPKGの更新とは独立。

## 配置（永続データ領域 `DOWNLOADER_DATA_DIR` 配下）
```
engine/
  .download/            取得途中のファイル（*.part）。検証後に移動、失敗時は削除
  java/jdk-21.0.13+9-jre/   検証・展開済みJRE（展開は staging→rename の原子的切替）
  jd2/                  JDownloader.jar と JD2 が自己更新・設定・キューを書く場所
  jd2/cfg/org.jdownloader.api.RemoteAPIConfig.json   初回起動前に作成（下記）
  state.json            インストール済みバージョン・ハッシュ・自動起動フラグ（変更時のみ書込）
```
- tar展開は安全検査（絶対パス・`..`・デバイスファイル・ディレクトリ外を指すリンクを拒否）後に行う。
- 空き容量 400MB 未満、保存先未選択、Python/プラットフォームがx86_64 Linux以外なら開始しない。

## 初期設定の手順（ボタン1回で順に実行、各段階を画面に表示）
1. **事前確認**: 保存先選択済み・空き容量・搭載メモリ（`/proc/meminfo`）。
2. **Java取得** → **検証** → **展開** → `bin/java -version` 実行確認（20秒上限）。検証済みなら再実行時はスキップ。
3. **JD2取得** → **検証** → `engine/jd2/JDownloader.jar` へ配置。
4. **ローカル接続設定の作成**: `cfg/org.jdownloader.api.RemoteAPIConfig.json` に `{"deprecatedapienabled": true, "deprecatedapiport": 3128, "deprecatedapilocalhostonly": true}`。既存ファイルがあれば上書きせず中止して理由を表示。
5. **初回起動・公式アップデート**: `java -Xmx<RAM別> -XX:+UseSerialGC -Djava.awt.headless=true -jar JDownloader.jar -norestart` を `engine/jd2` で実行。JD2が自己更新のため終了したら再実行（最大6回・各10分上限）。標準入力は `/dev/null`、出力はRAMのみ（最新300行）。
6. **常駐起動**: 同じ引数で起動し、Hubが監視（`start_new_session`、PIDと開始時刻を記録）。
7. **ローカル接続の安全確認**（下記）→ `jd/getCoreRevision` で応答確認。
8. **保存先と推奨設定**: `config/list` で実在が確認できたキーだけを使い、既定の保存先を選択済みフォルダに設定し、既存の推奨設定（自動開始OFF・自動解凍OFF・パッケージ別フォルダOFF等、`jd2_policy.py`）を適用してreadbackで確認。
9. 完了。以後Hub起動時にエンジンを自動起動（設定でOFF可能）。

失敗した段階で停止し、理由とRAMログを表示する。再実行は検証済みの段階を飛ばして続きから行う。

## メモリ（Javaヒープ）
`MemTotal` < 3GiB → `-Xmx256m`、< 6GiB → `-Xmx384m`、それ以上 → `-Xmx512m`。実機計測後に調整。

## ローカル接続の安全確認（毎回の接続前）
1. `/proc/net/tcp` と `/proc/net/tcp6` から 3128 のLISTENを列挙。**すべて** `127.0.0.1` / `::1` / `::ffff:127.0.0.1` であること。0.0.0.0 等があれば拒否。
2. そのソケットinodeを、Hubが起動・記録したJavaプロセス（PID・開始時刻・cmdlineにエンジンのjarパス）が保有していること（`/proc/<pid>/fd` の `socket:[inode]`）。別プロセスなら拒否。
3. 呼び出しはHub内の固定許可リストのメソッドのみ（任意メソッドの中継なし）。ブラウザからJD2へは直接届かない。
4. 残余リスク: 同一ホストのroot権限プロセスは到達可能（Hubと同じ信頼境界）。コンテナ（別ネットワーク名前空間）からは届かないことを実機で確認する。

## ローカルAPIの呼び出し形式（実機で確認する前提）
`GET http://127.0.0.1:3128/<namespace>/<method>?<引数1のJSONをURLエンコード>&<引数2…>`。応答JSONが `{"data": …}` ならその中身を使う。タイムアウト5秒、応答最大4MiB。
許可リスト: `jd/getCoreRevision`, `update/isUpdateAvailable`, `update/runUpdateCheck`, `update/restartAndUpdate`, `config/list`, `config/get`, `config/set`, `downloadsV2/queryPackages`, `downloadsV2/queryLinks`, `linkgrabberv2/addLinks`。

## 画面
- 新しいタブ（ホーム）: エンジン未導入なら「JD2エンジンの初期設定」カード＋「初期設定を開始」ボタン。
- 設定 › **エンジン**（`#/settings/engine`、旧「JD2接続」を置換）: 状態、各コンポーネントのバージョンとハッシュ、状態に応じた主操作1つ（初期設定を開始／起動／稼働中＋停止）、開いたときの自動接続確認（v0.1.12で「接続確認」ボタンを廃止）、自動起動、設定中/失敗時の段階一覧と進捗（成功後は折りたたみ記録）、「詳細」（コンポーネント・版・ハッシュ）、「ログ」（RAM・最新300行）。JD2の更新確認・推奨設定の再適用もこの節に移動した。ホームの「はじめに」カードからも初期設定を開始できる。
- 進捗の取得は初期設定の実行中かつ画面表示中のみ1.5秒ごと。
- 開始前の確認ダイアログに、取得元・サイズ・配置先・ダウンロードは自動開始しないことを明記。

## API（管理者認証・CSRF・Origin必須）
- `GET /api/engine/status` — 状態、段階、バージョン、自動起動、エラー理由
- `POST /api/engine/setup {"confirmed": true}` — 202。実行中は409
- `POST /api/engine/start` / `POST /api/engine/stop {"confirmed": true}`
- `POST /api/engine/autostart {"enabled": bool}`
- `GET /api/engine/log` — RAMログ（秘密情報は含めない）

## MyJD削除に伴う変更
`jd2_myjd.py`・`tests/test_jd2_myjd.py`・`docs/myjd-setup.md`・`requirements-jd2-build.txt`（myjdapi）を削除。ビルド・検証スクリプト・README・各docsから参照を除去。NAS上の既存 `config/myjd.json` は削除せず無視する。

## 非対象（後続）
JD2ログのRAM化・HDDアクセス計測（M3）、ダウンロード操作ボタン（M2）、Javaの自動更新（ピン値更新はHubリリースで行う）。


## コンテナ方式（v0.1.15、`docs/container-runtime.md` §2〜3）

実機未検証。`engine/state.json` に `mode`（`container` / `native`）を記録する。モード未記録の既存導入は `native` として扱う。

### 実行形態
- Docker CLIは `container_runtime.py` の固定関数だけで使う（`/sbin/getcfg container-station Install_Path -f /etc/config/qpkg.conf` → `<path>/bin/docker`、無ければ `/usr/local/bin/docker`）。list引数・タイムアウト・最小環境変数、シェル不使用。ラベル `org.qnapdownloader.managed=1` かつ名前 `qnapdl-` 始まりのHub自身のコンテナ以外は、一覧も停止も削除もしない（既存のDocker版JD2を含む）。
- イメージはダイジェスト固定の `eclipse-temurin:21-jre@sha256:0b99b337…6901`。専用ネットワーク `qnapdl-jd2-net`（bridge、ラベル付き）に `qnapdl-jd2` を1台だけ置く。**`-p` なし**。
- `docker run -d --restart no --read-only --log-driver none --memory 1g --memory-swap 1g --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges --user <uid>:<gid>`。マウントは `engine/jd2`→`/jd2`（rw）と選択済み保存先→`/downloads`（rw）のみ。`--tmpfs /jd2/logs:size=64m`、`--tmpfs /tmp:rw,exec,nosuid,nodev,size=64m`（JNA等のネイティブヘルパーを読み込めるようexec。`/jd2/logs` はnoexecのまま）。コマンドは `java -Xmx512m -XX:+UseSerialGC -Djava.awt.headless=true -jar /jd2/JDownloader.jar -norestart`。
- 再起動ポリシーは付けない。Hubが監督する（Hub起動時に自動起動設定なら起動、落ちたら既存のバックオフで再起動）。停止は `docker stop -t 30` の後、自コンテナだけ `docker rm -f`。Hub終了時もコンテナを止める。
- 起動前に `MemAvailable ≥ 1.5GiB` を確認し、不足なら起動せず理由を表示する（他アプリは止めない）。
- 実行ユーザー: `engine/jd2` の所有者 uid:gid。Hubがroot所有のデータを見つけた場合のみ `engine/jd2` 配下を 1000:1000 に変更する（シンボリックリンクは辿らない）。rootでは実行しない。保存先がそのuidで書込できない（モードビットで判定。ACLは評価しない）場合は理由を表示して起動しない。
- コンテナのログは保存しない（`--log-driver none`）。コンテナ方式では画面の「ログ」にJD2の出力は出ない（Hubの操作ログのみ）。

### ローカルAPIと接続の安全確認
- `cfg/org.jdownloader.api.RemoteAPIConfig.json` はコンテナ方式では `{"deprecatedapienabled": true, "deprecatedapiport": 3128, "deprecatedapilocalhostonly": false}`。コンテナ外（ホスト）から届かせるため。到達できるのは、ポート非公開の専用ブリッジネットワーク上のコンテナIPだけ。
- モード切替でこのファイルだけを書き換える。JD2は初回起動後に項目を追記するため、JSONオブジェクトとして読み、`deprecatedapienabled` がtrue（または無し）かつ `deprecatedapiport` が3128（または無し）のときだけ、`deprecatedapienabled`・`deprecatedapiport`・`deprecatedapilocalhostonly` の3項目を設定し、他の項目は保持して原子的に書き込む。JSONでない・オブジェクトでない・ポートが別の値・無効化されている場合は理由を表示して中止する。ネイティブ起動時のみ例外として、更新できない場合は変更せず続行する（loopback限定のリスナー検査が残るため）。
- ホスト→JD2は、`docker inspect` で次を**毎回**確認したコンテナIP:3128へ接続する（結果は最大10秒キャッシュ）: ラベルが付いている、起動中、接続先ネットワークが `qnapdl-jd2-net` のみ、公開ポートなし、IPがプライベート。コンテナ方式では `/proc` のリスナー所有者検査は行わない（置き換え）。
- 保存先の変換: JD2から見る保存先は `/downloads`。送信先は起動時にマウントした保存先の配下だけが `/downloads/…` に変換され、外は拒否する（保存先を変更したらエンジンを再起動してマウントし直す）。

### 初期設定の段階（コンテナ方式）
Container Station検出と事前確認 → 内蔵エンジンの停止（移行時のみ） → メモリ確認 → イメージ取得（取得済みなら再利用） → 専用ネットワーク作成 → JD2取得・検証・配置（既存なら再利用、**Java取得は行わない**） → ローカル接続設定 → 初回起動・公式アップデート（コンテナが終了したら再実行、最大6回） → 常駐起動 → 接続確認（inspect と `jd/getCoreRevision`） → 保存先（`/downloads`）と推奨設定 → 完了。失敗した段階で止まり、再実行は完了済みの段階を飛ばす。

### 移行（ネイティブ → コンテナ）
画面の「コンテナへ移行」（ネイティブ導入済みでContainer Stationが使えるときだけ表示）。実行中のネイティブJD2を先に停止し（単一ライター）、同じ `engine/jd2` を使ってコンテナ版の段階を実行する。設定・キュー・アカウントは消えない。ネイティブ用のJava（`engine/java`）は削除しない。

### API
`POST /api/engine/setup {"confirmed": true, "mode": "container"|"native"}`（`mode` 省略時は導入済みならそのモード、未導入ならコンテナ。Container Stationが無い場合に自動でネイティブへ落とさない）。`GET /api/engine/status` に `mode`・`preferred_mode`・`can_migrate`・`container`（`available`/`version`/`state`/`name`/`network`/`image`/`mounted_dir`）を追加。

### 画面
設定 › エンジンに実行方式（「コンテナ (Container Station)」/「QTS直接実行」）を表示。Container Stationが無いときは「QTSのApp CenterでContainer Stationをインストールしてください」と案内し（リンクなし）、「詳細」から非推奨のネイティブ初期設定を選べる。

## v0.1.16 追加

### MyJDownloaderの自動接続を無効化（実機未検証）
ヘッドレスのJD2は起動時に `IllegalStateException: No Console Available!`（`MyJDownloaderController.askLoginsOnConsole`）を出す。MyJDownloaderの資格情報をコンソールで尋ねようとするため。対策として、ネイティブ・コンテナのどちらでも、初期設定の「ローカル接続設定の作成・MyJDownloader自動接続の無効化」段階と**毎回の起動前**に、`cfg/org.jdownloader.api.myjdownloader.MyJDownloaderSettings.json` へ `"autoconnectenabledv2": false` だけをマージ書き込みする（無ければ作成、他のキーは保持、JSONオブジェクトでない/読めない/シンボリックリンクなら上書きせず中止、アトミック書き込み。メールアドレス・パスワード系のキーは一切書かない）。**未検証**: このキー名が実機で効かず、JD2が引き続きコンソールを要求する場合は別のキー（設定一覧の `config-keys` で確認）が必要になる。

### 推奨設定（実機のキー名で確定）
`config-keys` で確認した実名に合わせ、`jd2_policy.KNOWN_KEYS` はインターフェース名の完全一致＋キー名の大文字小文字無視で照合する。
- `LinkgrabberSettings`（`org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings`）: `LinkgrabberAutoConfirmEnabled`（→false、「リンク自動確定」）、`LinkgrabberAutoStartEnabled`（→false、「追加後の自動開始」）、`AutoExtractionEnabled`（→false、「自動解凍」）
- `GeneralSettings`（`org.jdownloader.settings.GeneralSettings`）: `AutoStartDownloadOption`（enum `ALWAYS` / `ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS` / `NEVER`）。「起動時の自動開始（『常に』以外ならOK）」。`ALWAYS` のときだけ `ONLY_IF_EXIT_WITH_RUNNING_DOWNLOADS` に変更（再起動後の再開は残し、新規リンクは開始しない）。enumは文字列で `config/set` する。
- パッケージ別フォルダは設定キーに対応させない。Hub管理のエンジンへの送信時に `linkgrabberv2/addLinks` へ `overwritePackagizerRules: true` と明示の `destinationFolder` を渡し、フラットな保存先を優先する（`packagizer.package_subfolders` / `general.package_subfolders` は必須項目から外し、「送信時に強制」として扱う）。Packagizerのルール自体は変更しない。
- 4項目が確認でき適用されると、段階8は成功し `can_send` が真になる想定（実機未検証）。

### 従来方式（QTS直接実行）の残骸の削除
コンテナ方式で初期設定が完了し、ネイティブのプロセスが動いておらず、初期設定が実行中でないときだけ、`engine/java/`（展開済みJRE）と `engine/.download/` を削除できる（設定 › エンジン › 詳細）。`engine/jd2/`（JDownloader.jar・cfg・キュー・部分ファイル）は**絶対に消さない**（コンテナがバインドマウントして使う）。`engine/java` がシンボリックリンクなら拒否。状態ファイルの `java` 項目も消す。QTS直接実行には戻せなくなるが、再度初期設定すれば取得し直せる。`/api/engine/status` の `native_leftovers: {present, bytes}`（60秒キャッシュ、リンクは辿らずサイズ計算）、`POST /api/engine/native/remove {confirmed:true}`。
