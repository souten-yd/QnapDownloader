# QnapDownloader — 内蔵JD2・Java・統合UIアーキテクチャ（実装設計）

設計日: 2026-11-01 / 基準: main v0.1.6（実装状況は必ずソースとQPKGを確認）

## 0. 決定と非決定

**採用案: QnapDownloaderの単一Web UI + QPKG内のヘッドレスJDownloader 2エンジン + フルJava LTSランタイム + ローカル限定の認証付き制御ブリッジ。**

QnapDownloaderの名称・QTS App Center登録・Webポート17864・iPhoneホーム画面入口を一つにする。JD2のデスクトップSwing UIは稼働させない。JD2のリンク解析・アカウント認証・プラグイン・ダウンロードはJD2がauthoritative。QnapDownloaderは操作UI、プロセス監視、インストール・アップデート調停、セキュリティ・監査のauthoritative。Web UIの状態表示はJD2実状態を取得し、架空の進捗を出さない。

現時点で**未実装**: JD2本体/Javaの同梱、ローカル認証ブリッジ、実ダウンロードのUI、アカウントUI/秘密情報操作、JD2単独更新の実機検証。既存MyJDownloader接続は代替・移行用オプションとして維持し、内蔵JD2をデフォルトに切り替えるのは全受入れゲート合格後とする。

JD2本体の第三者QPKG再配布について権利者の許可・ライセンス表示義務・依存ライブラリを確認すること。**配布可能性の根拠が確定するまで、JD2実バイナリをリポジトリや公開QPKGに入れない。** Temurin等OpenJDKバイナリは対応するライセンスおよび通知・ソース提供義務を確認して同梱する。公式JD2.jarを使用するときは署名をGPGで照合（鍵fingerprintは公式ドキュメントを参照）、SHA-256 pinとSBOM・LICENSESを付ける。起動時に不透明なURLから自動取得しない。

## 1. アーキテクチャの比較

| 案 | JD2実装資産の流用 | UI統合 | QNAP負荷 | 実装難易度 | 判定 |
|---|---|---|---|---|---|
| A: 外部Docker JD2 + MyJD | 高い | 中（他のコンテナへの依存が残る） | 既存のまま | 低～中 | 移行期間の互換モード |
| B: 内蔵JD2ヘッドレス + Hub Web + 内部認証ブリッジ | 高い | 最高（入口/操作を一つに統一） | GUIなしで比較的軽い | 高（内部ブリッジ検証・配布・更新） | **推奨** |
| C: 内蔵JD2のSwing GUIを遠隔表示 | 高い | 低（二重UI） | X/描画/配信の負荷が増える | 中 | 不採用 |
| D: JD2を書き直した独自ダウンローダー | 低い（ホスト別解析/プレミアム認証の再実装） | 高い | 最適化可能 | 非常に高い・互換性リスク | 不採用 |

Javaの「headless」はJavaランタイムを削った`-headless` OSパッケージを意味しない。JD2公式は**GUI非表示でもフルJRE/JDKが必要**と説明。Java 17/21 LTSなど、実際に動作検証したx86_64 Linuxランタイムを使用する。

## 2. QPKG/プロセス/永続領域

```text
QnapDownloader.qpkg
  shared/
    downloader.sh
    webapp.py / static UI
    engine-supervisor.py                  # 別PRで実装
    jd2-bridge/                           # 権限分離された限定API
    runtime/java-x64/                     # pinned full Temurin Java runtime
    jd2-seed/                             # 配布許諾が確認された公式JD2 seed
    manifest.json / LICENSES/ / SBOM
Persistent root (QPKG install dirの外)
  /QnapDownloader-data/
    config/                               # Hub設定・管理者認証（トークンによる初回登録は不要）
    db/                                   # Hub SQLite + JD2との対応ID
    engine/active/                        # JD2本体の書込可能更新コード
    engine/previous/                      # ロールバック用検証済みスナップショット
    jd2/cfg/                              # JD2 accounts.ejs/プラグイン設定等
    jd2/logs/                             # 上限/ローテーション
    downloads/                            # 管理者選択の別NAS共有フォルダを利用
    backups/                              # バージョン互換性と権限を検証
Optional RAM tmpfs
  private-java-cache/                     # Java実行用の読取キャッシュ・短命ファイルのみ
```

実際のJD2インストールはJD2更新処理が直接ファイルを変更し得る。更新後にファイルが永続するディレクトリで運用するか、公式アップデータが書いた内容を停止・署名/整合性検証した後に永続スナップショットへpromoteする方式を実機で比較する。**更新可能なJD2全体を無条件にtmpfsへ配置しない。** 設定・パスワード・キュー・未完了ダウンロード・プラグイン状態・ログは永続領域に置き、RAM上のものは再起動で消えてよいデータだけとする。

既存Docker JD2は自動停止・移行・リセット・削除しない。内蔵モード選択時も既存Dockerのファイルや設定を読みに行かず、明示操作＋事前バックアップ＋差分プレビュー＋確認が必要。二重エンジンが同じ保存先に並行書込しないよう、単一ライター排他を設ける。

## 3. TS-253Be向けJava/RAM/I/O方針

公式TS-253BeのSKUは2GB/4GBで、増設後の最大は8GB。搭載RAMをQNAP実機から読み、他QPKG・QTSが使う余裕を確保する。JD2公式はheadless推奨256MBだが、これは**QNAP上での実運用総使用量の保証ではない**。

1. **既定**: Javaの変更不可ファイルはQPKG内の永続ストレージ、Linux page cacheに再利用させる。Java heapは運用計測に基づく上限を付ける（初期案 -Xmx384m / 4GB機、2GB機ではより小さくし起動ゲートを慎重に設計、8GB機で段階調整）。環境依存の実測前に固定上限を押し付けない。
2. **オプション**: 管理者が「RAMキャッシュを有効化」したときのみ、空きメモリ・tmpfs mount許可・QTSの起動条件を検査し、メモリ上のJava実行ファイル一式を一時展開。共有`/dev/shm`へ無制限にコピーしない。privileged mountや`exec`が必要になる機種があるので自動的にmountオプションを変えない。失敗時はエラーで通知し**勝手にデータ配置を変更しない**。
3. RAMコピー時に起動ディスクの一回の読込が発生。page cacheもRAMを消費するため、RAMコピーは常に高速・低I/Oになるわけではない。ベンチマークをした上で、保守しやすい既定方式を決める。
4. ログ、プラグイン更新、JD2アカウント、メタデータ、未完了DL、一部アーカイブ処理は書込が不可避。ダウンロード成果物は当然HDDへ書込。目標は**不要な小刻みな書込を減らすことで、ディスクI/Oゼロではない**。
5. QNAPのOOM・swap・温度・空きメモリ・HDD read/write・起動時間・リンク数別に実測し、RAM不足ではRAM展開を止める（実行中の別アプリをkillしない）。

## 4. エンジン通信と認証の要件（最重要スパイク）

`Hub Web => Hub backend => engine transport => JD2`

ローカルエンジンのAPI通信方式を確定するPoCが第一の実装ブロッカー。

- **優先**: JD2で実装・保守できる署名/認証付きの最小限のJD2内部ブリッジ（Unix domain socketまたはOS権限でアクセス制限したループバック）を検証。JD2内部APIとの互換性、プラグイン署名/ロード方法とJD2更新での維持を調べる。Hubが許可済みAPIリクエストのみを発行し、秘密情報は固定の認証・暗号化経路のみ通す。
- JD2の旧deprecated RemoteAPIをLANや0.0.0.0へ開放しない。認証のないループバックAPIを「NASの127.0.0.1だから安全」と決めつけない（QNAPの他のローカルユーザー/コンテナから届き得る）。OS peer-credential/アカウント分離でアクセス元を固定し、Hubのセッション認証・CSRF・Origin制約を経由させる。
- JD2公式MyJDownloader APIはバックアップ方式。JD2とHubを単一QPKGにした場合、MyJDアカウントがなければ使えない構成は目標から外れる。
- いずれもリモート命令の任意実行、JD2 EventScripterへの自由入力、管理画面からのPython/Java実行口、外部サイトからの直接API呼出は禁止。
- Engine stateは`not_installed/ready/running/updating/restarting/needs_attention/failed`をBackendが管理し、無効操作は拒否。起動/停止/更新は単一ロック、JD2側とのreadbackで完了判定。ログ・例外をAPI経由で生の秘密情報付きで返さない。

## 5. ひとつのWeb管理画面

ナビゲーション: **概要 / リンク収集 / ダウンロード / プレミアムアカウント / 安全ブラウザ / 設定 / 更新**。

- 概要: Hub/JD2/Javaのバージョン、接続方式、RAM/CPU、ディスクI/O、キュー総数、進行中件数、エラー、JD2更新有無
- リンク収集: 既存の手動保留・Rapidgator貼付抽出を維持。LinkGrabberの実プレビューを表示。自動開始OFF維持。個別/一括の確認・保存先指定
- ダウンロード: パッケージ/個別リンク一覧、状態、速度、進捗、ETA、サイズ、ホスト/アカウント、キュー順、開始・一時停止・再開・明示的削除、エラー詳細。JD2`downloadsV2/queryLinks/queryPackages`や`downloadcontroller` APIのデータをmapする。削除はファイル削除オプションを初期OFFとし、破壊操作は必ず確認
- プレミアムアカウント: 別仕様`docs/premium-accounts-design.md`。サービスごとの追加・状態・有効/無効・再確認・更新/削除・期限/残容量/利用優先度。秘密情報の非露出が前提
- 安全ブラウザ: 既存の隔離型iframeプレビューとSafariへの切替を維持。外部保護（Cloudflare、X-Frame-Options）を迂回しない
- 設定: 保存先、同時接続、帯域・低I/O設定、通知、利用モード（内蔵/外部互換）、Java RAMキャッシュ設定
- 更新: **Hub QPKG / JD2 Core+プラグイン / Javaランタイム**を独立した項目・バージョン/結果/ログに分割

更新・ダウンロードはユーザーの明示操作。デフォルトの自動開始、自動解凍、自動削除、パッケージサブフォルダ作成はOFFにする。JD2更新後の推奨設定再適用はプレビュー/差分/個別ルール確認/適用後readback/失敗時rollbackを要求し、既存のPackagizerユーザールールを勝手に初期化しない。

## 6. JD2独立アップデート

- Hubバージョン(`QPKG_VER`)、Java build、JD2 Core revision、JD2 plugin revisionを**別々に追跡**する。
- JD2独立更新では公式JD2アップデータを使用。Hub/Java/QPKGを変更しない。更新前は新規ジョブ投入停止・状態snapshot・cfg/アカウントDBと更新可能なJD2ファイルの一貫性あるバックアップ・容量/権限確認。
- JD2が更新のため再起動するとき、Hubはプロセス死活を監視し、アップデータ中の意図的停止と異常終了を区別する。更新後にrevision/checksum/ready/queue/accounts/plugin機能を確認。失敗した場合は既知の正常版へ戻せる設計とし、JD2のデータフォーマット後方非互換には注意して自動リストアを安易に行わない。
- Java更新は別の検証済みランタイムディレクトリへ展開し、起動ヘルス・ロードテストが済んだ場合のみ切替。HubのRC/stable更新は既存のSHA-256・backup・install healthゲートを維持。
- 初回QPKG起動時にnpm build/pip install/JD2の不透明なremote jar取得を行わない。更新は管理者の明示操作と検証済み公式アーティファクトのみ。

## 7. 実装順序（PR分割）

1. **PR-Engine-0 — 調査/Spike/受入れ基盤**: 配布権利確認、JD2 signed jar + full Java LTS のQNAP x86_64互換、ローカル認証ブリッジ方式、2/4/8GB RAM測定。既存外部JD2を変更しない。**これが以後の実装のブロッカー**。
2. **PR-Engine-1 — QPKG内蔵ペイロード/永続化**: Javaのバージョン固定+ハッシュ/SBOM/ライセンス、署名検証したJD2初期seed、永続ディレクトリ、バックアップ、再起動後の自動起動、起動失敗時rollback。最初はopt-in、既定外部モードを維持。
3. **PR-Engine-2 — Engine監督/限定API**: ヘルス/バージョン/起動/停止、ソケット権限、JD2本体の稼働・設定readback、単一ロック、ログマスク。Hubの実施権限を持つ認証済み管理者からだけ。
4. **PR-Engine-3 — ダウンロードUI(read-only first)**: JD2実データを取得してパッケージ・リンク・進捗・速度・ETA表示。読み出し専用で実機テスト後、確認付き開始/停止・再開・整理を別コミットで追加。
5. **PR-Engine-4 — プレミアムアカウント(read-only first)**: status・期限・残容量・対応ホストを安全なフィールドに正規化。HTTPS経由の認証情報追加/更新、有効・無効、再確認、確認付き削除。Cookieログインとサービス別ログイン方式を独立アダプタで扱う。
6. **PR-Engine-5 — JD2独立更新**: 停止・バックアップ・公式更新・再起動・revisionとプラグイン検査・推奨設定再適用/差分・失敗時保留。
7. **PR-Engine-6 — RAMキャッシュ/低I/O/性能**: 実測比較（page cache vs tmpfs）、2GBモデルガード、スワップ急増防止。失敗時に設定を勝手に変えない。
8. **PR-Engine-7 — 統合と移行**: 画面の一貫性、iPhone Safari・QNAP App Center、既存外部MyJD互換、移行ウィザード（preview+backup+rollback）、QDK build/TS-253Be実機・更新試験。stable化は全受入れ完了後。

PRごとにmainの実ファイル・QPKG manifest・docs・tests・UI・安全grepを直接確認。PR本文やCIの成功だけで実機動作を保証しない。小規模修正は次のPRにまとめ、レビューが困難な機能は追加PRを許容。

## 8. 受入れゲート

- QNAP TS-253Be 2/4/8GB条件でメモリガード、QTSブート/アプリ再起動/QPKGアップデート後の状態保持
- QPKGからJD2を起動して1個のWeb UIでアカウント/ダウンロード表示、外部MyJDアカウントなしでも操作可能（内蔵エンジン認証ブリッジ合格後）
- ダウンロード進捗・速度/ETAとJD2実状態が一致、開始/停止/再開・破壊操作確認、ホスト別エラー
- プレミアムアカウントの状態表示、資格情報のログ/API非露出、TLS無しの追加/更新拒否、cookie/API-keyログイン差異
- JD2独立更新後もHub Webのログインと設定/保存先/アカウント/キュー維持、プラグイン整合、QPKGアップデートは独立
- ジャーナル/失敗時の一貫性・異常終了・データ消失/過剰書込/port exposure/旧deprecated APIの未使用
- 既存Docker/JD2は停止・削除・設定変更しない、外部モードに戻せる
- QPKG単体の正式配布可能性（権利者・OpenJDK・署名/SBOM）を確認、実機インストール検証済み

## 参考

- https://support.jdownloader.org/en/knowledgebase/article/install-jdownloader-on-nas-and-embedded-devices
- https://support.jdownloader.org/en/knowledgebase/article/how-to-verify-integrity-of-our-own-alternative-installer-setups
- https://support.jdownloader.org/en/knowledgebase/article/portable-jdownloader-version
- https://my.jdownloader.org/developers/
- https://www.qnap.com/en/product/ts-253be/specs/hardware
- https://adoptium.net/support
