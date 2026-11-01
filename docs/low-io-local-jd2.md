# QnapDownloader v0.1.8 — 低I/O運用・安全なローカルJD2事前診断

> **注記（v0.1.11）:** MyJDownloader連携は削除され、JD2との通信はHub管理のローカルエンジンのみです（docs/engine-setup.md）。本書中のMyJD記述は設計当時の経緯（履歴）です。

## 現状と目的
v0.1.7以前はQPKG起動時、Python標準出力・エラー出力をNASの永続`logs/downloader.log`へ書き込んでいた。普通の健康確認やWeb画面表示でもディスクアクセスが生じ得た。v0.1.8ではHubの通常ログをディスクへ出力せず、必要最低限の匿名化イベントだけPythonプロセス内に最大64件保存する。

## 通常運用と障害解析
- 初期値`DOWNLOADER_LOG_MODE=memory`: 起動標準出力/stderrは`/dev/null`へ。Web管理画面のログイン失敗、起動成功をイベント種別だけプロセス内RAMリング最大64件に保持し、日時以外の詳細を記録しない。Cookie、URL、アカウントID、ログイン値を保管しない。再起動で消える。
- 管理者がSSHで一時的に`DOWNLOADER_LOG_MODE=disk`を設定しQPKGを再起動すると、従来の`QnapDownloader-data/logs/downloader.log`へstderr/stdoutを保存できる。ログには例外のパスやアプリ詳細が含まれる場合があるので共有前に必ず確認する。問題解決後はmemoryへ戻す。
- 旧バージョンのdownloader.logが残っていても自動削除しない。アプリデータ全体を消さない。`downloader-diagnose`はログなしを異常と誤判定しないよう修正。
- `GET /api/diagnostics/io`: 管理者ログイン後だけ、Linuxの`/proc/self/io`から**Hub Webプロセスの累積物理I/Oカウンター**を読む。これはディスクへの書込ではない。`write_bytes`は実ディスクへ向けた書込統計であり、DB更新などの影響も含み得る。JD2/Java/Docker/他QPKG分は含まれない。結果の保存/自動ポーリングなし。

## JD2ローカル接続
- 管理者が設定画面の「NAS内3128番ポートを確認」を押した場合だけ、QnapDownloaderバックエンドから固定IPv4`127.0.0.1:3128`へのTCP接続を最大0.35秒試みる。HTTPリクエスト、JD2メソッド、Dockerソケットへのアクセス、プロセス起動・停止・設定変更は一切なし。
- TCP接続できた場合も**JD2の実体、ユーザー認証、外部ネットワークからの到達不能を証明しない**。`api_authenticated=false, network_isolation_verified=false, operations_enabled=false`を返し、MyJDモードを切替えない。
- QNAP Container StationのDocker版が古いと、ホスト127.0.0.1への-p公開が同一L2 LANから届く脆弱なケースがある。Docker公式の28.0.0以降の修正と、NASの実ポート公開設定・ルーティングを調査するまで、認証のない旧RemoteAPIを使ったJD2のダウンロード/アカウント変更を実装しない。
- 既存DockerのJD2への接続実証は、QNAP実機のネットワーク分離・アクセス権とJD2認証済みブリッジの選択がブロッカー。MyJDownloader不要の内蔵JD2方式（v0.1.11でローカルエンジンとして実装、実機未検証）は`docs/embedded-jd2-architecture.md`の後続PRで実装する。

## QNAP SSH読取専用確認
```sh
docker version --format 'server={{.Server.Version}}'
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}' | grep -i -E 'jdownloader|jd2'
netstat -tnl | grep -E '[.:]3128[[:space:]]' || true
ROOT=$(/sbin/getcfg QnapDownloader Install_Path -d '' -f /etc/config/qpkg.conf)
sh "$ROOT/downloader-diagnose"
```
この出力に資格情報が含まれないか確認して共有すること。DockerソケットやJDownloaderの構成を書き換えない。

## 受入れ/非対象
- 起動・Web・QPKG manifest・version一致、ログディスク書込なし、ログはメモリ内64件上限、認証済みI/O/ポート診断、JD2操作は未許可であることをunit/HTTPテストで確認。
- QNAP物理HDDのアイドル書込全体はQTS/jbd2、既存Docker、別プロセスに依存。Hub起動ログをディスクレスにすることと、JD2のすべての書込がなくなることは別。SQLiteはWAL+synchronous=FULLを維持し、認証設定・キュー変更の耐障害性を落とさない。
- Java/JD2のRAM展開、JD2アカウント/キューのRAM移動、Dockerの再構成、認証のないJD2 APIの有効化、実ダウンロード操作は今回行わない。RAMの安易なtmpfs化はswap/OOM/データ消失を招くため、実RAM/I/O測定後の別PR。

参考: Docker公式 https://docs.docker.com/engine/network/port-publishing/ / JD2公式 https://support.jdownloader.org/en/knowledgebase/article/jd-wakes-up-my-hdds