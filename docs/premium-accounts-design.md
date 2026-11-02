# QnapDownloader — プレミアムアカウント管理 設計仕様

> **注記（v0.1.11）:** MyJDownloader連携は削除され、JD2との通信はHub管理のローカルエンジンのみです（docs/engine-setup.md）。本書中のMyJD記述は設計当時の経緯（履歴）です。

設計日: 2026-11-01 / 開発前提: JD2がダウンロード処理とホスト別アカウントの唯一の正本

## 実装状況（v0.1.23）

Phase A（読取）とPhase B（ユーザー名/パスワード型アカウントの変更）を実装した。**実機（TS-253BeのJD2・Rapidgator）では未検証**で、JD2の`accountsV2`戻り値のフィールド名は公開APIの記述（`UUID`/`hostname`/`username`/`validUntil`/`trafficLeft`/`trafficMax`/`enabled`/`valid`/`errorType`/`errorString`）に基づく。Cookie/APIキー型・MFA・ホスト利用優先度（Phase C）は未実装。

- コード: `jd2_accounts.py`（正規化・マスク・検証・キャッシュ・レート制限・readback）、`jd2_local.py`（許可リスト8メソッドと秘密保護呼び出し）、`webapp.py`（`/api/accounts*`）、`web/`（設定 › アカウント、ホームの任意項目）。
- Hub API（管理者ログイン必須。POSTはCSRF+Origin、本文4 KiB以内、`no-store`）: `GET /api/accounts`（30秒RAMキャッシュ、`?refresh=1`で回避）、`GET /api/accounts/hosts`（10分RAMキャッシュ、rapidgator.netが先頭）、`POST /api/accounts/add|update|enable|refresh|delete`。エンジン停止中は503。§4の予定パス`/api/jd2/accounts*`とは別名に変更し、`check`は`refresh`にした。
- 公開モデルは§4のうち`revision`と`last_checked_at`を省き、`traffic_max_bytes`と`traffic_unlimited`（trafficLeftが-1）を加えた。未知フィールドは構築時に捨てる。`display_username`は先頭2文字（短い名前は半分まで）+`••••`+メールならドメイン。`account_type`は型フィールドか「将来の有効期限」からのみ導出し、判断できなければ`unknown`。`error_summary`は120文字以内で、ユーザー名・メール・URL・長いトークン様文字列を伏せる。一覧は200件まで。
- 操作対象のIDは直前にJD2から取得した一覧と突合してから使う（`revision`の代わり）。enable/delete/updateは操作後に一覧を読み直して結果を確認し、失敗時の自動再試行はしない。追加は直前後の一覧差分で新IDを特定して`refreshAccounts`し、特定できないときはその旨を返す。削除は`confirm_host`が対象のホストと一致しなければ拒否（UIはホスト名の入力を求める）。
- 追加・更新は10分に10回まで（全体、RAM）。本文のusername 1〜200文字、password 1〜500文字、制御文字不可。パスワードはレスポンス・キャッシュ・ログ・例外に含めず、処理後に参照を捨てる（Pythonの文字列は上書きできないためベストエフォート）。

### 送信経路のルール（サーバー側強制）

資格情報を含む`add`/`update`は、`request_is_https(peer, headers)`（ループバックのTLS終端＝Tailscale Serve等、`X-Forwarded-Proto`は非ループバックでは信用しない）**または**`tailnet_peer(peer)`（WireGuardで暗号化されたTailscaleピア）のときだけ受け付け、それ以外（LANのHTTP等）は403「パスワードはHTTPSまたはTailscale経由でのみ送信できます」。一覧・ホスト取得・再確認・有効/無効・削除は認証済みなら経路を問わない。UIは`/api/session`の`https`/`tailnet`（同じ判定）でフォームを無効にし、「HTTPS接続」へ案内する。

### 残余リスク: JD2へのGETエンコーディング

JD2のRemoteAPIはNAS上で検証済みの形式がGETのみ（引数をURLエンコードしたJSONとしてリクエストラインに載せる）で、`addAccount`/`setUserNameAndPassword`のパスワードもリクエストラインに入る。対策と限界:

- 宛先はloopback（ネイティブ）またはHub専用コンテナの非公開ブリッジIP（コンテナ）のみ。LANへは出ない。
- Hubは`accountsV2`のURL・引数をログ・RAMログ・例外・レスポンスに入れない（失敗時の文言はメソッド名とHTTPステータスだけ。例外の連鎖も切る）。
- 限界: ①JD2自身やJavaのログが受信したリクエストを記録する可能性は制御できない（エンジンのRAMログには既存の`scrub`が通るが、パターンに合わない形式は漏れ得る。実機でJD2ログに残らないか確認が必要）。②同一ホスト上でloopback/ブリッジを観測できる特権プロセスには見える（rootが前提のため境界外）。③JD2が資格情報を保存する形式はJD2側の仕様（Hubは複製しない）。


## 1. UIゴール

QnapDownloaderの単一Web管理画面に「**アカウント**」タブを追加する。Rapidgatorをはじめ、JD2プラグインがサポートするホストごとに複数アカウントを管理できる。MyJDownloaderの別画面へ飛ばさず、iPhone Safari・QNAP App Centerの画面を共通入口とする。

**アカウント一覧カード**（秘密情報を含まない）の表示例:

```text
Rapidgator
  Account: user••••@example.com   [Premium] [有効]
  JD2検証: OK       残り容量: 120 GiB （JD2から取得できた場合のみ）
  有効期限: 2026-12-31 （JD2から取得できた場合のみ）
  最終確認: 2026-11-01 12:00
  [再確認] [無効化] [認証情報を更新] [削除...]
```

未取得・未対応の場合は`不明`と表示し、期限・残トラフィックを推測しない。多ホストサービスは「契約サービス」と「利用可能なホスト」を分ける。期限切れ・認証エラー・容量切れ・アカウント無効・更新中を混同しない。

## 2. 操作と初期権限

| 機能 | 読取か変更か | 初期方針 |
|---|---|---|
| アカウント一覧/ホスト/種別/状態/期限/残トラフィック | 読取 | 管理者ログイン後に利用 |
| 取得失敗/期限切れの診断 | 読取 | JD2によって得られた情報のみ、秘密文字列を除く |
| サポートホスト一覧・ホスト別認証方式 | 読取 | JD2から取得、未対応は非表示ではなく説明 |
| 新規追加（メール/ID、パスワード、APIキー） | 変更 | HTTPS + CSRF + 再認証/再確認、ユーザー明示操作 |
| Cookieによるアカウント追加 | 変更 | HTTPS限定。サービス별に明示サポート確認後、必要最低限のCookieのみ扱う |
| アカウント更新/再認証・有効/無効 | 変更 | API結果を再取得して確定。失敗時に無断削除・自動再追加しない |
| アカウント削除 | 破壊的 | 二段階確認、該当account ID/ホスト名一致、JD2への削除完了readback必須 |
| ホスト利用優先度/マルチホスト利用規則 | 設定変更 | 初期はJD2既定を維持。別途実JD2のrules schema検証後に実装 |
| 料金支払/契約自動購入 | 外部サイト機能 | **非対象**。QnapDownloaderは支払情報を保存しない |

## 3. JD2 API対応（公式MyJDownloader公開APIに基づく）

`/accountsV2/listAccounts(query)`、`/accountsV2/listPremiumHoster`、`/accountsV2/refreshAccounts(ids)`、`/accountsV2/addAccount(host,username,password)`、`/accountsV2/enableAccounts(ids)`、`/accountsV2/disableAccounts(ids)`、`/accountsV2/setUserNameAndPassword(id,user,password)`、`/accountsV2/removeAccounts(ids)` が公開されている。多くの同等機能が旧`/accounts`にもあるが、原則V2を使う。

これらのHTTP APIはMyJDownloaderプロトコルの一部であり、**QnapDownloaderのブラウザから直接呼び出さない**。内蔵JD2の専用認証ブリッジが公式JD2内部の対応操作へ変換できることを検証したのち、Hub backendのみから呼ぶ。アカウントDBファイルへの直接JSON書込や復号/暗号化実装を推測して手を加えない。

AccountQueryや戻り値Account構造はJD2の版・プラグイン更新で変わり得るため、実接続試験でスキーマの違いを確認してホワイトリストのUIモデルへ投影する。**JD2から返るAccount全フィールドをそのままJSONでブラウザへ返さない**（暗号化/生パスワードやCookie相当の情報が混じるリスク）。

## 4. Backend公開モデルと正規化

```json
{
  "id": "opaque-JD2-account-id",
  "host": "rapidgator.net",
  "display_username": "user••••@example.com",
  "account_type": "premium",
  "enabled": true,
  "status": "valid",
  "valid_until": null,
  "traffic_left_bytes": null,
  "last_checked_at": null,
  "error_summary": null,
  "revision": "opaque-revision"
}
```

各フィールドは型チェックする。`status`は`valid/invalid/expired/temporarily_unavailable/disabled/unknown`のみ。取得できない値はnull。生IDやホスト名をそのまま任意のJD2コマンドへ流さず、直前に取得した対象account ID+ホスト+revisionに対して突合してから操作する。画面ログ・バックエンドJSONは秘密フィールドを常に削除してから出力する。

APIの予定:
- GET `/api/jd2/accounts`: ページング・返却上限・TTL付きのマスク済み一覧
- GET `/api/jd2/accounts/hosts`: 対応サービス一覧
- POST `/api/jd2/accounts/check`: account IDを検証して更新/再照会（JD2の外部通信が発生し得るため明示）
- POST `/api/jd2/accounts/add`: host、username、secret、login_methodの固定schema
- POST `/api/jd2/accounts/update`: id、revision、必要な変更のみ
- POST `/api/jd2/accounts/enable`: id、revision、bool
- POST `/api/jd2/accounts/delete`: id、revision、二段階確認で認証済み管理者のみ

URLパスに秘密文字列を含めない。POST bodyは最大サイズを厳格に制限し、失敗時に機密payloadをエラーメッセージ/アクセスログに残さない。機能が未接続・未検証ならボタン無効・HTTP 503等で明示し、成功を偽装しない。

## 5. 資格情報の扱い

- **HTTPS必須**: Hubログイン後でもLANの`http://192.168.x.x:17864`でパスワード/クッキー/APIキーを入力させない。QTSのHTTPSリバースプロキシやTailscale等の暗号化接続をセットアップしてから書込操作を解放。Webの`location.protocol`だけでは信頼できない。サーバー側で接続のTLS終端と信頼済みproxy設定を検証し、偽装した`X-Forwarded-Proto`は信頼しない。
- 資格情報はJD2のaccount storeを唯一の正本にする。Hub SQLiteに生パスワード/セッションクッキーを別途保存しない。JD2側のファイル暗号化品質を推測せず、QNAPの権限0700/0600、ユーザー分離、NASボリューム暗号化の有無に応じて脅威を評価。
- パスワード入力は`autocomplete`/キャッシュ制御を考慮。入力後メモリから参照を消し、GETレスポンス、WebSocket、診断エクスポート、クラッシュログ、監査ログに秘密を含めない。レスポンスに`Cache-Control: no-store`、秘密入力URLのブラウザ履歴保持禁止。
- Cookie型はサイトごとの正規のログイン方法だけを扱う。ユーザーのブラウザからCookie自動窃取、Challenge回避、外部サイトへの偽装/認証回避はしない。手動エクスポートしたCookieのインポートも、HTTPS・最小権限・範囲限定・有効期限と失効/削除が確認できてから導入。
- 管理者ログイン認証と、プレミアムアカウント資格情報は別の責務。最初のQnapDownloader管理者登録にSSHトークンを再導入しない。
- リモートJD2互換モードでは、（MyJD連携はv0.1.11で削除済み。）外部JD2との接続経路とpremium account UIの経路を区別し、Hubのアカウントパスワードと混同しない。

## 6. ログイン方式差・運用

サービスによってパスワード型、APIキー型、Cookie型、Basic Auth、マルチホスト型がある。JD2のホスト別プラグインが判定する方式を尊重し、QnapDownloaderは全サービスを`username/password`に強制統一しない。

- Rapidgatorはサイトの公開仕様・JD2プラグイン現行版で実機確認する。ログイン・トラフィック・期限・captcha/2FAは、JD2が返すエラーや対話要求に従って表示。
- ホスト別利用優先度はJD2既定を維持。JD2公式によれば既定では通常のPremiumがFreeより優先される。個別/マルチホストの利用規則は後続PRでのschema確認を要する。
- アカウントステータス照会はサイト側へ通信する場合があるので、高頻度ポーリングしない。手動refreshまたは長いTTL、エラー時指数的backoffで外部側の負荷・停止措置を避ける。

## 7. 実装PR / テスト

**Phase A (読取)**: サポートホスト、JD2アカウント一覧のマスク表示、account_type/status/expire/trafficの正規化と型・データ上限。FakeJD2で秘密フィールドを含むレスポンスが絶対に漏れないことをテスト。実JD2のフィールド差異はログに秘密を書かずにschema-onlyで検証。

**Phase B (変更)**: HTTPSプロキシ構成の受入れとserver-side強制。追加・更新・有効/無効・refresh・削除を個別の明示操作として実装。POSTのCSRF・Origin・Host・rate limit・body size・secret masking・revision precondition・失敗時readbackを検証。多重POSTによる重複登録は避け、通信不明時に勝手に再試行しない。

**Phase C (詳細)**: Cookie/API Key/MFA/認証方法ごとのUI、マルチホストと利用優先度、期限切れ通知。各ホストのJD2プラグインと利用規則を実機検証してから適用。削除後のダウンロード済みデータ/他アカウントを変更しない。

受入れ:
1. 1ホストに複数アカウント登録/認証/無効化/再有効化/削除して正しい対象だけが変わる
2. 間違ったパスワード/期限切れ/API拒否/通信途絶/更新によるschema変更でも秘密情報を開示しない
3. HTTPから追加・更新・Cookie操作できない。HTTPSかつ管理者認証とCSRFが必須
4. アカウント削除や無効化がキュー・ダウンロードファイルを勝手に消さない
5. JD2単体更新/Hub QPKG更新/Java更新の後もアカウント永続化、ID drift検知、状態再取得ができる
6. 既存Dockerのアカウントを黙って読み込まない・移動しない。移行は任意・バックアップ・確認・破壊的変更なし

## 参考

- https://my.jdownloader.org/developers/ （accountsV2とdownloadcontroller API）
- https://support.jdownloader.org/en/knowledgebase/article/where-should-i-buy-and-enter-my-premium-account
- https://support.jdownloader.org/en/knowledgebase/article/account-manager-account-useage-rule-disable-free-download
- https://support.jdownloader.org/en/knowledgebase/article/headless-settings
