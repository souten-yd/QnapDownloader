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

## 保留リンクの手動LinkGrabber転送（次の段階）

Hubのキューで「JD2に手動で送る」を押した場合に限りJD2へ送信します。自動クロール・自動開始・任意スクリプト実行はしません。ネットワークエラー後の送信成否が不明なリンクはuncertainで永続保持し、自動再送しません。

実機上の既存Docker JD2ではNAS上のパスとJD2コンテナ内のパスが異なるため、管理者が `config/myjd.json` に次の項目を明示してください。

```json
{"nas_root":"/share/Download","jd2_root":"/downloads","allow_link_submit":true,"allow_unverified_packagizer":true}
```

※上記は既存myjd.jsonに追加するフィールドのみの例です。実際のパスはQNAP側とDockerマウント設定に合わせてください。

`allow_unverified_packagizer`は**既存のPackagizerルールを確認した管理者のみ**trueにしてください。自動でPackagizerのルールを削除したり、サブフォルダ生成を強制OFFにすることはありません。JD2の実際の一般設定とPackagizerルールが未検証なら、フラット保存は保証できません。

手動転送でもLinkGrabberの自動確定・自動開始が実際にOFFであることを送信直前に確認し、HTTPS URL・保存先マッピング・権限・明示的許可のいずれかが不明なら送信を停止します。送信直前の障害はuncertainとなるため、復旧前にJD2 LinkGrabberを確認してください。
