# 内蔵JD2エンジンの初期設定（v0.1.11 / ロードマップM1）

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
