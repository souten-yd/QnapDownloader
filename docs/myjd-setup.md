# 認証済みMyJDownloader接続設定と単独アップデート

QnapDownloaderはJDownloader 2 (JD2)の公式MyJDownloader APIを利用します。無認証の旧RemoteAPIは使用しません。

## QNAPでの設定

JD2側でMyJDownloaderにログインし、対象デバイス名を確認。NAS上の永続データディレクトリを確認します。

```sh
ROOT=$(/sbin/getcfg QnapDownloader Install_Path -f /etc/config/qpkg.conf)
DATA="$(dirname "$ROOT")/QnapDownloader-data"
mkdir -p "$DATA/config"
chmod 700 "$DATA/config"
```

このディレクトリに config/myjd.json を作成します。例:

```json
{"email":"your-account@example.com","password":"YOUR_PASSWORD","device_name":"NAS JD2","allow_jd2_update":false}
```

```sh
chmod 600 "$DATA/config/myjd.json"
```

設定後Hubを再起動します。認証情報はGitHubやログへ記録しないでください。ファイルは0600の通常ファイルを必須とします。

## 単独更新と設定復旧の現状

allow_jd2_updateは初期値false。trueの場合のみJD2公式Updaterの更新要求を許可し、HubのQPKGは変更しません。Docker版JD2が読み取り専用の場合はJD2更新自体が失敗することがあり、Hubはコンテナを無断で作り直しません。

実JD2のconfig/listとconfig/getから検証済みのLinkGrabber自動確定/自動開始キーのみ読取可能です。Packagizerルール・一般保存先・解凍の全設定キーは実機確認が終わるまでは適用不可とし、プレビューのmissingに出してfail-closedで停止します。カスタムルール、既存リンク、ダウンロード済みファイルを変更しません。

実機で全必須キーの実値・型・読戻し・rollbackを検証することが、推奨設定一括適用ボタンを有効にする条件です。

API仕様: https://my.jdownloader.org/developers/
