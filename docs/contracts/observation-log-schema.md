# 観測ログ schema v1 — 二層 data plane（凍結）
> **用語注記**: `wip#NN`・`pgl#NN`・`wip-persona-engine` は公開前の非公開トラッカー/リポの史料参照。

> Status: **FROZEN**（pgl#4 lane #0）。実装先 = 観測コレクタ v1（pgl#5）・使用ログ（pgl#6）・Tier S（pgl#7 鏡）。
> plugin-convention seam 4（data plane）からの逸脱を明示: **Tier L の local raw は vault へ同期しない**。
> vault に出るのは Tier S の集計値のみ。
> **P2 不変条件: pgl 自身が生成した発話・状態変化（受け入れ実ターン・probe・warm-up 等）は、観測にも証拠にも決して入らない。**

## 1. 二層の定義

| 層 | 実体 | 内容 | 同期 |
|---|---|---|---|
| **Tier L**（local raw） | `~/.persona-growth-loop/obslog/<face>/YYYY-MM-DD.jsonl`（MBP の PGL_HOME・dir 0700・file 0600） | フィルタ・scrub 済みの発話断片 | **同期禁止**（マシン間・vault いずれも）。バックアップ対象からの除外を推奨（Time Machine 除外は operator 設定・setup チェックで警告） |
| **Tier S**（shared） | 共有 vault（オーナー家族の既存 data plane 置き場・private） | **集計値のみ・phrase 本文なし**（§5） | 書き出し前 secret-scan 必須 |

phrase 本文を含むレポート（鏡ディープレポート等）は Tier S に出さず、**オーナー限定置き場**
（本リポ private `docs/records/` または `~/.persona-growth-loop/reports/`）に置く。
家族共有面への可視範囲は CP-4 のオーナーゲート。

Tier L の「同期禁止」は、scrub 済み Tier L ファイルを第二ホストへ複製しないという意味である。
remote runtime を観測源とする面は、read-only transport で raw を MBP のプロセスメモリへ通過させてよい。
ただし raw は MBP 上の通常ファイル・`/tmp`・spool・DB copy・一時 rsync を含むいかなる path にも
永続化せず、同一プロセス内で filter → scrub を完了してから Tier L だけを書き出す。remote 側にも pgl 用の
raw archive を新設しない。

## 2. Tier L 抽出契約（content-block レベル・機械可読）

対象 v1 = Claude Code transcript JSONL（Alpha 面）。
Hermes Luca 面は §2.6 の子契約を本契約へ追加する。
起動形態 = **夜間バッチ**（当日分 transcript を1日1回走査。ハーベスターと同じ夜間パイプライン内・
lock/killswitch 規律は overlay-contract §9/§10 に従う。session-end hook 等のリアルタイム収集はしない —
実装詳細は pgl#5）。

### 2.1 include（この2形のみ）

1. `type=="user"` の turn の `message.content[]` のうち `type=="text"` のブロック
2. `type=="user"` の turn で `message.content` が**素の string** の場合、それを text ブロック1個と等価に扱う
   （#0 での明確化: 実測分布で user turn の content は tool_result 88 / text 5 / plain-string 4 —
   plain-string は人間のタイプ入力の実体であり、除外すると観測が痩せる。
   tool_result を含む turn の除外という原契約の意図に反しない）

### 2.2 exclude / 除去（順に適用）

| # | 規則 |
|---|---|
| 1 | `type!="user"` の turn 全体（assistant・system 等。assistant は §4 使用ログのみ） |
| 2 | `toolUseResult`・`content[].type=="tool_result"` を含むブロック全体 |
| 3 | `<system-reminder>…</system-reminder>` 全域を除去 |
| 4 | `<command-name>` / `<command-message>` / `<command-args>` / `<local-command-stdout>` タグ行を除去 |
| 5 | コードフェンス（``` で囲まれた領域）を除去 |
| 6 | パス様行（`://` を含む・行頭 `~/` `/` `\\`・`/Users/` を含む）を除去（platform 注記・非規範: `/Users/` は macOS のホーム接頭辞。Linux/WSL2 のホームパス `/home/…` は行頭 `/` `~/` の規則で同様に除去される） |
| 7 | 断片長 > **240字（コードポイント数）** はその断片ごと**破棄**（貼り付け長文 = 第三者テキストの候補化除外を保存段階で実施。口ぐせ収穫に長文は不要 — データ最小化を兼ねる） |
| 8 | 鍵様・base64 様はその断片ごと破棄: `AKIA` / `sk-` / `ghp_` / `github_pat_` / `xox` / `-----BEGIN` / `eyJ` 接頭 / `[A-Za-z0-9+/=]{40,}` 連続 |
| 9 | 面別 denylist（Alpha/Claude Code は `config/obs-denylist.txt` の project/cwd 一致で当該 session 全体、Luca/Hermes は §2.6 の定め〔意図ジャーナル窓 + 受け入れ session 台帳を正本・prefix は第三層〕で除外。粒度 = 台帳/prefix は当該 **session 全体**・ジャーナル窓は**窓内の全行**。断片単位へ狭めない） |

### 2.3 record schema（JSONL 1行）

```json
{"ts": "2026-08-02T23:31:00+09:00",
 "host": "mbp",
 "face": "alpha",
 "session": "a1b2c3d4e5f6",        // session id の sha256 先頭12
 "project": "persona-growth-loop", // denylist 通過後の slug
 "speaker": "owner",                 // runtime 由来（§3）
 "text": "うん、その案で進めよう",
 "len": 11}
```

`host` は**観測源の来歴**であり Tier L の物理配置ではない。Alpha のローカル transcript は `"mbp"`、
Hermes Luca は `"vps-hermes"` とするが、どちらの Tier L も MBP の PGL_HOME にのみ置く。

- record は上記に加えて **optional `ucd`**（string・記録時の UCD〔Unicode Character Database〕
  バージョン来歴。G5 Unicode admission ゲートの検証条件を記録する provenance であり、
  hash・identity・session 帰属のいかなる入力にも混ぜない）を持ちうる（#26 追認 2026-08-09）。
- **record のキー集合は閉集合**である: 上記列挙 + optional `ucd` 以外のキー追加は本契約の改定を要する
  （harvester / aggregator は本閉集合を superset 検査で強制する — 実装実態の条文化・#26）。

### 2.4 書き出し時 scrub（第二層）

抽出後・書き出し直前に再走査: §2.2-8 の鍵様パターン再適用 + メールアドレス・電話番号のマスク
（`***@***` / `0**-****-****`）。フィルタ（第一層）と scrub（第二層）は独立実装で二重化。

### 2.5 保持・権限

- 30日 prune（夜間ジョブ。killswitch 中も実行 — 削除方向は安全側）
- dir 0700 / file 0600。作成時に umask ではなく明示 chmod
- 収穫（候補化）の関係スコープ: 面ごとに **speaker を閉じる**（alpha 面 = speaker=="owner" のみ候補化対象。
  他者の発話・第三者テキストは候補化しない）

### 2.6 Hermes Luca 子契約（normative）

- 正本は VPS の `~/.hermes/profiles/luca/state.db`。MBP の夜間コレクタは read-only ssh dispatcher の
  read 系サブコマンド（**閉集合 = `read-sessions` / `hash` / `read-owners` の3つ。observe 期はこれ以外を
  実装しない**。deploy 系〔deploy/restore/accept〕の追加は inject 域の契約改定 — 設計 §5 バッチ2 +
  D3 delta 再レビューを要する）を介し、SQLite `mode=ro` URI の一貫した snapshot から当日窓の
  `role='user'` turn を読む。旧 `~/.hermes/profiles/luca/sessions/*.jsonl` は決して読まない。DB/WAL の copy、
  rsync、scp、一時ファイル化は禁止。DB lock 競合・schema drift・dispatcher 失敗は当夜を非0で停止し
  `[RED]` とする。
- 対象 session は `source in {telegram, slack}` の 1:1 DM を基本閉集合とする。`api_server`（voice）は
  owner-level Bearer の信頼境界が文書化され、config で明示された場合だけ加えられる。`cli`、`webui`、
  group、channel、未知 source/chat type は reject する。
- user turn は `sessions.user_id` と runtime の session metadata を、config に明示した source 別 owner uid
  allowlist と突合する。uid 欠落・不明形式・帰属不一致は断片ごと reject し、本文から owner/speaker を
  推定しない。通過した user turn だけ `speaker: "owner"` を付与する。
- owner uid は Hermes runtime の owner/route 定義から転記し、setup 時に owner が確認する。その実体は
  Hermes の `channel_directory.json` の DM エントリである（実測 2026-08-07: Hermes 設定に
  `owner_verified` 様のフラグは存在しない — 設計 v1.1 §1.2-6 の「install.yml を実読」は本実体への
  読み替え。設計の意図 = 起動時に runtime の owner 検証定義と config の期待を突合、は不変）。コレクタは
  起動時に同定義を dispatcher の read 系サブコマンド経由で実読し、対象 route の DM/owner 集合が config の
  owner uid allowlist の期待と一致しなければ収集前に非0で停止して `[RED]` とする（未知の DM エントリの
  出現も不一致として停止する — fail-closed）。転記した uid 値は実 session fixture の runtime uid と
  突合して一致を必須検証する。突合対象の規範形: `channel_directory.json` の `platforms.<platform>[]`
  のうち **`type == "dm"` のエントリのみ**を比較集合とし、比較キーは `(platform, id)`。DM 以外
  （group/channel 等）は比較集合に含めない。未知 platform・型不一致・パース不能は自己検査失敗として
  停止する（fail-closed）。実測形（2026-08-07）:
  `{"platforms": {"telegram": [{"id": "100000001", "type": "dm", ...}], "slack": [...]}}`。
  config は record 帰属用の owner uid allowlist と、起動時自己検査用の**期待 DM エントリ集合**を**別キー**で持つ
  （telegram は DM エントリ id = owner uid と同値・slack は DM チャネル id で、thread 接尾辞 `:<ts>` を伴う
  前方一致を許容）。期待外 DM エントリの出現・対象 platform の DM エントリ消失は、いずれも自己検査失敗として
  停止する。
- Telegram group 前置 `^\[.+\|\d+\]\n` に一致する text は uid 検査後も reject する。既知の
  memory-context 等の注入マーカーは除去後に marker 様文字列が1つでも残れば断片全体を reject する。
- 文字列規則 §2.2-3〜8 と §2.4 scrub は共有ライブラリにより Alpha と**バイト同一**に適用する。
  filter（第一層）と scrub（第二層）は独立性を保つため、別 module・別 constant table のまま共有する。
  Claude Code 構造に固有の §2.2-1〜2 は Hermes の `messages.role`/content schema に置き換え、§2.2-9 は
  session/conversation・窓単位の denylist として適用する。P2 の除外は二層（luca-lane v1.2 §1.2-5）:
  **第一層 = 意図ジャーナル窓**（pgl 起因の api_server 発話は送信前に window open を fsync 記録・
  コレクタは open/close 実窓内の api_server 行を無条件不採用・close 欠落 = close または有人解決の記録まで
  **各バケット**で不採用 + RED。**ジャーナル自体が不在・読取不能・行不正なら台帳故障と対称に api_server
  当夜全不採用 + RED**〔「読めない = 窓 0 件」は不適合〕。margin は MBP↔VPS 実測時計差以上）、
  **第二層 = 受け入れ session 台帳**（`$PGL_HOME/state/luca-verify-sessions.jsonl`・追記型・0600・
  `origin ∈ {"seed","acceptance"}`）で、台帳記載 session は user/assistant turn とも query 対象へ入れない。
  `exclude_session_prefixes` は少なくとも `pgl-verify-` を含み、prefix が session id/key に残る観測源への
  第三層として維持する（現行 route 制約下の合致対象は 0 件の安全網。telegram/slack 側で合致が出現したら
  経路違反の兆候として WARN）。
- pgl の受け入れ・probe・warm-up は voice/`api_server` route に固定し、dispatcher `accept` の応答が返す
  session UUID 群を MBP 側が台帳へ fail-closed 追記する（追記失敗 = 受け入れ FAIL・wip-persona-engine#90。
  孤児 session は第一層の窓が遮断する）。Telegram/Slack の owner DM を受け入れ route に使うことは禁止する。
  台帳が**不在・読取不能・行不正（1行でも parse 不能なら全体不可の all-or-nothing）・voice 有効時の
  0 エントリ / `origin:"seed"` 不在**の run は `api_server` 行を丸ごと不採用とし digest RED を出す
  （fail-closed・他 source は独立に継続）。`voice_enabled: true`（または sources に `api_server`）のとき
  台帳 config キーは必須（欠落 = config ロードエラー）。
  既知受け入れマーカー形の検知は台帳・窓除外の**後**に走査し**警報のみ**: 完全一致 `deployment warm-up` =
  RED・接頭辞 `/persona ` = digest の件数記録行（正規 voice UX と重複し警報にしない）。content による
  除外・所有権推定は行わない。マーカー閉集合はコレクタのコード定数（変更は契約改訂に準じる）とし、
  受け入れ emitter 吐出文言との同期を fixture で表明する。台帳行数の単調非減少を前回 run と突合し、
  減少 = 行不正と同様に扱う。
- record は §2.3 と同じで `face: "luca"`、`host: "vps-hermes"`、`project: "hermes-luca"` を固定する
  （§2.3 の optional `ucd` と閉集合規定も同様に継承する — #26）。
  raw の扱いと保存先は §1、assistant turn の限定走査は §4 に従う。

## 3. speaker 識別

- **runtime 由来のみ**: transcript の role + セッションの runtime identity（どの対話面の transcript かは
  コレクタ設定で静的に決まる）から導出。
- **本文からの自己申告推定は禁止**（「〜です。オーナーです」等のテキスト内容で speaker を決めない —
  SPEC §6.2 の route ctx と同じ原則: モデル出力・メッセージ本文から信頼判定を作らない）。

## 4. 使用ログ（「user 発話のみ」原則の明示的限定緩和）

- 夜間バッチが assistant 発話を走査し、**adopted / candidates の phrase 出現マッチのみ**を記録する。
  assistant 発話の全文・マッチ前後の文脈は**保存しない**。
- **走査元は生 transcript の assistant turn**（コレクタと同じソースを直接読む）。Tier L は §2 で
  assistant turn を除外しているため使用ログの入力にはならない — 使用ログのパイプは Tier L 非経由と明記。
  Luca 面では「生 transcript」を、同じ read-only ssh pull / SQLite snapshot の `role='assistant'` turn と
  読み替える。user 観測と単一走査で phrase 出現だけをメモリ内照合し、remote raw・assistant 全文・
  マッチ前後文脈は永続化しない。P2 denylist は user/assistant の両走査へ session 単位で同一適用する。
- 目的: 帰属（candidates が実際に使われたか）と holdout 判定（evidence-rules §3）の前提。
  これが無いと昇格条件が検証不能になるため、原則の**限定緩和**として #0 で凍結する。
- record: `{"ts", "session"(hash12), "face", "phrase_id", "state": "staged|adopted"}` + **optional `ucd`**
  （§2.3 と同義の UCD バージョン来歴・hash/identity 非混入・#26 追認）（JSONL・Tier L 同置・
  同権限・同 prune。enum が staged|adopted なのは render に掲載されうる state がこの2つだけのため —
  overlay-contract §1 の state→render 対応。キー集合は §2.3 同様**閉集合**であり、追加は本契約の改定を要する）
- 収集系の liveness marker（`state/collector/<face>.last-run.json`）にも `ucd` が入るが、marker は
  Tier L / Tier S いずれの schema でもないため本契約では条文化しない（判断の記録・#26）

## 5. Tier S（vault へ出る集計値の全列挙）

週次（鏡ライトレポートと同時生成）。**以下以外の項目を Tier S に追加する場合は本契約の改定が必要**:

- 候補数 / staged 数 / adopted 数（面ごとの件数のみ）
- 当週の露出回数（`window_exposure_total`）
- `holdout_opportunity_total` = **holdout（非掲載）日に使用ログへ出現した件数**（当週窓・合計値）。
  day-toggle（evidence-rules §3）下では非掲載日の phrase は render に存在せず、**正常時は 0**。
  >0 は **protocol 逸脱**（履歴・記憶経由の使用等）の検知値であり、evidence-rules §3 が想定内の
  観測対象として扱う。
  注1: 本値は鏡週次の**逸脱計数器**であり、evidence-rules §3 の**群割当**（逸脱検知セッションは
  露出群として数える）とは別軸。
  注2: キー名は歴史的経緯（Tier S 閉集合保全のため改名しない — #26 明文化 2026-08-09）
- 負シグナル件数 / 明示的言及件数（件数のみ・本文なし）
- 昇格・降格・block 件数
- cap 使用率（render バイト数 / 上限）
- soul 定点ハッシュ照合結果（OK / MISMATCH / NO-BASELINE）。NO-BASELINE は基線 manifest が
  pin されていない状態を示し、operator は `pgl-baseline <face>` を実行する
- killswitch 状態（ON/OFF と mode のみ）

書き出し前に secret-scan（§2.2-8 パターン）を必須通過。**phrase 本文・発話断片・session id（生値）は
いかなる形でも Tier S に出さない**。

## 6. fixture テスト仕様（pgl#5 の受け入れ条件・数値凍結）

- fixture 構成（2部構成・件数は fixture メタ `expected_counts` に明記）:
  - **base 部**: 実 transcript から匿名化した JSONL。**実測分布を保存**すること:
    user turn 内 content-block 種別 = `tool_result` 88 / `text` 5 / plain-string 4
    （2026-08-02 実測・Opus 席 F3）。text/plain-string の9断片はクリーン（全除去規則を通過する内容）
  - **注入部**: §2.2 の各除去・変換規則に対応するサンプルを **user turn の text ブロックとして**追加
    （text ブロックとして埋めることで規則 3〜8 が実際に演習される）: system-reminder 断片 /
    command タグ行 / コードフェンス / パス様行 / **241字断片（破棄される）と240字断片（通る — 境界対）** /
    ダミー鍵 `AKIA…`・`ghp_…`・base64 様 / メール・電話（マスクされて通る）/ denylist project の session
- assert（全て必須）:
  1. tool_result 由来テキストが出力に **0 件**
  2. 抽出母集団（除去規則適用前）= base 9断片 + 注入 text ブロック N 件（N は fixture メタと一致）
  3. 最終出力 = base 9断片 + 240字境界断片 + マスク済みメール/電話断片 **のみ**（それ以外 0 件）
  4. system-reminder / command タグ / コードフェンス / パス様行が出力に 0 件
  5. 241字断片が 0 件・240字断片が 1 件（コードポイント境界テスト）
  6. ダミー鍵・base64 様断片が 0 件、メール・電話がマスク済みで存在
  7. denylist project の断片が 0 件
  8. 出力ファイル 0600・dir 0700
  9. speaker が fixture メタ（runtime 由来値）と一致し、本文由来の値でない
  10. 同一入力の再実行で出力バイト同一（決定論）
- fixture 自体にも実鍵・実 PII を置かない（ダミーのみ）。CI で毎回実行。
- Hermes Luca 子契約の実装 Issue は別 fixture を持ち、uid 欠落 reject、group 前置、注入 marker 残留 reject、
  240/241字境界、鍵様、マスク対、意図ジャーナル窓の api_server 全除外（close 欠落 = 解決まで各バケット +
  RED・**ジャーナル不在/読取不能/行不正 = 全不採用 + RED** を含む）、受け入れ台帳記載 session の全除外
  （台帳不在・読取不能・**行不正1行 + 正常行混在 → 全体不可**・**voice 有効時の 0 エントリ = 不採用 +
  RED**・未知 origin 値 reject・行数減少検知を含む fail-closed）、**台帳に実在しない誤 UUID を書いても
  実 session は除外されない負例**、**voice 故障時も telegram/slack が通常件数で継続する表明**、
  マーカー集合と受け入れ emitter 吐出文言の同期表明、`pgl-verify-*` prefix session の全除外（第三層）、
  0700/0600、決定論、ならびに remote raw の
  中間ファイルが通常 path・`/tmp`・spool のいずれにも存在しないことを検証する。
