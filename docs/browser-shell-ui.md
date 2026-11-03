# ブラウザ型Web管理画面（v0.1.12 / ロードマップM0）

> v0.1.11までの記述のうち、ホーム・ダウンロード・リンク・設定の画面構成と任意の自動更新スイッチは末尾の「v0.1.12 UX整理」で置き換えられました。セキュリティ不変条件は変更ありません。

## 目的
QnapDownloaderの画面を「普段使っているWebブラウザ」と同じ感覚で使えるようにする。JD2の管理（ダウンロード・リンク収集・設定）はブラウザのタブのように切り替え、外部サイトの閲覧も同じタブ列で行う。ただし**管理用タブと閲覧用タブは役割も寿命も違う**ので、見た目と挙動で明確に分ける。

参考にした一般的なブラウザの慣習:
- Chrome / Firefox の「固定タブ（ピン留め）」: 左端・アイコン中心・閉じるボタンなし。常に使う画面に最適。
- Chrome の新しいタブページ: アドレスバー＋よく使うサイト。
- Chrome の `chrome://settings`: 設定はサイドバー付きの1ページにまとめ、URLで各節へ直接移動できる。
- Chrome のダウンロードボタン: ツールバー右側に件数バッジ。押すとダウンロード画面へ。
- iOS Safari: 下部ツールバー、タブ数ボタンからタブ一覧（グリッド）。

## 画面構成（PC幅 ≥ 760px）
```
┌ タブ列 ─────────────────────────────────────────────────┐
│ [⬇ ダウンロード][🔗 リンク][⚙ 設定] │ [example.com ×][rapidgator.net ×][+]   │
├ ツールバー ───────────────────────────────────────────────┤
│ [←][→][⟳]  [🔒 https://… / qnap://downloads        ][☆]  [⬇ 3][⋮]        │
├ ブックマークバー（Webタブ・新しいタブで表示）──────────────────┤
│ ★サイトA  ★サイトB …                                                     │
├ コンテンツ ───────────────────────────────────────────────┤
```

### 固定タブ（管理）
| タブ | 内部URL（表示用） | ハッシュ | 内容 |
|---|---|---|---|
| ダウンロード | `qnap://downloads` | `#/downloads` | 既存の読み取り専用一覧＋要約＋絞り込み（すべて/実行中/待機/完了）＋ファイル名検索（クライアント側のみ）＋任意自動更新 |
| リンク | `qnap://links` | `#/links` | URL追加、Rapidgator貼り付け抽出、保留キュー、JD2へ手動送信 |
| 設定 | `qnap://settings/<節>` | `#/settings/<節>` | サイドバー: 保存先 / JD2接続 / ブックマーク / 更新 / 診断 / このアプリについて |

- 閉じられない・増えない。左端にアイコン＋短いラベル（狭い幅ではアイコンのみ）。
- ハッシュルーティングにより、ブラウザ自体の戻る/進む・再読み込み・ブックマークで管理画面へ直接戻れる。
- 旧「概要」は「新しいタブ」ページへ統合（Hub/JD2の状態カード＋よく使う操作＋ブックマーク）。`#/` または `#/home`。

### 通常タブ（Web閲覧）
- `+` または ⋮ メニュー「新しいタブ」で作成。最大6枚（iPhoneのメモリと隔離iframeの負荷を考慮）。上限時は理由を表示し作成しない。
- タブ名はホスト名（別オリジンのページタイトルは読めないため）。新しいタブは「新しいタブ」。
- 各タブは独自の履歴（Hubで検証済みURLのみ）と隔離モード（強い制限/限定操作/互換）を持つ。iframe内部の遷移は読めない旨を従来どおり表示。
- タブごとにiframeを保持し、非表示タブは `hidden` で隠す（状態維持）。ページ再読み込み後の復元時は**選択されたタブだけ**読み込み、他は選択時に遅延読み込み。
- タブ一覧は `sessionStorage`（try/catch、失敗時は復元なし）にURL・モード・履歴だけ保存。復元したURLも読み込み前に `/api/browser/validate` で再検証。Cookie等は保存しない。
- 閉じたタブは最大10件までメモリに保持し、⋮ メニュー「閉じたタブを開き直す」で復元。
- ⋮ メニュー（Webタブ時）: 隔離モード切替（限定操作・互換へは確認ダイアログ）、Safari/外部ブラウザで開く、このURLをリンク保留へ追加（確認あり）、ブックマークに追加、タブを閉じる。

### オムニボックス（アドレスバー）
- 管理タブでは `qnap://…` を表示。Webタブでは現在の検証済みURL（🔒）。
- 入力の解釈:
  1. `https://` で始まる、または `example.com/…` 形式 → `https://` を補ってHubで検証 → 現在のWebタブ（管理タブ表示中なら新しいWebタブ）で開く。
  2. `qnap://downloads` 等 → 該当管理画面へ移動。
  3. Rapidgatorリンクを含む複数行テキストの貼り付け → 「リンク」画面のRapidgator抽出欄へ転記して移動（自動保存しない）。
  4. それ以外 → 「URLを入力してください（検索エンジンは未対応）」を表示。外部検索サイトへ勝手に送らない。
- ショートカット: `/` または `Ctrl/⌘+K` でフォーカス、`Esc` で元に戻す。`Ctrl+L`・`Ctrl+T` はブラウザ本体が使うため奪わない。

### ダウンロードバッジ
ツールバー右の ⬇ に、最後に取得した一覧の「実行中」件数を表示（取得していなければバッジなし）。押すとダウンロード画面へ。バッジのために自動取得はしない。

### 任意の自動更新（ダウンロード画面）【v0.1.12で廃止。下記参照】
- 「表示中は自動更新（15秒）」スイッチ。既定OFF。選択は `localStorage` に記憶（try/catch）。
- ダウンロード画面を表示中かつ `document.visibilityState === "visible"` の間だけ動作。他タブへ移動・画面非表示で停止。取得中は多重実行しない。エラー時は自動更新を止めて理由を表示。
- サーバー側の定期ポーリングは追加しない（ブラウザ側の明示的な選択によるもののみ）。

## スマートフォン幅（< 760px）— Safari型
- 上部: コンパクトなオムニボックス（Webタブでは戻る/進む/再読み込みを左に）。
- 下部固定ツールバー（safe-area対応、各44px以上）: ⬇ダウンロード / 🔗リンク / 🌐ブラウズ（Webタブ数バッジ） / ⚙設定。
- 🌐: Webタブがなければ新しいタブを作成。Webタブ表示中にもう一度押すと**タブ一覧**（カードのグリッド、×で閉じる、＋新しいタブ）を開く。
- PCのタブ列は非表示。ブックマークバーは新しいタブページ内のみ。

## 通知・ダイアログ
- 成功通知はトースト（6秒で自動消去）、エラーは閉じるまで表示。`role="status"`/`aria-live`。
- 破壊的・外部送信を伴う操作は従来どおり `confirm()` による明示確認（文言は既存を踏襲）。

## テーマ・アクセシビリティ
- CSS変数でトークン化し、`prefers-color-scheme` でライト/ダーク両対応。
- タブ列は `role="tablist"`/`role="tab"`/`aria-selected`、コンテンツは `role="tabpanel"`。フォーカスリング可視、`prefers-reduced-motion` 尊重。

## セキュリティ不変条件（変更禁止・テストで検証）
- iframe の sandbox 値は次の3つの定数のみ（`app.js` の `sandboxFor(mode)` 経由で1箇所だけ設定）: `""`（強い制限）、`"allow-scripts allow-forms"`（限定操作）、`"allow-scripts allow-forms allow-same-origin"`（互換、v0.1.10〜）。`allow-popups`・`allow-top-navigation`・`allow-downloads`・`allow-modals` はいかなるモードでも付与しない。
- `allow-same-origin` は互換モードの定数1箇所にだけ現れてよい（テストで出現回数1を検証）。以前の「scriptsとsame-originの併用禁止」という一律禁止は、理由を明示した次の条件付き許可に置き換えた。理由: 隔離iframeは同一オリジン権限を持たないopaque originになりCookie/ストレージが使えず、Cloudflare等の正規の確認が完了できない。外部ページは常に公開DNS名のhttps（`browser_safety.validate`）、Hubは `http://<LAN/VPN IP>:17864` またはリバースプロキシのホストなので、外部ページのオリジンがHubと一致することはなく、`allow-same-origin` が与えるのはそのページ**自身**のオリジンのCookie/ストレージだけでHubのセッションやストレージには届かない。この前提を壊さないため、バックエンドは `/api/browser/validate` でHub自身のホスト名（Hostヘッダー、大文字小文字無視・ポート除去）と `*.ts.net` を全モードで拒否し、フロントも互換iframe生成直前に同じ検査をする。
- iframe の `src` には `/api/browser/validate` が返したURLだけを設定。`referrerpolicy="no-referrer"`。
- DOM生成は `textContent`/`createElement` のみ（`innerHTML`・`insertAdjacentHTML`・`document.write`・`eval`・`new Function` 不使用）。
- インライン `<script>`/`style=` 属性/イベントハンドラ属性を HTML に書かない（CSPが `script-src 'self'; style-src 'self'` のため）。SVGアイコンは `<symbol>`＋`<use>`。
- `sessionStorage`/`localStorage` に資格情報・CSRFトークン・Cookieを保存しない（v0.1.10で追加した保存物も、最後のルートのハッシュと「ホスト名→モード」の組のみ）。
- CSP・Cookie設定は変更しない。バックエンドの変更は v0.1.10 の `/api/browser/validate` のHub自身/`*.ts.net`拒否のみ。

## 非対象
外部ページのタイトル/ファビコン取得（サーバーが外部へアクセスすることになるため不可）、iframe内リンクの自動検出、検索エンジン連携、タブの並べ替え・グループ、ダウンロード操作ボタン（M2）。

## v0.1.10 変更

### 1. Tailscale経由ではHTTP警告を出さない
`isTailscaleHost(hostname)`（純関数）が、100.64.0.0/10のIPv4・`*.ts.net`・`fd7a:115c:a1e0::/48`のIPv6（角括弧付き可）をTailscaleと判定し、その場合は「HTTP接続です…」警告（`#lanSecurityWarning`）を隠す。TailscaleのWireGuardトンネルが通信を暗号化するため。それ以外の非ループバックHTTPでは従来どおり表示する。

### 2. 互換モード（Cloudflareの確認が白画面のままになる問題）
- タブごとの第3モード「互換」。sandbox は `allow-scripts allow-forms allow-same-origin` ちょうど。ポップアップ・ダウンロード・モーダル・親画面遷移は付与しない。安全性の根拠は上記「セキュリティ不変条件」を参照。
- 切り替え時に `confirm()`: サイト自身のCookie/ストレージを枠内で使える／Cloudflareを回避する機能ではない／iPhone Safariのサードパーティ Cookie ブロックで動かない場合は「Safariで開く」を使う。
- 互換でないWebタブには、ツールバー下に細いヒントバー「Cloudflareの確認が進まない場合 → [互換モードで再読み込み] [Safariで開く]」を表示（ブロックしない・タブごとに×で閉じられる）。
- 選んだモードはホスト名ごとに `localStorage`（try/catch）へ記憶し、同じホストを開くとき適用する。保存するのはホスト名とモードの組のみ、最大100件、強い制限（既定）は保存しない。互換は、記憶のない別ホストへ移ると強い制限に戻る。新しいホストの既定は従来どおり強い制限。
- バックエンド: `/api/browser/validate` は `browser_safety.validate` の後に、URLのホスト名がリクエストのHostヘッダーのホスト名と等しい（大文字小文字無視・ポート除去）、または `*.ts.net` の場合、日本語メッセージで400を返す。

### 3. なめらかさ・iOSの全画面再読み込み対策
「タブ切り替え時にアイコンだけの読み込み画面が出る」原因は、アプリ側のスプラッシュではなく、メモリ不足でiOSがWebContentプロセスを終了しホーム画面アプリを丸ごと再読み込みすることと推定される。対策:
- 生きた外部iframeは狭い画面（<760px）で最大1、PCで最大3（LRU）。外れたタブは検証済み履歴を保持し、選択時に再検証して再読み込み（薄い読み込みバーのみ、空白の全画面状態は出さない）。タブ数の上限6は不変。
- 表示のstale-while-revalidate: ホーム状態・保留キュー・設定各節（保存先/フォルダ選択、JD2設定、更新状態）・ブックマークは直近データを即時表示し、10秒より古い場合だけ背景で再取得。取得前に一覧を消さず、JSONが変わった時だけDOMを更新。フォルダ選択は設定を開くたびに作り直さない。ダウンロード一覧は従来どおり手動/任意の自動更新のみ（サーバー側ポーリングは追加しない）。
- 高さの予約（`min-height`の骨格、スピナーのオーバーレイなし）、長い一覧に `content-visibility:auto`、画面切替の120msフェード（`prefers-reduced-motion` では無し）。ループ内でレイアウトを読まない。
- ルーティングは `hashchange` と `history.replaceState` のみで、ページ全体の再読み込みはしない。
- 復元の高速化: ホーム画面アプリが再読み込みされたら、`sessionStorage` からタブ列と最後のルートを `/api/session` の応答前に描画し、その後セッションを確認する（外部ページはこの時点では読み込まない）。
- `<meta name="mobile-web-app-capable">` を追加。

### 4. Hub更新後の自動再読み込み
`POST /api/update/apply` 成功後、消せない進捗トースト「更新中… 完了後に自動で再読み込みします」を出し、2秒ごとに `GET /api/health`（`no-store`、再起動中のネットワークエラーは無視）を最大5分間確認する。適用前と違うversionが返ったら `location.reload()`。5分経過したらエラートーストと手動の「再読み込み」ボタン。セッションはメモリ上のため再読み込み後はログイン画面が出る（トースト文にも記載）。
また、どの画面でも `/api/status` のversionがページ起動時と違えば（他端末で更新した場合など）「再読み込み」ボタン付きのトーストを出す（自動再読み込みはしない）。

## v0.1.12 UX整理
目的: 「使いやすく、不要なボタン・重複した操作や表示をなくす」。セキュリティ不変条件・確認フラグ・CSRFは変更しない。

### ホーム（新しいタブ）
- 削除: 「よく使う操作」ボタン行（固定タブと重複）、「Web管理画面 稼働中」カード、「初期開発版…」の注意書きと長い説明（要点は 設定 › このアプリについて へ）。
- 「はじめに」カードを1枚だけ表示: ①保存先を選ぶ（済なら✓、未なら「選ぶ」→`#/settings/storage`）②JD2エンジンを準備（「初期設定を開始」。①が済むまで非表示、実行中は「3/9 Javaの展開…」の進行を表示）。保存先が選択済みでエンジンが導入済み（`installed`）になるとカードごと消える。
- 準備後は1行の状態表示（エンジン稼働中 · ダウンロード中 N件 → `#/downloads`。停止中は「起動」ボタン）とブックマーク（空のときは1行の案内のみ）。
- オムニボックスの案内文は「URLを入力または貼り付け」。

### ダウンロード
- 説明文・「エンジン設定」ボタン・自動更新スイッチを削除。エンジンが動いていないときの表示は「JD2エンジンが起動していません」＋1ボタン（未導入・設定中=「初期設定へ」、停止中=「起動」→`POST /api/engine/start`）のみ。
- 稼働中は5秒ごとに自動更新（ローカルのデータのため）。ダウンロード画面を表示中かつページが可視のときだけ。多重リクエストなし、3回連続失敗で停止（手動更新で再開）。控えめな「更新: hh:mm:ss」とアイコンだけの更新ボタン。旧`localStorage`キー`qnapdl.downloads.auto`は起動時に黙って削除する。
- パッケージとファイルを1つの一覧に統合。行=パッケージ（名前・進捗・サイズ・速度・残り時間・状態）で、クリックで同じスナップショット内のファイルを展開する。紐付けは`/api/jd2/downloads`のパッケージ`id`とファイル`package_id`（JD2の`uuid`/`packageUUID`。64bit値のため文字列で返す）。同じページに親パッケージがないファイルは折りたたみの「他のファイル」に出す（パッケージが1件もないページではそのまま一覧に出す）。
- 一覧が空のときは絞り込みチップと検索を隠す。ページ送りは`has_more`または2ページ目以降のときだけ表示。
- ツールバーのバッジ: ダウンロード画面を開いていないときも、エンジン稼働中かつページ可視の間は30秒ごとに`/api/jd2/downloads?page=0`で件数を更新（取得中なら見送り）。

### リンク
- 入力欄は1つ「リンクを貼り付け（URL・テキスト・HTML）」。400msのデバウンス後、単一のhttps URLなら「このURLを追加」、それ以外は既存の抽出プレビュー（`commit:false`）で「N件見つかりました」と一覧、「N件を追加」（既存のdigest確認つきcommit）。HubのリンクリストへのOS確認ダイアログは廃止（ローカルで削除できるため）。JD2への送信は引き続き`confirm()`あり。
- 「保留キュー」を「追加したリンク」に改名。各行: URL・状態チップ（未送信/送信中/要確認/送信済み）・「JD2へ送る」（エンジンの`can_send`のときだけ）・「削除」（`staged`のときだけ）。ヘッダーに「すべてJD2へ送る」（`can_send`かつ未送信2件以上。確認は1回、順次送信、最初のエラーで停止）。
- 新API（管理者+CSRF+Origin）: `POST /api/links/delete {id}`（`staged`の行だけ削除、他は400。改訂番号+1）、`POST /api/bookmarks/delete {id}`。

### 設定
- サイドバー順: エンジン / 保存先 / ブックマーク / 更新 / 診断 / このアプリについて。`#/settings`（節なし）の既定はエンジンが稼働していなければエンジン、稼働中なら保存先。
- エンジン: 状態の見出し1つと状態別の主操作1つ（未導入=初期設定を開始、設定中=進行表示、停止中=起動、稼働中=「稼働中」＋目立たない「停止」）。接続確認はセクションを開いたとき自動で行い結果を行内表示（ボタンは廃止）。自動起動スイッチは導入後のみ表示。9段階の一覧は設定中/失敗時だけ開き、成功後は折りたたみ「前回の初期設定の記録」。コンポーネント名・版・SHAは「詳細」、RAMログは「ログ」（開いたときだけ取得）。使えないボタンは無効表示でなく非表示。
- 「JD2の更新を確認」「JD2を更新」（更新があるときだけ）「推奨設定を再適用」は更新節からエンジン節の1行に移動（エンジン稼働中のみ）。
- 更新: Hubのみ。「現在 0.1.12 · 最新を確認」とチャンネル選択を同じ行に置き、結果は「最新です」または「0.1.13-rc1 があります [更新]」。状態は日本語表示（idle/checking等の生の値は出さない）。更新ログは折りたたみ。
- 保存先: 現在のフォルダを大きく表示し「変更」でピッカーを開く（未選択のときは最初から開く）。方針4行は「フォルダ作成・自動開始・自動解凍・自動削除はすべてOFF」の1行に。
- このアプリについて: バージョン、ホームから移した短い注意点、「詳細」、ドキュメントへのリンク。

### クロム
- ☆（ブックマーク）ボタンはWebタブ（URLが読み込まれているとき）だけ表示し、`qnap://`ページでは隠す。戻る/進むは管理画面でもブラウザ履歴で動く。
- 狭い画面: ダウンロードの見出しは1行（タイトル＋更新時刻＋アイコンボタン）に収め、余白を詰める。

## v0.1.13 追加

設定に「HTTPS接続」（`#/settings/https`、保存先の次）と「iPhoneから送る」（`#/settings/share`）を追加。リンク画面は `#/links?paste=<URLエンコード>` で貼り付け欄に事前入力できる（自動保存なし）。HTTP接続のWebタブのCloudflareヒントに「HTTPSで開くと通過しやすくなります」リンクを表示。詳細は [https-and-share.md](https-and-share.md)。

## v0.1.14 常にSafariで開く / Webタブの簡素化（v0.1.30で「Safariで開く」系は廃止）

実機（iPhone、Tailscale ServeのHTTPS、互換モード）で、Cloudflare保護のサイトが枠内で完全に空白のまま（確認の文言すら出ない）になった。サイト側が埋め込みを拒否しているため、ホスト単位で「常にSafariで開く」を選べるようにした。セキュリティ不変条件（sandbox値、`innerHTML`不使用、検証済みURLのみ、ストレージに秘密なし）は変更なし。

### 外部モード（`external`）
- 既存のホスト別モード（`localStorage` `qnapdl.hostmodes.v1`、ホスト名→モード、上限100件、全アクセスtry/catch）に値 `"external"` を追加。タブのモード（`MODES`・sandbox）ではない。
- 対象ホストへの遷移（アドレスバー、ブックマークバー、スピードダイヤル、タブ内の戻る/進む/再読み込み）は、iframeを作らず `window.open(url, "_blank", "noopener,noreferrer")` でSafariへ渡す。空白の新しいタブから開いた場合はそのタブを閉じ、別のページを表示中のタブはそのまま残す。
- iOSでは `window.open` はユーザー操作の呼び出し内でないとブロックされるが、`/api/browser/validate` は非同期。空のウィンドウを先に開くとopener付きのポップアップになるため不可。そこで、外部モードのホストは「過去にサーバー検証を通したページで選ばれた」ことを前提に、同期的な簡易チェック（https、認証情報・ポートなし、ドットを含む公開らしいホスト名、保存済みホストと一致、Hub自身・`*.ts.net`・`.local`等は除外）だけで即座に `window.open` し、その直後に `/api/browser/validate` を呼ぶ。サーバー検証が最終判断で、拒否されたらエラートーストを出して外部モードを解除する。初めてのホスト（外部モードでない）は従来どおり非同期検証のフロー。
- 開いた後に「Safariで開きました（このサイトは常に外部で開きます）」トースト（8秒）と「タブで開く」（外部モードを解除してタブで開く）。
- 復元されたタブ（ユーザー操作なし）が外部ホストのときは自動でSafariを開かず、検証だけして枠なしで表示し、1行の「Safari」ボタンで開く。

### タブの1行表示
- 旧: 複数行の説明＋ヒントバー。新: 1行（高さ40px、最大44px、折り返しなし）`[モードチップ] ホスト名… [Safari] [⋯]`。チップは「強い制限／限定操作／互換／Safari固定」。「Safari」はその場で1回だけ外部で開く。⋯は既存メニュー。
- 細いヒントバー「表示されない場合: [Safariで開く（次回から自動）] [互換で再読み込み]」は、読み込み開始から6秒後、またはチップをタップしたときだけ表示（タブごとに×で閉じられる）。互換モードでは「互換で再読み込み」を隠す。互換への切替の `confirm()` は従来どおり。
- クロスオリジンの読み込み失敗は確実に検出できないため、6秒のタイマーは`load`イベントの有無にかかわらず出すヒューリスティック。
- 長い説明文（「隔離表示中…」「枠内の遷移先URLは…」「HTTPSで開くと…」）はタブUIから削除し、設定 › このアプリについて と HTTPS接続 に移動。

### 設定
- 設定 › ブックマーク の下に「常にSafariで開くサイト」一覧（ホスト名＋「解除」）。

## v0.1.16 NASブラウザ（リモートブラウザ）

Cloudflare等で手元のブラウザ（iframe）では開けないサイトを、NAS上のコンテナ内Chromiumで開く第3のモード。設計・安全性は [container-runtime.md](container-runtime.md) §4。端末には外部サイトの通信もスクリプトも届かず、HubのJPEGフレームと入力だけがやり取りされる。

- モード: `"remote"` はホスト別の記憶値（`external` と同じ `qnapdl.hostmodes.v1`）とタブのフラグ `tab.remote` で、`MODES`/sandbox値ではない（sandbox定数は3つのまま）。Cloudflareヒントのバーとメニューの「NASブラウザで開く」を選ぶと、そのホストが「NASブラウザで開くサイト」（設定 › NASブラウザ）に記憶される（解除可）。メニューの「NASブラウザをやめる（通常表示へ）」で戻せる。
- 表示（v0.1.22で変更）: `#remoteView` の `<img>` に、`GET /api/rb/frame.jpg` のロングポーリング（`since`/`rev`）で受けたJPEGバイトを `Blob` → `URL.createObjectURL` で設定する（旧：data URL）。新しい画像はまず画面外の `Image` で `decode()` してから切り替え（ダブルバッファ）、直前のオブジェクトURLは `URL.revokeObjectURL` で解放する。`.src =` は iframe の `validatedUrl` と、この `probe`/`img` の2箇所。ポーリングは「リモートタブが表示中かつページが可視」のときだけ1本。外れるとHubが5秒でスクリーンキャストを止める。
- 入力: タップ→`tap`、ドラッグ／PCのホイール→`scroll`（v0.1.22：差分を合算して**アニメーションフレームごとに1回**、前の送信が完了するまで次は送らず合算を続ける）。座標はHubが返すフレームの幅・高さ（CSSピクセル）に換算。「キーボード」ボタンで非表示の入力欄にフォーカス（iOSのキーボード用）、IMEは `compositionend` で確定文字だけ送る。Enter/Backspace/Tab/矢印/Esc/Delete は `key`。戻る/進む/再読み込みは `back`/`forward`/`reload`。
- 「リンク N件」バッジ: ページ読み込み完了の1.2秒後に `GET /api/rb/links` で更新。タップで下からのシート（URL一覧）、「N件を追加」は既存の `/api/rapidgator/extract`（プレビュー→digest付きcommit）で追加したリンクへ、「JD2へ送る」（エンジンの `can_send` のときだけ）は追加後に既存の `sendLinks`（確認ダイアログあり）。
- 復元: 再読み込み後、リモートタブのURL履歴は復元するが、NAS側のタブは自動では開かない（「NASブラウザで開く」ボタン）。Webタブを閉じる／ログアウトすると対応するNASタブを閉じる。コンテナが自動停止していたら404で検知して案内する。
- 設定 › NASブラウザ（エンジンの次）: 状態（停止中/起動中/稼働中）、メモリ使用量とNAS空きメモリ、「初期設定（イメージ作成）」（確認ダイアログ→段階表示、エンジンの初期設定と同じ表示）、「停止」、メモリ保護と自動停止の説明、Container Station未導入の案内。
- 設定 › エンジン › 詳細: 従来方式のJava削除（`native_leftovers.present` のときだけ表示）。
- CSP: v0.1.22で `img-src 'self' data: blob:`（`blob:` はスクリーンキャストのオブジェクトURLにだけ必要。他のディレクティブは不変）。
- 既知の制約（実機未検証）: ピンチ拡大は未対応（操作はタップ/ドラッグ/キーボード）。（v0.1.22でページは端末の表示領域に合わせたサイズになった。）

### v0.1.22 速度・リンク検出
- **ビューポート**: 画面（`#remoteStage`）のCSSサイズとdevicePixelRatioを `POST /api/rb/viewport` で送る。送るタイミングは、最初のフレームを要求する前（タブを開いた／切り替えて戻った直後）と、`ResizeObserver`／`resize` の250msデバウンス後（回転・キーボードなど）。幅760未満はHub側でモバイルとして描画される。ブラウザの識別文字列は変えない。
- **タップ**: 座標は、最新フレームのヘッダ（`X-Frame-Width/Height`＝CSSサイズ）と表示中の`<img>`の大きさの比で換算する。
- **検出ボトムシート**（`#interceptSheet`）: `X-Frame-Intercepts` が増えたら `GET /api/rb/intercepts?tab=&since=` で取得し、キューに積む（最大20件、表示中のタブ分だけ先頭から1件ずつ）。見出し「リンクを検出しました」、URL（1行・末尾省略）、ボタン「JD2へ送る」（主ボタン。`can_send` のときだけ表示。確認ダイアログあり）「リンクに追加」（`can_send` でないときは主ボタン）「ページを開く」（ホスターのみ。Fetch検出を1回だけ素通しにして開く）「閉じる」。他に待機中があれば「ほかN件が待機中」。**ポップアップ（`kind:"popup"`）はシートを出さず**、画面下の小さなトースト「ポップアップ: <ホスト名> を開く」＋「開く」（`POST /api/rb/intercepts/open` でタブを遷移）を表示する。同時に1つだけ（最新が優先）、8秒で自動的に消え、タブ切り替え・閉鎖でも消える。ホスターのポップアップは従来どおりシート。従来の「リンク N件」バッジ（ページ内リンクの一覧）はそのまま。
- DOMは `textContent` のみ（`innerHTML` 不使用）。

## v0.1.24 送信後すぐ開始・サイトごとの設定・長押し確認・速度（実機未検証）

実機フィードバック（リンクは検出できるがJD2で待機のまま／まだ遅い）への対応。API・CDPの詳細は [container-runtime.md](container-runtime.md) §4。

### 送信後すぐダウンロード開始
- 設定 › 保存先「JD2へ送ったらすぐダウンロード開始」（`settings.send_autostart`、既定ON、`POST /api/settings/options {send_autostart|block_ads: bool}`、変化したときだけ書き込む）。ONのとき、Hubの送信経路は `addLinks` に `autostart:true` を付け、続けて `downloadcontroller/start` を呼ぶ（開始指示だけ失敗しても送信は成功扱いで、画面の「開始」を案内）。JD2本体の自動確定・自動開始（常にOFFを確認）は変更しない。確認ダイアログとトーストの文言は設定に合わせる。
- ホワイトリスト追加: `downloadcontroller/start`、`downloadcontroller/stop`、`linkgrabberv2/queryLinks`（`name/host/bytesTotal/uuid/packageUUID` のみ要求、最大50件）、`linkgrabberv2/moveToDownloadlist(linkIds, packageIds)`。
- ダウンロード画面: 見出しに「開始／停止」（実行中が1件以上なら「停止」。停止は確認ダイアログ）、「JD2で待機中のリンク」（`GET /api/jd2/linkgrabber`：件数と名前、最大50件、50件以上のときは「N件以上」）と「すべて開始」（確認後 `POST /api/jd2/linkgrabber/start {confirmed:true}`＝一覧のリンクと所属パッケージを `moveToDownloadlist`→`start`）。`POST /api/jd2/downloads/start|stop`（stopは `confirmed:true` 必須）。

### サイトごとの設定
- 設定 › NASブラウザ: 「広告をブロック」（既定ON）、「サイトごとの設定」（ホスト名、自動送信、ポップアップ［確認する／許可（新しいタブ）／ブロック］、広告ブロックの切替と削除）。`GET /api/rb/site-rules`（`{items,block_ads,max}`）、`POST /api/rb/site-rules {host, auto_send?, popup?, block_ads?}`、`POST /api/rb/site-rules/delete {host}`（管理者＋CSRF、ホストはDNS名として検証）。
- リンク検出シート: 「このサイトでは今後自動で送る」チェック（送信時に保存）。自動送信されたら「JD2へ自動送信しました」のトースト（送信済みのリンクは「すでにJD2へ送信済み」）。
- ポップアップのトーストに「常に許可」「常にブロック」。許可サイトのポップアップは新しいNASブラウザのタブで（Webタブが上限なら開いた元のタブで）開き、ブロックしたものはタブ下部に「ポップアップ N件ブロック」と表示。

### 長押しでリンク先を確認
- NASブラウザ画面を550ms以上・10px以内の移動で長押しすると、150ms後から指の位置に輪が広がり、確定で `POST /api/rb/probe`。長押しではタップを送らない（OSのコンテキストメニューも抑止）。
- シート「リンク先を確認しました」: リダイレクト回数と最終ホスト、見つかったリンク（各「送る」「追加」）、「すべてJD2へ送る」「リンクに追加」。見つからないときはシートを閉じて「リンクは見つかりませんでした（最終: ホスト）」とトーストで知らせる（v0.1.26。シートが残るのは自動送信オフ／送信不可でリンクがあるときだけ）。確認中は最大15秒（閉じても確認用ページはHub側で必ず閉じる）。

### 速度
- ⋯メニュー（NASブラウザのタブのみ）: 画質「速度優先／標準／高画質」（既定は速度優先＝等倍・JPEG 40。端末の `localStorage` `qnapdl.rbquality.v1`、try/catchで保護）、「速度表示」（画面左上に 直近5秒の fps・1枚あたりKB・フレーム要求の応答時間。クライアントのみで計測）。
- Chromium引数・広告遮断・`prefers-reduced-motion` は container-runtime.md §4 の v0.1.24 を参照。

## v0.1.25 長押しの自動送信・重複スキップ・履歴削除（実機未検証）

- 設定 › NASブラウザ「長押しで見つかったリンクを自動でJD2へ送る」（`probe_autosend`、既定ON）。長押しでホスターのリンクが見つかりエンジンが送信可能なとき、`POST /api/rb/probe/send {tab, probe_id}` がサーバ側に保持した直近の結果（タブごと・最大50件・5分）だけを送ります（クライアントのURLは受け付けません）。重複はスキップし、`send_autostart` に従い、確認ダイアログなしでトーストのみ表示します。失敗時・送信不可時・リンクなしのときは従来のシートを開きます。
- 単発のJD2送信（NASブラウザの検出シート、長押しシート）は確認なし。リンク画面の「すべてJD2へ送る」は5件以上のときだけ確認します。Packagizerに関する文言は削除しました（Hub管理のエンジンは `overwritePackagizerRules:true`）。
- 重複リンクは常に確認なしでスキップし、件数だけ表示します。
- リンク画面「履歴を削除」: `POST /api/links/clear {include_staged}` が送信済み・要確認の記録（任意で未送信も）を削除します。JD2・ファイルには触れません。
- ダウンロード画面「完了したものを一覧から消す」: `downloadsV2/cleanup`（`DELETE_FINISHED` / `REMOVE_LINKS_ONLY` / `ALL`、固定値）。保存ファイルは削除されません。

## v0.1.28 NASブラウザの高速表示（全体表示）（実機未検証）

スクロールのたびに「スマホ → NASで描画 → JPEG → スマホ」を往復していた遅さをなくすための新しい既定表示。従来のスクリーンキャストは「ライブ」として残る。

- **表示の選択**: ⋯メニュー（NASブラウザのタブのみ）に「表示: 高速（全体表示）」「表示: ライブ」。サイトごとに端末の `localStorage` の既存ホスト別マップ（`qnapdl.hostmodes.v1`）へ保存する。値は `remote`（高速、**既定**。エントリが無いときも高速）と `remote-live`（ライブ）。どちらも「このサイトはNASブラウザ」を意味し、設定 › NASブラウザのサイト一覧・解除も共通。
- **画面（高速）**: ネイティブスクロールのコンテナ（`#tileScroll`）に、絶対配置の `<img>` タイルを並べる（高さ＝タイルの高さ×コンテナ幅/ページ幅）。`IntersectionObserver` で画面の上下1画面以内のタイルだけ `/api/rb/tile.jpg` から取得して `blob:` のオブジェクトURLを `<img>` に設定し、3画面より離れたタイルは `URL.revokeObjectURL` で解放する。同時取得は3枚。画面には何も重ねない（タップ位置はリンク位置表で当たり判定するだけ）。
- **撮り直し**: 上部に細い進捗バーを表示。ナビゲーション完了時（タイトル/URLの変化）、キー・文字入力の800ms後、タップ後、10秒ごとにページが「DOM変更あり」と報告したとき、上端から下へ引っ張る／右上の「⟳」で撮り直す。新しい表示の最初のタイルが届くまで古い表示を残し、URLが同じなら位置を保ち、違えば先頭へ。
- **操作**: タップ → `POST /api/rb/tap-page`（ページ座標）。長押し → クライアントが持つリンク位置表からリンク先を特定し、`POST /api/rb/probe {tab, href}` を即送信（Hubが `browser_safety.validate` と Hub自身のオリジン拒否で検証し、確認用ページを開くのは従来どおりHub）。リンクが無い位置の長押しは何もしない。キーボード・Enter・⌫・Tab は従来の `/api/rb/input`。
- **Cloudflare**: Hubが「タイトルに `Just a moment`／`しばらくお待ちください`、またはURLに `/cdn-cgi/challenge`」を検出し（`challenge` フラグ。`/api/rb/meta`・`/api/rb/snapshot`・`frame.jpg` のヘッダ `X-Frame-Challenge`）、そのタブをライブに自動で切り替える。確認画面が終わって読み込みが済むと元の表示へ戻る。
- **安全**: タイルは画像だけ。リモートのHTML/JSは端末に届かない。リンクの `href` は当たり判定と長押しの入力にだけ使うテキストで、Hubが再検証してから動く。CSPは `img-src 'self' data: blob:` のまま変更なし。

## 高速表示のAPI（v0.1.28）
- `POST /api/rb/snapshot {tab, force?, if_changed?}` → `{snap_id, width, height, tile_height:1600, tiles, scale, quality, url, title, links:[{x,y,w,h,href}], inputs:[{x,y,w,h}], challenge, reused}`。直前の変更なしのスナップショットが2秒以内なら再利用（`reused:true`）。`if_changed` は10秒ごとの確認用で、ページが「DOM変更なし」と答えれば再利用する。
- `GET /api/rb/tile.jpg?tab=&snap=&i=` → `image/jpeg`（初回要求時に描画）。スナップショットが破棄済みなら404（クライアントが撮り直す）。
- `POST /api/rb/tap-page {tab, snap, x, y}`（ページ座標）→ `{ok, navigated, editable, snap_needed:true}`。
- `POST /api/rb/snapshot/release {tab}`（ライブへ切り替えたときにタイルをRAMから解放）。
- `GET /api/rb/meta?tab=&rev=&wait=`: スクリーンキャストを始めないタブのメタデータ（URL・タイトル・読み込み・検出数・`challenge`・最新の`snap`）のロングポーリング。
- `POST /api/rb/probe` は `href` を受け付ける（`x`,`y` の代わり）。いずれも管理者ログイン必須、POSTはCSRF＋Origin。


## v0.1.30 NASブラウザ: リンク認識の強化・リーダー表示・表示方法の選択（実機未検証）

### 表示方法（設定 › NASブラウザ「既定の表示方法」）
- 3つ: **リーダー** / **高速（全体表示）** / **ライブ**。DB設定 `rb_default_mode`（`reader`/`fast`/`live`、既定 `fast`）。
- サイトごとの記憶（端末のホスト別マップ）が優先。値は `remote`（設定の既定に従う）・`remote-fast`・`remote-live`・`remote-reader`。⋯メニューの「表示」で3つとも選べる。
- Cloudflareの確認画面は従来どおり自動でライブになり、終わると選択中の表示へ戻る（ボタンは「元の表示に戻す」）。
- 「リンクの枠を表示」（`rb_link_boxes`、既定OFF）: 高速・ライブでリンク範囲に薄い枠線を重ねる（端末側のオーバーレイ、pointer-events なし）。

### リンクの認識（全表示）
- 端末側: 長押し位置を含むリンク枠、なければ28 CSS px以内で最も近い枠（同距離は面積の小さい方）。なければHubへ座標で問い合わせ（高速表示はスナップショットIDとページ座標、Hubがスクロールして変換）。
- Hub側の座標解決も同じ許容: `elementFromPoint`→`closest('a[href]')`、なければ全アンカーの `getClientRects()` で28px以内の最近傍。
- ライブ表示: `GET /api/rb/meta?tab=&links=1[&vp=<rev>][&fresh=1]` が `viewport_links` `{rev, scrollX, scrollY, width, height, links:[{x,y,w,h,href}]}`（ビューポート基準CSS px、最大600件、http(s)のみ）を返す。ページに注入したフラグ（スクロール終了200msデバウンス、リサイズ、MutationObserver）が変化を示すときだけ再走査し、画面フレームごとに `X-Frame-Links-Rev` が進む。端末はライブ表示中かつ画面が見えているときだけ取得（最短500ms間隔）。長押し直前に古ければ `fresh=1` で取り直す。

### リーダー表示
- `POST /api/rb/reader {tab}`: 固定の抽出スクリプトをページ内で実行（本文を文書順にたどる）。出力ブロック `h`/`p`（runs: text, href?）/`li`（depth）/`img`（i, alt, w, h, href?）/`pre`/`hr`。script/style/nav/aside/footer/header/form・非表示・広告らしいclass/id・極小/画面外要素は除外。除外領域にあるRapidgator系リンクも最後の「ダウンロードリンク」ブロックに追加。上限: 5000ブロック、JSON 2MB、1ブロックの文字数20000。Hubが全フィールドの型・長さ・hrefを再検証する（http/https・2048文字まで）。
- 画像URLは端末へ送らない（Hubが画像番号→URL表を持つ、1タブ300件）。`GET /api/rb/reader/img?tab=&i=`: 読み込み済みなら `Page.getResourceTree`＋`Page.getResourceContent`、なければページ内 `fetch(url,{credentials:'include'})`。4MB上限、種別はマジックバイトで判定（JPEG/PNG/GIF/WebP/AVIFのみ。SVGは拒否）、`Cache-Control: no-store`・`nosniff`。不明な番号は404、拒否は415、取得失敗は502。
- 端末は `createElement`/`textContent` だけで組み立て（innerHTMLなし）。リンクは `<a data-href>`（実際のhrefなし）: タップでNASブラウザが遷移（読み込み後に再抽出）、長押しは正確なhrefでそのまま確認。画像はIntersectionObserverで遅延読み込み（blob URL、遠くなったら解放）。ネイティブスクロール・ピンチ・文字選択が使える。

### Webタブまわりの整理
- 表示されない場合のバーは折り返し可で「NASブラウザ」を先頭に、「互換」を続ける（各36px以上）。
- 「Safariで開く」系（Webタブ行のボタン、バー、⋯メニュー、ホスト別の「常にSafariで開く」、設定の一覧）は廃止。保存済みの `external` は読み込み時に `remote`（NASブラウザの既定表示）へ変換。iPhoneの共有シート／ブックマークレット（設定 › iPhoneから送る）は残る。

### ライブ表示のストリーム（MJPEG push）
- `GET /api/rb/stream.mjpg?tab=ID`（ログイン必須、Host/接続元チェックは他と同じ）: `multipart/x-mixed-replace; boundary=qdlframe`、`Cache-Control: no-store`。各パートは `Content-Type: image/jpeg` と `Content-Length` 付き。最新フレームだけを即送信し、書き込みが遅いときは途中のフレームを飛ばす。15秒フレームが来なければ最後のフレームを再送（ハートビート）。書き込みエラー・タブ閉鎖・コンテナ停止・10分経過で終了。
- 上限: 1タブ2本、全体4本、超過は429。ストリームは閲覧中としてカウントされ（アイドル停止の判定に入る）、他のリクエストを止めない（デーモンスレッド、ソケットのタイムアウト10秒）。最初のフレームが届いた時点で応答が始まる。
- 端末: ライブ表示では `<img>` の `src` を同一オリジンのストリームURLにする。タブ情報・リンク位置表の版・fpsは `/api/rb/meta`（2秒ロングポーリング）から受け取る（fpsはHub側で直近3秒から算出）。ストリームが10秒以内に2回エラーになるか、5秒以内に画像が出ない場合は、そのタブだけ従来の `frame.jpg` ポーリングへ切り替える。タブが非表示・切替・表示方法の変更・ログアウトの際はストリームを閉じる（`src` を外す）。

### 速度表示（⋯ › 速度表示）
- 先頭に表示の種類と方式を出す。ライブ（ストリーム）: 「ライブ(ストリーム)・速度優先 · 9.8 fps · 22 KB/枚 · 応答 120 ms」（応答は最初の画像が出るまで）。ライブ（ポーリング）: 「ライブ(ポーリング)・標準 · …」。高速: 「高速(全体表示)・速度優先 · タイル 4/12 · 撮影 850 ms · 1枚 140 KB」。リーダー: 「リーダー · ブロック 230 · 画像 12/40 · 取得 600 ms」（抽出時間、画像があれば「画像取得 ○ ms」の平均を追記）。fps以外は端末側で計測。

## v0.1.33 UI/UX・処理の全面見直し
仕様と計測は [ux-review-0131.md](ux-review-0131.md)。要点:
- **設定は4節**: `#/settings/jd2|browser|connection|system`。節内の小見出し（jd2: engine/accounts/storage/send、browser: nas/rules/bookmarks、connection: https/share、system: updates/diagnostics/about）は `#/settings/jd2/accounts` のように指定するとスクロールする。旧URL（engine, accounts, storage, https, share, bookmarks, updates, diagnostics, about）は新しい位置へ `replaceState` で移る（`#/settings/browser` と `#/settings/jd2` は新しい節の先頭）。
- **表示の抑制**: 説明は1行（`.set-text`、はみ出しは省略）。長い説明は行末の「ⓘ」（`<details class="hint">`、閉じた状態）。設定行は左ラベル・右コントロール（`.set-row`）。オン/オフは `role="switch"` のスイッチ。既定の表示方法は選択ボックス。保存先はボトムシート（選ぶと閉じる）。リンク画面の履歴削除はヘッダーの「⋯」（履歴があるときだけ表示、未送信を含める操作は別項目）。
- **通知**: 同時に1件。同種（group）は件数を足して1件にまとめる（例「JD2へ送信しました（3件）」）。エラーは閉じるまで残り、新しいエラーが置き換える。エラー表示中は成功の通知を出さない。更新の進行などの固定バナー（sticky）は別枠。
- **状態の一本化**: `GET /api/state?rev=N&wait=S[&full=1]`。`{rev, engine:{state, mode, running, can_send, preset_applied, installed, autostart, error, cs, sig}, downloads:{running, waiting, finished}|null, rb:{state, tabs, mem, sig}, settings_rev, version, https, tailnet}`。`full=1`（初回のみ）は settings と bookmarks も返す。`rev` が違えば即時、同じなら最大25秒待つ。`sig` は詳細ステータスの変化検出用で、設定›JD2／NASブラウザが開いているときだけ `/api/engine/status`・`/api/rb/status` を取り直す。`/api/status` `/api/engine/status` `/api/rb/status` `/api/jd2/status` は互換のため残るが、周期的には呼ばない。
- **スケジューラ**: 周期処理（ダウンロード一覧5秒など）は `sched` に登録し、表示中のみ実行。`setInterval` は使わない。
- **遅延初期化**: 設定とNASブラウザのイベント設定は初めて開いたときに行う（`ensureSettings` / `ensureRemote`）。
- **サーバー側のキャッシュ**: `ttlcache.SingleFlightCache`。engine 2秒・JD2件数/状態 5秒・ダウンロード一覧/待機リンク 2秒・NASブラウザ 2秒。同時の呼び出しは1回の取得を共有し、書き込み系のPOST（NASブラウザの画面入力を除く）でキャッシュを捨てる。docker inspect / stats、アカウント一覧・ホスト一覧は同時呼び出しの共有（TTLなし、`_require_ours` と `inspect_ip` は常に自分で確認）。
- **削除**: `/api/rb/frame`（JSON）、`/api/jd2/local-preflight`（3128番の事前診断）とその画面、個別ポーリング用のタイマー、未使用CSS。


## v0.1.35 NASブラウザ専用化・ディスク書き込み計測・デバッグログ
- 端末側のiframe表示（強い制限／限定操作／互換）、そのメニュー、表示されない場合のバー、サイト別のモード記憶を削除。httpsのWebタブはすべてNASブラウザ（`openRemote`）で開く。サイトごとに記憶するのは表示方法（リーダー／高速／ライブ）だけで、古い記憶（iframeモード・`external`・`remote`）は読み込み時に破棄する。CSPは `frame-src 'none'`。
- NASブラウザが未設定・開けないときは、タブに「NASブラウザが準備できていません」と設定 › ブラウザへのボタン1つだけを表示（`/api/state` の `rb.ready` を使い、未設定ならHubへ要求しない）。
- 設定 › システム › 診断: 「ディスク書き込み」（`GET /api/diag/diskio`、`POST /api/diag/diskio/start|stop`）と「デバッグログ」（`GET /api/debug/log?since=&limit=`、`POST /api/debug/log|config|clear`）。どちらもRAMのみ。

## v0.1.37 リーダー表示の見た目（実機未検証）

- ヘッダーの「Aa」で表示設定のボトムシート（既存のシートの見た目を流用、背景は薄いベールだけでページを見ながら調整）。すべて端末の `localStorage` の `qd.readerPrefs`（1つのJSON。取得・保存は try/catch、使えなければ既定）。値は項目ごとに検証し、不正な値はその項目だけ既定に戻す。旧 `qd.readerSize`（小/標準/大/特大）は初回のみ最も近い段階へ移行。
- 項目: 文字サイズ 13/14/16/18/20/22/24/26px、書体（ゴシック＝-apple-system・Hiragino Sans・Hiragino Kaku Gothic ProN・Noto Sans JP／明朝＝Hiragino Mincho ProN・Yu Mincho・Noto Serif JP。端末内蔵のみで Webフォントは取得しない）、テーマ（自動＝アプリのライト/ダークに追従／ライト／セピア／ダーク／ブラック #000）、行間 1.6/1.85/2.1、本文の幅 30/36/44em（スマホでは画面幅−余白）、両端揃え（`text-align:justify`＋`line-break:strict`、見出し・カードには適用しない）、画像（大きく／標準／小さく／表示しない）、連続画像をまとめる（3枚以上を2列。切替時のみ再描画）、見出しを強調、進行状況バー、「既定に戻す」。
- 色はトークン（`--rd-*`）で定義し、本文・補助文・リンクとも各テーマで背景に対し 4.5:1 以上（tests/test_web_shell.py が計算で検証）。
- 画像: 抽出スクリプトが `srcset`/`data-srcset`/`<picture><source>`（`w`・`x` 記述子対応）から「幅1280px以上で最小」、なければ最大の候補を選ぶ（NASのページはDPR1でcurrentSrcが小さいため）。選んだ幅と縦横比をブロックの `w`/`h` に入れ、端末は縦横比どおりのスケルトンを表示してレイアウトずれを防ぎ、読み込み後にフェードイン（`prefers-reduced-motion` ではアニメーションなし）。`IMAGE_MAX_BYTES`（4MB）と判定は従来どおり。大きい候補が4MB超・非対応形式・取得失敗のときは、Hubだけが持つ代替URL（ページが実際に表示していた小さい方）で1回やり直す。
- 画像ビューア: 通常の画像のタップで全画面（ピンチ／ダブルタップ／ホイールで拡大、拡大中はドラッグで移動、拡大していない状態で左右スワイプ＝前後の画像、下スワイプ＝閉じる、✕・Esc・矢印キー）。リンク付き画像は従来どおりタップ＝リンク先、長押し＝確認で、虫眼鏡ボタンでビューアを開く。「表示しない」では1行のプレースホルダ（alt付き）をタップすると読み込む。
- 本文: `BLOCKQUOTE` 内の段落は `p` ブロックの `q:true`（Hubは真偽値 `true` のときだけ通す）で、罫線つきの引用として表示。リストの行頭は折り返してもそろう（ぶら下げ）。約物のぶら下げ（`hanging-punctuation`）・見出しだけの `palt`／`text-spacing-trim` は対応ブラウザのみ。
- 進行状況バー（既定ON）と読む位置の記憶: タブ×URLごとにRAMへスクロール位置を保持（最大40件）。同じURLの取り直し・表示の切替・タブ切替から戻っても同じ位置へ。
- セキュリティは従来どおり: ページのHTML/CSS/スクリプトは端末へ届かず、画像はHubのエンドポイント経由のblob:のみ、リンクは `data-href`、新しいフィールド（`q`・代替URL）もHubが再検証し、CSPは変更なし。

## v0.1.39 ミラー表示（デジタルツイン・フェーズ1／実機未検証）

4つ目の表示方法「ミラー」。NASのChromiumがページを描画し、Hubがその**無害化したコピー（HTML＋CSS、スクリプトなし）**を作り、iPhoneは `<iframe>` でそれを表示します。スクロールは端末のネイティブ（慣性・ラバーバンド）で、レイアウトはNASの幅（端末の幅）で確定済みのものです。メニューの「表示」は4分割（リーダー／ミラー／高速／ライブ、320px幅でも1行）、設定 › ブラウザ › 既定の表示方法にも「ミラー」を追加。サイトごとの記憶・`tabDisplay`/`applyDisplay` はほかの表示方法と同じです。

### 構成
- NAS側（`rb_mirror.py`、`remote_browser.py`）:
  - ページごとに分離されたworld（`Page.createIsolatedWorld`）で**固定のシリアライザ**を実行（ページのJSからは見えない／書き換えられない）。`script`/`noscript`/`template`/`iframe`/`object`/`embed`/`canvas`/`video` などは落として「埋め込みコンテンツ」のプレースホルダーに、`on*`・`javascript:`・`srcdoc` などは除去、`form`は`div`、`input`/`textarea`は`readonly`＋`inputmode=none`（キーボードは出さない）。リンクは `data-href`、要素には `data-m` の安定ID。画像は `srcset`/`<picture>` から「幅1280px以上で最小」をリーダーと同じ規則で選択。
  - CSSはCDP（`CSS.getStyleSheetText` ほか）から取得し、`url()` はすべてHubのトークンに置換。`@import` は取り込み、`behavior`/`-moz-binding`/`expression()` は除去。
  - 上限: 要素30000／HTML 2 MiB／CSS 2 MiB／リソース参照500（超過は切り詰めて通知）。キャプチャは（URL, DOMの版）ごとにキャッシュ。
  - DOM変更はMutationObserver（800 msに1回まで）でカウントし、`/api/rb/meta` の `mirror_rev` として長いポーリングに乗せます。
- Hubのエンドポイント（すべてログイン・CSRF・Origin必須）:
  - `POST /api/rb/mirror`（gzip対応）／`GET /api/rb/mirror/doc?tab=&rev=`（iframeの文書）／`GET /api/rb/mirror/res?tab=&r=`（画像・フォント。RAM LRU 48 MiB、`Cache-Control: private, max-age=300`、SVGは `default-src 'none'; style-src 'unsafe-inline'` のCSP付き）／`POST /api/rb/mirror/click`（リンクは既存の遷移経路、それ以外は scrollIntoView＋実マウスクリック）／`POST /api/rb/mirror/release`。
  - 失敗は日本語のメッセージ（古い表示は410、非対応の形式は415、取得失敗は502）。debug-log のカテゴリは `render`。
- 端末側（`web/app.js` の mirror 節）: iframeの文書はHubから読み、以後の版は `DOMParser`（iframe自身のもの）で解析して `body` を差し替え（読み位置を保持）。タップ・長押し（既存のprobe）・クリック転送は親が `contentDocument` に付けたリスナーで処理。更新は1秒以上あけ、触っている間は待ちます。2回失敗するか空の結果なら高速表示に戻してトースト通知。速度表示は「ミラー · 要素 N · HTML N KB · CSS N KB · 画像 n · 取得 N ms」。

### セキュリティモデル
- iframeは `sandbox="allow-same-origin"` のみ（`allow-scripts`・フォーム・ポップアップ・トップ遷移なし）。スクリプトが動かないため、親が `contentDocument` を操作できても中身は何も実行できません。
- 二重のCSP: 文書のレスポンスヘッダ（`default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; frame-ancestors 'self'` ほか）と、文書内の同じ内容の `<meta>`。アプリ本体のCSPは `frame-src 'self'`（外部ページは枠に入れない。`srcdoc` は親のCSPを継承してしまうので使わず、同一オリジンの `/api/rb/mirror/doc` を読む）。
- 端末は外部サイトと一切通信しません。画像・フォントはHubのトークン経由（トークンはタブごと・RAMのみ・遷移で破棄、中身は先頭バイトで形式確認）。
- NAS側の実行はページから分離されたworldに限り、`Runtime.enable` は使いません（検知対策。カウンタはHubがポーリング）。

### 今後（フェーズ2・3）
- 差分（DOM増分）の送信、フォーム入力の転送、フォントまわりの調整。

### 未検証
- 実機NAS（Chromiumコンテナ）での動作、iOS Safariでのsandbox付きiframe・ネイティブスクロールの挙動（検証はデスクトップChromeと390px幅のエミュレーションのみ）。

## v0.1.44 自己テスト（設定›診断›自己テスト）（実機未検証）
「ボタンを押すと自動でテストして結果を出し、改善につなげる」ための画面。GitHubのE2Eでは見えない実機のNAS（TS-253Be）・iPhone Safari・実サイトの状態を、そのまま貼れる1つのレポートにする。

- 画面: 診断の最初のサブ。「自己テストを実行」（同時に1回、進み具合、中止）、任意の「実サイトも測る（URL、改行で最大5件）」（オムニボックスと同じ検証。入力はlocalStorageに記憶）、
  結果（合格/注意/失敗と所要時間、セクションごとの表、各行の状態アイコン・値・「改善のヒント」）、「結果をコピー」（クリップボード、だめなら選択できるテキスト欄）、「結果を保存」（端末側で作るファイル。Hubは何も書かない）、
  「URLを含める」（既定オフ。オフならURLはホスト名だけ）、直近5件の履歴（HubのRAMのみ）。
- Hub側（`selftest.py`、背景スレッド、上限4分）: 環境（CPU・メモリ・データ領域・Container Station・イメージ・JD2）→ NASブラウザの起動 → 内蔵テストページ
  （リーダー・ミラー・JSメニュー・動的更新・タイル・ライブのフレームレート・変化通知）→ 任意の実サイト → 後片付け。内蔵ページは `Fetch.fulfillRequest` でRAMから返す仮想サイト
  （`https://selftest.example.com/`）で、ネットワークを使わず、内部タブとしてユーザーのタブ数に数えず、最後に必ず閉じる。
- 端末側（`web/app.js`）: 画面外の保持領域で、アプリの純関数（`cleanReaderData`・`mirrorUnsafe`・`mirrorMorph`・`wireMirrorDoc`）をそのまま使って測る。ユーザーのタブや表示には触れない。測定値だけをHubへ送り、判定はHubが行う。
- API: `GET /api/selftest/status?since=&wait=`（ロングポーリング）、`GET /api/selftest/results[?id=]`、`GET /api/selftest/report?id=&urls=0|1`、
  `POST /api/selftest/start`・`/cancel`・`/client`（管理者・CSRF・Origin。`/client` は32 KiB上限・項目の型と範囲を検証）。同時実行は409。
- レポート: アプリのバージョン、NASの事実、端末のUA・viewport・DPR・エンジンのフラグ、全項目の値・しきい値・状態、デバッグログ末尾50行（rb/render/engine/http）。パスワード等とURLの中身は入れません。
- 未検証: 実機NAS、実iPhone（WebKit）、実サイト、headfulなChromiumでのライブfps（ヘッドレスでは断続的になるため、fpsは注意止まり）。
