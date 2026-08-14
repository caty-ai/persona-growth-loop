# Overlay 契約 v1（凍結）

> Status: **FROZEN**（pgl#4 lane #0）。適用範囲: overlay 層の全書き込み経路。
> 依拠する engine 側事実 = persona-engine SPEC v2 §2（pack schema）/ §4（build・budget）/ §5（正規形）。
> 本契約の改定は Epic チェックポイント（CP-2 以降）または新 Issue + S/M quorum レビューを要する。
> **改定記録**: v1.1 = 2026-08-04 G1/G2 方向二分（pgl#20・council 3席 + オーナー決裁「推奨案でいこう」・
> レビュー5席〔enforcement〕。§5.3 a/a2・§5.5 新設・§10・§14・§15。凍結 tag `contracts-v1` = a840c63 は
> v1.0 の来歴として不変・repoint しない）。
> 2026-08-09 #26 = 契約精緻化（§2 長さ保存形・§5 idempotency 拡張・§5.5 クロス参照・§12 計数対象）。
> 2026-08-09 #47 = inject 域バッチ2（設計正本 = docs/luca-lane-v1.md v1.2 FROZEN。§5.3 g0/a3 新設・
> e staging build 化・e2 挿入・f 後置、§5.4 e2 失敗規定 + 附則A、§5.5 luca 搬送段、§10 読み手行・
> 削除系手動操作行、§11 tag semantics + R13 luca 再定義、§15 クロス参照）
>
> **用語注記**: `flh` = fable-loop-harness（[caty-agent-harness](https://github.com/caty-ai/caty-agent-harness) の公開前の非公開系譜）。
> `flh#NN`・`pgl#NN`・`wip#NN` は公開前の非公開トラッカーの決裁史料番号（来歴参照として本文に保持）。
> `wip-persona-engine`（エンジン面の非公開開発リポ）・`alpha-wiki`（非公開設計 wiki）への言及も同じ非公開来歴の参照。
> governance 条文の現行正本は harness 公開版 [`docs/governance-rules.md`](https://github.com/caty-ai/caty-agent-harness/blob/main/docs/governance-rules.md)。

## 0. 契約の骨子（1画面サマリ）

1. overlay は**台帳**（注入されない）と **render 成果物**（注入される）の2面に分離する
2. 書き込みは**決定論 applier**（LLM でない・path-scoped）のみが行う。writer（LLM）は提案生成まで
3. 採用は**原子パイプ**（write → build → 検証 → commit+tag → 通知。失敗 = 全 revert+停止+通知）
4. **CP-2 発効前は overlay への書き込みを手動含め全面禁止**（例外 = §6 の空ファイル bootstrap。applier の初回起動は CP-2 発効+CP-3 GO 後 — §14）
5. soul には**永遠に書かない**

## 1. 2面分離

**overlay home 原則: 面ごとに2面（台帳+render）を単一の git repo に同居させる**（commit+tag が両面を
一括で覆い、snapshot 復元・rollback が1 repo 内で完結するため）:

| 面 | overlay home（git repo） | 台帳 | render 成果物（注入される） |
|---|---|---|---|
| luca（engine 面） | applier 専用 clone `~/.persona-growth-loop/faces/luca-repo/`（wip-persona-engine） | `growth/overlay-ledger.yml`（repo ルート相対） | `persona-engine/catalogs/overlay/{candidates,adopted}.txt`（staging install root の `pack/catalogs/` へ同期後、`catalog_refs` 経由・SPEC §2.2） |
| alpha（非 engine 面） | `~/.persona-growth-loop/faces/alpha/`（専用ローカル git repo・pgl#8 で init） | `overlay-ledger.yml` | `overlay.md`（CLAUDE.md からは本ファイルへの参照1行のみを人間が一度だけ設置） |

wip-persona-engine の実 repo に `pack/` はなく、`persona-engine/` が実 path である。`pack/` は VPS と
staging の install root における配置名。staging [2] は git 外の生成物であり snapshot 対象ではない（§11）。

- 台帳は**注入されない**・render は**注入される**。書き手はいずれも applier のみ。
- 新しい面の追加 = overlay home（git repo）+ 2面のパス + soul ファイル集合を宣言するだけ（一般化）。
  **面の新設宣言は governance-R12a 変更（council + オーナー承認）+ face ごとのオーナー GO（applier 初回
  起動前）を要する**（flh `docs/governance-rules.md` Face onboarding・Epic close 後も存続する常設規則）。
- 面ごとの **soul file set はコード所有**（`growthlane/faces.py`）である。Alpha 面は CLAUDE.md の
  `### Identity (アルファ)` / `### Warmth Persona Core v1` / `### F. 関係の記憶` の3節と overlay 参照行を
  節単位で抽出する。見出しは前方一致・位置非依存とし、対象見出しまたは参照行の一致が0件または複数なら
  fail-closed とする。**soul file set の変更は governance-R12a 級（council + オーナー confirmation）**である。

**state → render ファイル対応（凍結）**:

| 台帳 state | render 掲載先 |
|---|---|
| `candidate` | **なし**（台帳のみ・注入されない。初回掲載 = candidate→staged 遷移が §5 の diff レビュー必須通過点） |
| `staged` | `candidates.txt`（**稀少制約付き試用注入の実体**。Epic・裁定の「candidates=試用注入」はこの render 面を指す。candidates.txt の cap（§12）は staged エントリに適用） |
| `adopted` | `adopted.txt` |
| `demoted` / `blocked` | **なし**（台帳のみ・render から除去） |

- 台帳には evidence 統計・出所・制約メタ・state 履歴を置く。**台帳の内容がプロンプトに乗る経路は存在しない**
  （engine 面: `catalog_refs` の path allowlist に台帳を含めない。doctor 級 lint で検査）。
- render 成果物は **phrase スロットのみ可変**。地の文（説明フレーム）は soul 側凍結（§4）。

### 1.1 台帳 schema（schema_version: 1）

```yaml
schema_version: 1          # int。未知 version は applier が fail-closed（§13）
face: luca                 # 面 id: luca | alpha | ...
phrases:
  - id: p-0001             # 連番・欠番可・再利用禁止
    text: "なるほどね"
    state: candidate       # candidate | staged | adopted | demoted | blocked
    source:
      first_seen: 2026-08-02
      window_count: 9      # 30日窓の出現回数（evidence-rules §1）
      distinct_days: 6
    staged_at: 2026-08-16  # staged 遷移日（同時試用ローテーションの決定論キー・evidence-rules §2）
    holdout:               # 日次 render トグル方式（evidence-rules §3）
      exposed_days: 7      # 掲載日数
      holdout_days: 6      # 非掲載日数
      exposed_neg: 0       # 掲載日群の負シグナル件数
      holdout_neg: 0       # 非掲載日群の負シグナル件数
      exposed_mentions: 1  # 掲載日群の事後明示言及件数
      holdout_mentions: 0  # 非掲載日群の同件数
    evidence:
      uses: 3              # 使用ログ由来（assistant 発話マッチ）
      last_used_at: 2026-08-30 # 90日自然風化の判定源（使用ログは30日 prune のため台帳が持つ）
      explicit_mentions: 1 # 事後の明示的言及（主たる正）
      negative_signals: 0
    constraints:
      max_per_session: 1
    history:
      - {at: 2026-08-16, from: candidate, to: staged, by: applier, proposal_id: "sha256:..."}
```

## 2. phrase スロット制約（deny 文法）

applier は提案 phrase を以下で検証し、1件でも該当すれば**その phrase を reject**（採用パイプは他 phrase で続行可）:

| 規則 | 定義 |
|---|---|
| 長さ | > 24字（コードポイント数）reject。**長さ判定は保存形に対して行う**: 保存形 = NFC 正規化後、あらゆる Unicode 空白（タブ・全角スペース等）の連なりを U+0020 単一へ置換し前後の空白を除去した文字列（実装 = `canonicalize_for_storage`・#26 追認）。改行含みは本規則とは別に**原文段階の独立 reject 事由** |
| 命令形 | 末尾が命令・依頼形（〜しろ / 〜せよ / 〜して / 〜してください / 〜すること）reject |
| 二人称 | 二人称主語で始まる（あなた / お前 / 君 + は・が・も）reject |
| 否定命令 | 〜するな / 〜しないで / 〜禁止 reject |
| 権限・ツール・承認語彙 | 承認 / 許可 / 権限 / 実行 / 削除 / sudo / rm / push / merge / commit / token / password / key / secret / パスワード / 秘密 を含む reject |
| URL・パス様 | `://` / `/Users/` / `~/` / `\\` を含む reject |
| コード様 | バッククォート / `{}` / `;` / `$(` を含む reject |
| 数字列 | 5桁以上の連続数字を含む reject |

- 語彙リストは guard lint の定数として保持し、**追加は自由・削除は S/M quorum レビュー必須**（緩和は一方向に難しく）。
  **削除・緩和はさらに council + オーナー承認を要する**（governance-R12a — harness 公開版 `docs/governance-rules.md`
  guardrail-loosening 条項。CP-2 整合 2026-08-02。緩和か強化か争いがある場合は緩和扱い）。
- **guard lint 仕様**: 上記全規則の判定器。実装は applier 内蔵 + リポ CI の両方で同一実装を使う。
  **test vectors 必須**: accept ≥5 / 各 reject 規則につき ≥1（実装 Issue pgl#6 の受け入れ条件）。
- 残余リスク: 字面規則で意味空間は閉じない（architecture-v1.md §7）。異種モデル diff レビュー（§5.2）が意味層の網。

## 3. writer 資格情報モデル（三択の確定）

**採用: 仲介スクリプト方式（path-scoped 決定論 applier）**。

| 候補 | 判定 | 理由 |
|---|---|---|
| 別リポ化 | 却下 | SPEC §2.2: install root では `catalog_refs` が正規化解決後に `pack/catalogs/` 配下の実ファイルを要求する（`..`・絶対パス・symlink 脱出は `E_CATALOG_REF`）。overlay home の `persona-engine/` は staging の `pack/` へ同期するため、overlay だけをさらに別リポへ分けても copy step の write 資格へ境界が移るだけで isolation は増えない。面ごとのリポ増設は家族展開の一般化コストも高い |
| deploy key | 却下 | remote push のスコープ制御。書き込みはホスト内ローカル（SPEC 不変条件5）であり、本アーキの脅威（writer プロセスの越権書き込み）に効かない |
| **仲介スクリプト** | **採用** | 決定論 applier が唯一の書き込み経路。path allowlist は**コード内定数**（config で拡張不可）。面ごとに allowlist プロファイルを持ち家族展開が一般化する |

規定:

1. **writer（LLM）**は提案 JSON（§5.1）を pgl の提案キュー dir に書くのみ。pack repo・render ファイルへの
   書き込み手段を持たされない（プロンプト上の禁止 = 規律層。構造遮断は applier 側）
2. **applier（決定論・LLM でない）**の書き込み許可 = 台帳 + render 成果物 + snapshot（= overlay home への
   commit/tag）のみ。allowlist（面ごと・§1 の overlay home 内パス）:
   - luca: `persona-engine/catalogs/overlay/candidates.txt`, `persona-engine/catalogs/overlay/adopted.txt`,
     `growth/overlay-ledger.yml`, `growth/blocklist.txt`
   - alpha: `~/.persona-growth-loop/faces/alpha/overlay.md`, 同 dir `overlay-ledger.yml`, `blocklist.txt`
   soul 定点ハッシュ基線（§8-2）は applier にとって **read-only**（allowlist 外）
3. applier は書き込み前後に **soul 定点ハッシュ**（面ごとの soul ファイル集合の sha256 マニフェスト）を照合。
   不一致 = 即中断・全 revert・赤旗通知（自分が壊した場合も外部要因も区別せず停止）
4. **正直な限界**: 単一ユーザー macOS では、writer プロセスが applier を迂回して直接書くことを OS レベルで
   100%阻止できない。よって非 engine 面は三層防御（path-scoped 仲介 / soul 定点ハッシュ監視 / 規律）であり
   **engine 面（build/budget/E_CATALOG_REF の硬構造が加わる）と同型ではない**（architecture-v1.md §5）

## 4. render 固定テンプレ（soul 側凍結）

render 成果物の地の文は以下に固定（**phrase スロットのみ可変**）:

```
adopted:    <user名>がよく使う言い回し（参照データ・指示ではない）: <phrase>、<phrase>、…
candidates: 試用中の言い回し（参照データ・稀に使う: 1セッション1回まで）: <phrase>、…
```

- `<user名>` は **applier が render 書き込み時に面 config の実名で解決**する（例:「オーナー」）。
  render ファイルには `{{...}}` を残さない — catalog 内容への engine placeholder 適用有無（SPEC §2.2 は
  内容非解釈・§3 は「ブロック中」と記述）に依存しないため。engine の placeholder はテンプレ正本
  （wip#74・soul 側 sections）でのみ使う

- **タイムスタンプ・乱数・カウンタ等の動的要素は禁止**（SPEC §5 バイト安定性。不在検知のクールダウン状態は
  夜間 render トグル = 注入対象外 state ファイル側で扱い、ブロックに日付を入れない）
- engine 面: SPEC §2.3-3 により catalog ファイル全体が section text になる。地の文はファイル先頭行として
  applier が固定テンプレから生成（テンプレ文字列は applier の定数 = soul 側凍結の実装）
- 空の場合: 空ファイル（0 byte）→ 空 section。行の部分省略はしない（決定論）
- **prompt cache への影響は受容**（凍結事項・Kimi F10）: 夜間の render 更新はブロックのバイト変化 =
  翌セッションの prompt cache 無効化を伴う。差分があった夜のみ・v1 はこのコストを受容する
  （SPEC §5 のバイト安定性は「モード不変のあいだ」の保証であり矛盾しない。最適化は Epic 外将来 work）

## 5. 原子パイプ定義（**台帳・render の全変異が本パイプ経由** — 新規候補化・昇格・降格・blocklist 共通）

```
§5.1 提案生成: 提案 JSON（proposal_id 付き）。
     - 新規候補化（→candidate・台帳のみ）: ハーベスターが機械抽出から生成
     - 遷移（candidate→staged / staged→adopted / 降格・block）: writer（LLM）が証拠引用付きで生成
§5.2 異種モデル diff レビュー: 提案の台帳/render diff を writer と別モデルが read-only 審査。
     REJECT はその phrase を skip（ブロック権あり・会話は止めない）。
     **candidate→staged（= 初回 render 掲載）は本ステップの必須通過点** — 新規収穫 phrase が
     意味層レビューなしに注入へ到達する経路は存在しない
§5.3 applier（luca 面では g0・a・a2・a3 が gate 層 = **毎夜・差分の有無に関わらず**実行し、
     b 以降は差分がある夜のみ — 設計 = docs/luca-lane-v1.md §3.1。alpha 面は従来どおり a–a2）:
     g0. luca 面のみ・gate 層（実装位置 = nightly の check_all と同じ gate 層。前段として
         overlay home clone を `git pull --ff-only`（失敗 = [RED] 停止 — 設計 §3.3））:
         reconciliation ゲート — deploy dispatcher `hash` の本番 digest を
         台帳 snapshots[-1].content_hash と照合（比較の正本 = `$PGL_HOME/state/luca-prod-anchor.json`・
         台帳 snapshots[-1] は e2 経路の同値記録 — 設計 §2.1）。不一致 = 当夜停止 + [RED]
         （自動で追いつかない）。UNAVAILABLE（ssh 不達・読取失敗）も同じく停止
         （「不一致ではない」と読まない — fail-closed。設計 = docs/luca-lane-v1.md §3.1・
         レビュー Opus F21/GLM MAJOR1）
     a. killswitch チェック（§10）→ ON なら中断（§5.5 deletion 区分の手動操作、および
        **luca 面の `pgl-rollback` 搬送段（§5.5 luca 搬送段条項 — 緊急復旧の脱出路）**を除く —
        block / forget は安全側操作として ON 中も実行可・eject は `mode: eject` が前提。§10）
     a2. 鏡 liveness チェック → 鏡の直近週次レポートが **14日** より古ければ中断+通知
         （governance-R12b 条件6の standing 検証。定数14日=オーナー確認済み 2026-08-02・CP-2 整合）。
         適用範囲 = admission 方向（§5.5）のみ。a2 の目的は「観測・検証が死んだまま自動系が
         学習を進めること」の防止であり、注入量を減らす方向の手動操作（block / forget / eject）
         には適用しない。鏡失効下で deletion を実行した場合は §5.5 の監査担保に従い、
         [RED] 通知 + commit トレーラ `Mirror-Liveness: stale(<age>d|missing)` を必ず残し、
         次回の週次鏡レポートに当該実行を明示する（改定 2026-08-04・オーナー決裁 G2）
     a3. luca 面のみ: parity ゲート — 直近週次鏡 marker の `parity == GREEN` を必須とする
         （marker の parity フィールド = docs/mirror-v1.md）。**RED も UNAVAILABLE も admission 停止**
         （fail-closed）。適用範囲は a2 と同じく admission 方向のみ（deletion には課さない）。
         alpha 面の a2 semantics は変えない — ルカ面の追加ゲートとして新設。乖離検知の主経路は
         毎夜の g0 であり、週次 parity は背後検知（設計 = docs/luca-lane-v1.md §2.1・
         4席独立収束: Grok C3/GLM MAJOR1/Fable M3/Opus F5・新設 #47）
     b. lock 取得（§9）・対象リポ dirty チェック（§9）
     c. guard lint（§2）・cap 検証（§12）・soul ハッシュ照合（§3-3）
     d. 台帳更新 + render 書き込み
     e. engine 面のみ: **staging install root（§1・git 外）で** `persona build` → doctor/hash 検証
        （build content_hash 取得）。build 前に overlay home の `persona-engine/` を staging の
        `pack/` へ rsync 同期する。生成物（`build/`）は git に commit しない（§11 snapshot の
        対象は overlay home のみ — 設計 = docs/luca-lane-v1.md §3.3・改定 #47）
     e2. luca 面のみ: **本番搬送**（deploy dispatcher 経由・順序固定 — 設計 =
         docs/luca-lane-v1.md §3.1/§3.2）:
           killswitch 再チェック（e2 突入直前 — §10 読み手行・レビュー Opus F20）
         → 意図ジャーナル「deploy 開始」記録（`$PGL_HOME/state/`・fsync）
         → backup（本番 install.yml + pack + build → `~/.hermes/backups/`・rotation）
         → install.yml 等価検査（staging と本番の placeholder 宣言集合 diff ゼロ — レビュー Fable m8。
           期待 placeholder 値は面 config 由来〔`display_name`〕+ コード内 persona 名 — 凍結リテラルの
           改竄検知ピンではない〔公開版 2026-08-13 の脱実名化トレード・検査自体は exact-match fail-closed のまま〕）
         → rsync（staging pack → 本番 pack・dispatcher 固定宛先）
         → out-of-place build（本番側 temp dir で build + doctor → 版ゲート → content_hash equality
           〔staging と等値・bare ">0" 禁止〕→ **全ゲート通過後に rename で build/ へ昇格**。
           temp dir は build/ と**同一 filesystem に co-locate**〔rename の原子性保証 — delta r2 GLM〕。
           失敗時は本番 build/ に未検証バイトを残さない — レビュー Fable C1）
         → killswitch 再チェック（restart 直前）
         → restart（両 unit）
         → 実ターン受け入れ（voice/api_server 固定。**発話送信前に意図ジャーナルへ
           acceptance-window open を fsync 記録**〔open 無しの送信は禁止〕→ accept〔dispatcher〕→
           accept 応答の session UUID 群を受け入れ台帳へ追記〔**追記失敗 = 受け入れ FAIL**〕→
           完了時に acceptance-window close 記録。受け入れ実体 = warm-up → 事前 mode 記録 →
           切替検証〔block_bytes/block_sha256 の manifest 等値〕→ **事前 mode へ復元**。
           curl --max-time 必須・直近 N 分に private domain の活動があれば当夜 skip
           〔fail-closed hold — レビュー Grok M3。**skip は成功ではない**: 処置は附則A 行5 を準用
           （無条件 backup 復元 + 再 restart + 旧受け入れ）・f 以降は実行しない・digest に
           skip 理由1行（§7・[RED] ではなく skip 行）— 処置定義は #47 delta〕）
         → 意図ジャーナル「受け入れ成功」記録
     f. 成功時のみ commit + tag（snapshot: source SHA + build content_hash 対。§11）。
        **luca 面では f は e2 の受け入れ成功の後に置く（f の後置 — commit は本番検証済み状態のみを
        記録する）**。luca 面は加えて意図ジャーナル「commit 完了」記録 → origin へ ff-only push。
        push 失敗 = [RED] + 手動解決（**push は §5.4 の revert 対象外** — 本番と local commit は
        一致しており戻さない — レビュー Opus F22）
     g. 採用通知（オーナーダイジェスト行 + 台帳 history 追記）
§5.4 いずれかの失敗（**luca 面では e2 の各段の失敗を含む**）= 全 revert + レーン停止
     （当夜の以降の適用を中止）+ 通知。沈黙禁止。**受け入れの当夜 skip（活動検知 hold）は
     成功でも失敗でもない第3の終端だが、処置は失敗と同扱い**（附則A 行5 準用・f 非実行・
     digest は [RED] でなく skip 理由行）。
     luca 面の e2 失敗の扱い: **backup 取得後の e2 のいかなる失敗も = working tree 全 revert +
     staging build 再生成 + 無条件で本番 backup 復元**（restart 前の失敗でも復元する — #82 実績
     「step4 以降は全面復元」に一致・レビュー GLM MAJOR6/Grok M1）**+ 復元後の受け入れ再実行
     （旧 hash 等値確認）+ レーン停止 + [RED]**。復元自体の失敗 = [RED] + レーン停止 +
     有人エスカレーション（二次失敗も沈黙禁止）。push は上記のとおり revert 対象外。
     失敗点ごとの規定の正本 = **附則A 失敗マトリクス（閉集合）**
```

- 提案 JSON: `{proposal_id, face, phrase_id, transition: candidate→staged 等, evidence 引用, generated_at}`。
  `proposal_id = sha256(face + phrase_id + transition + date)`。
- **idempotency**: applier は台帳 history に同 `proposal_id` があれば no-op で成功終了（再実行安全）。
- **idempotency の拡張（deletion の変異ゼロ終了・#26 項目5・オーナー明示承認 2026-08-09）**:
  deletion 操作が**変異ゼロで終了**する場合（既に blocked 済み phrase への `pgl-block` 等の冪等再実行）は、
  事前検査（lock〔§9〕・CP ゲート〔check_all〕・soul 照合〔§3-3〕）通過後、**§5.3 d–f（変異・build・
  commit・tag）は発生せず、g のうち §7 の digest 通知1行のみを必須**とする（**台帳 history 追記は
  生じない**・§12 cap 検証も発生しない。d–f/g の区分は §5.3 の定義に従う — history 追記は g の構成要素）。
  本項は §5.5「d–g は必ず通過」の適用範囲を**「変異が生じる実行」に限定する解釈の固定**である。
  **台帳に不在の phrase_id の指定は no-op ではなく操作失敗**（[RED]・非0 exit — 診断品質の改善は #54）。
- 例外: staged phrase の**日次 render トグル**（evidence-rules §3 holdout・不在検知クールダウン）は
  遷移を伴わない決定論 re-render であり §5.1/§5.2 を要しない（§5.3 の **g0**–f は全て通る —
  g0 挿入に伴う範囲追従 #47 delta）。

### 5.5 ゲートの二分（admission / deletion）（新設 2026-08-04・オーナー決裁 G1 = A+B 複合）

overlay の書き込み経路は、注入量に対する**方向**で二分し、要求するゲートを分ける。

| 区分 | 経路 | 要求ゲート |
|---|---|---|
| **admission**（注入を開始・増加・維持する） | 夜間パイプ（§5.1–5.3 全体。**夜間サイクル内の自動 即降格 + blocklist〔§15〕を含む** — 下記注記）・`pgl-rollback`（復元 = 注入の再導入。**luca 面の搬送段実行様式と緊急復旧時の実行可否は本節末尾の luca 搬送段条項・§11 が正** — 改定 #47） | §14 CP ゲート（CP-2 発効 + 面 CP-3 GO + governance pointer）+ §10 killswitch + §5.3 a2 鏡 liveness + a–g 全部。**2026-08-04 改定は admission 側のゲートを 1 bit も緩めない**（同改定の前後で admission の要求ゲートは不変。**#47 は luca 面 `pgl-rollback` の搬送段に限り、本節末尾の luca 搬送段条項の明示例外〔killswitch ON 中・CP 検証不能下の有人実行 — 凍結設計 v1.2 §3.4〕を追加する — それ以外の admission 経路の要求ゲートは不変**） |
| **deletion**（注入を減らす・ゼロにする） | **手動の** `pgl-block`・`pgl-forget`・`pgl-eject` の**列挙 3 操作のみ**（deletion 区分を名乗れる操作はこの列挙が**閉集合**であり、追加は本契約の改定手続きを要する。§15 の**手動**削除経路はここに属す） | §14 CP ゲート（発効**後**の実行ゲート — 発効**前**は §14 の全面禁止が deletion にも及ぶ）および §5.3 a2 の**対象外**。§5.3 b（lock・dirty チェック）・c のうち soul ハッシュ照合と §12 cap 検証・d–g（原子 commit・失敗時 revert・通知）は**必ず通過**（guard lint〔§2〕は admission 側の内容ゲート — 残存内容が lint 不合格でも削除を塞がない。**変異ゼロで終了する冪等再実行は §5 の idempotency 拡張条項に従う** — #26 項目5）。§10 killswitch の扱いは §10 の規定どおり（block / forget = 安全側操作として ON 中も実行可・eject = `mode: eject` の明示設定が前提） |

- `pgl-baseline` は overlay への書き込み経路ではなく soul 定点ハッシュ基線の更新手段（§8-2・applier
  allowlist 外 = read-only）であり、**本条の二分の対象外**。要求ゲートは §8-2 と現行実装どおり
  （CP + killswitch。鏡 liveness は課さない — 基線修復まで鏡失効で塞ぐと soul 基線不一致 × 鏡失効の
  複合で削除3操作の soul ハッシュ照合まで連鎖停止する新 wedge を作るため）
- 夜間サイクル内の**自動** 即降格 + blocklist（§15 の自動経路）は夜間パイプ = **admission 区分**に
  属し、CP 検証不能時は当夜停止する。CP 失効時に夜間の自動降格も止まる問題は council F-3 として
  **v1 では見送り**（複雑度に見合わない・手動操作で代替可能）・将来課題の記録のみ。
  その間の即時代替 = 手動 `pgl-block`
- **単調非増加条件（deletion の定義・必須の補償統制 — council Opus F-B）**: deletion 区分の操作は、
  完了後の台帳 candidate / staged / adopted の**各集合**と render 内容（比較単位 = 各 render ファイルの
  **phrase エントリ集合**。テンプレ地の文〔§4〕は対象外）が、実行前の同集合・同内容の**部分集合**であり、
  かつ blocklist（§15）が実行前の**上位集合**（blocklist の縮小 = 再候補化の解禁 = 注入方向のため
  deletion では不可）であり、かつ操作対象 phrase が不在であることを **applier が実行時
  （§5.3 f の commit 確定前）に検証する**。違反 = 中断 + 全 revert + [RED] 通知（§7）。
  この検証を欠く操作は deletion 区分を名乗れない。
  背景: block / forget は差分削除ではなく**台帳全体からの再 render**であるため、CP ゲートを単純に
  外すだけだと「台帳に phrase を注入 → 無関係 id を block → 注入入り render を未検証のまま commit」
  という**増加の抜け道**が開く。単調非増加検査はこの経路を名指しで塞ぐ（実行前に render に無かった
  内容が実行後の render に 1 件でも現れれば違反 = 中断 + 全 revert）。
- **台帳読取不能時の縮退（eject のみ — council F-A）**: 台帳が破損・読取不能な状態での `pgl-eject` は、
  台帳集合の検証を **render 内容の部分集合検証に縮退**して実行する（eject は render 空化 = 空集合は
  任意集合の部分集合として自明に成立）。縮退実行は `Monotonicity: unverifiable-ledger` を [RED] +
  commit トレーラで記録する — **検証不能を理由に eject を塞ぎ直さない**（eject は台帳非依存の脱出路）。
  `pgl-block`・`pgl-forget` は台帳必須（§15）のため本縮退の対象外
- **監査の担保（ゲート検証不能下での実行）**: deletion を CP 検証不能状態（§14）または鏡失効下
  （§5.3 a2）で実行した場合、ダイジェストに [RED] 1行（操作・面・理由コード）、commit メッセージに
  `Gates-State: unverified(<reason>)` / `Mirror-Liveness: stale(<age>d|missing)` /
  （台帳読取不能時の eject 縮退では）`Monotonicity: unverifiable-ledger` トレーラ、
  台帳 history（存在する場合）に同旨のメタを**必ず残す**。ゲートで止める代わりに、記録で追跡する
  （§7 沈黙禁止の適用）。
- **luca 面の搬送段（deletion・rollback の2ホスト化 — 設計 = docs/luca-lane-v1.md §3.4・改定 #47）**:
  luca 面では、deletion 3操作（`pgl-block`・`pgl-forget`・`pgl-eject`）と `pgl-rollback` は、
  **操作の一部として本番搬送段（§5.3 e2 相当・注入を減らす方向）を実行して完結する**
  （repo だけを変えて本番に届かない状態を正常終了としない — repo 先行の乖離が g0 reconciliation を
  恒久 RED にし「安全側操作をした罰で自動化が止まる」構造を作らないため）。
  - killswitch ON 中・CP 検証不能下でも実行可（§10 の「eject は d–g を完全通過」の自然延長）。
    deletion・rollback は手動操作であり実行者が居るため、**killswitch ON 中の本番 restart は
    無人ではなく有人トリガーで行う**（無人化はしない）。
  - 搬送失敗時は**操作自体を失敗**として [RED]（repo 先行のまま放置しない — 失敗時は repo side も
    revert。附則A の失敗規定を準用）。
  - **本搬送段の実行に際し §5.3 g0・a3 は課さない**（両者は夜間 admission パイプのゲートであり、
    乖離・parity RED という状態そのものが deletion・rollback の実行理由になるため — 塞ぐと
    「安全側操作をした罰で自動化が止まる」構造が戻る。搬送後の状態は単調非増加検証〔deletion〕・
    受け入れ検証・次回 g0 で担保する — #47 delta・Opus M3）。**§10 の in-flight 中断（ON 検知 =
    即中断）は自動夜間ジョブに適用するもの**であり、有人トリガーの本搬送段には適用しない
    （killswitch ON 中の実行可否は本条どおり — #47 delta・GLM MINOR-4）。
  - 単調非増加検証（本条上記）の**本番側適用の対象は deletion 3操作の搬送のみ**（設計 §3.2 の
    scoping が正: 「deletion 搬送の受け入れには §5.5 単調非増加検証の本番側適用を含める」。
    受け入れ時に render ファイル集合の部分集合検証を本番側にも行う — delta r2 Opus N3。
    **rollback は admission 区分のため対象外** — 復元は注入の再導入でありうるため部分集合検証は
    構造的に適用不能。rollback の正しさは §11 tag semantics〔本番実ターン検証済み状態のみが tag〕と
    受け入れ検証で担保する — #47 delta・3席収束）。
  - **変異ゼロで終了する冪等再実行（§5 idempotency 拡張条項）では搬送段も発生しない**
    （d–f が発生しない実行に e2 相当段は含まれない。本番一致の確認は次回 g0 が行う —
    #47 delta・Opus M6）。
  - 注記: `pgl-rollback` の区分表上の位置（admission = 復元は注入の再導入）は変えない。本項が
    定めるのは luca 面の**搬送段の実行様式**であり、rollback の killswitch ON 中・CP 検証不能下の
    実行可は凍結設計 v1.2 §3.4 の決定に従う（緊急復旧の脱出路 — R13 は §11 で luca 再定義）。

## 6. bootstrap 契約

- overlay render ファイルと台帳は**空で先に設置**する（candidates.txt / adopted.txt = 0 byte、
  台帳 = `schema_version: 1` + 空 phrases）。pack の `catalog_refs` は空ファイルを参照しても
  build が通ること（= 空 section）を回帰テストで保証（wip#76 の受け入れ条件）
- **「削除せず空にする」**: overlay を無効化する操作は常に「ファイルを空にする」であり、ファイル削除・
  `catalog_refs` 除去はしない（削除は `E_CATALOG_REF` で build を壊す = 事故経路）

## 7. 通知（沈黙禁止の実装）

- 採用/降格/中断/killswitch 検知/soul ハッシュ不一致は、当夜のダイジェスト（既存のオーナー向け通知経路）へ必ず1行以上出す
- soul ハッシュ不一致に限り、ダイジェストに加えて `soul_alert_argv` 経由でオーナーへエスカレーション配送する。
  配送失敗・未設定は `[WARN]` としてダイジェストへ残し、パイプの成否を変えない。
- 「実行されなかった夜」（lock 取得失敗・dirty skip 等）もダイジェストに skip 理由を出す — 静かな停止を作らない

## 8. 非 engine 面の三層防御（Alpha 面の実装規定）

1. **path-scoped 仲介**: §3 の applier のみが `~/.persona-growth-loop/faces/alpha/overlay.md` を書く。
   CLAUDE.md 本体には render ファイルへの参照1行のみを人間（オーナー/Alpha 手動）が一度だけ設置する
2. **soul 定点ハッシュ監視**: 面ごとのコード所有 soul file set を sha256 マニフェストとして基線化する。
   Alpha は CLAUDE.md 全体でなく、§1 の人格核3節と overlay 参照行を宣言順に連結した節単位抽出
   （`alpha-soul-v1`）を照合する。改行コードは CRLF から LF への正規化のみとし、空白は正規化しない。
   各節は対象見出しから、次のレベル1〜3 ATX 見出し（`#{1,3}` + 空白または行末）の直前までとし、
   レベル4以下は終端にしない。フラグメントは宣言順に `\n` 連結し、節本文が空なら fail-closed とする。
   基線 manifest の `coverage` には各節の行数・バイト数と参照行数を情報として記録する。
   Luca と `extract` 識別子のない旧マニフェストは従来どおりファイル全体を照合する。不明な識別子は
   fail-closed とする。**基線の置き場 = `~/.persona-growth-loop/soul-baseline/<face>.manifest`**
   `extract` 識別子のない既存（旧形式）manifest は再基線までファイル全体照合のままであり、節単位監視は
   オーナーが `bin/pgl-baseline` を明示実行した時点で発効する。
   （applier の allowlist 外 = applier からは read-only）。applier 実行時（§3-3）+ 鏡週次（pgl#7）で照合。
   基線の更新はオーナー/Alpha の手動編集後に明示コマンド `bin/pgl-baseline <face>` でのみ
   行う。soul mismatch は `[RED]` で停止し、`soul_alert_argv` 経由でオーナーへ配送する。配送失敗・未設定は
   パイプを止めず、ダイジェストへ `[WARN]` を残す。
3. **規律**: R12a（flh#123・発効 = CP-2）。Alpha の CLAUDE.md 直接編集権は本 Epic では変えない
   （R12a の規律対象であることを明文化）

- **writer 強制 hard cap**: §12 の cap を applier が enforcement（超過提案は書き込み拒否）。
  **tripwire は二重チェック**: applier とは別系統の夜間サイズ監視が render ファイル実サイズを検査し、
  cap 超過を検知したら赤旗通知 + killswitch 提案（自動 ON はしない — 検知系に書き込み権を与えない）

## 9. single-writer・lock・競合回避

- **single-writer**（sgl R1 相当）: pgl のコレクタ・夜間パイプライン・手動 applier 操作は面別の mkdir lock
  （`~/.persona-growth-loop/lock-<face>.d`）で同一面の単一インスタンスを保証する。alpha と luca は互いを
  不要に停止させない。取得失敗 = 当該面を当夜 skip + digest へ `[RED]` 1行 + 非0 exit（沈黙禁止・§7）。
  lock dir は 0700、`owner.json` は 0600・no-follow 作成とし、既存の owner 来歴・24時間 stale 判定・
  手動 clear 案内の規律を面別 path にそのまま適用する
- **夜間 writer × 人間 worktree の競合回避**（L0-1 WIP・R5 準用）:
  - overlay の書き込みパス（§3-2 allowlist）は **applier の常設 WIP 宣言**として各対象リポ README に記載
  - applier は対象リポの working tree が対象パス上で dirty なら**中止 + 通知**（上書きしない）
  - 人間が overlay パスを触る作業をする時は killswitch ON（§10）または Issue で HOLD 宣言してから
    （CP-2 発効前は手動含め全面禁止 — §14）

## 10. killswitch 実体（迎合キルスイッチ）

| 項目 | 規定 |
|---|---|
| 実体 | `~/.persona-growth-loop/KILLSWITCH`（ファイル存在 = ON。内容 YAML: `set_by / set_at / reason / mode`） |
| mode | `freeze`（既定）= 成長系全停止・現 adopted の注入は継続 / `eject` = 加えて render を空にする applier 1回のみ許可（注入量を減らす方向だけの例外）。**eject 実行は §5.3 の b・c のうち soul ハッシュ照合と §12 cap 検証・d–g を完全通過する**（lock・soul ハッシュ照合・engine 面は build+検証・commit・失敗時 revert — killswitch を soul ハッシュ照合や build 検証の迂回路にしない。通過を要さないのは §5.1/§5.2 の証拠・レビューゲート・§5.3 a2 の鏡 liveness〔a2 は admission 方向のゲート — §5.5。改定 2026-08-04〕・guard lint〔§5.5 表の deletion 行どおり〕。鏡失効中の実行は [RED] + commit トレーラで記録する〔§5.5 監査担保〕。eject 自体は `mode: eject` の明示設定という人間の行為を前提とするため、無人での自動実行経路は存在しない） |
| 削除系手動操作 | `pgl-block`・`pgl-forget` は注入量を減らす安全側操作として **killswitch ON 中も実行可**（§5.5 deletion・§5.3 a の中断対象外。従前は本表に明記が無かった運用の条文化 — 改定 2026-08-04。コード注記の contract-clarification candidate を本改定で解消）。**luca 面ではこれらの操作は本番搬送段を含めて完結し、killswitch ON 中の本番 restart は有人トリガーで行う**（§5.5 luca 搬送段条項 — 改定 #47）。**luca 面の `pgl-rollback` の搬送段も同条項により killswitch ON 中の有人実行可**（緊急復旧の脱出路 — #47 delta） |
| 読み手 | 書き込み系ジョブ全部（ハーベスター・集計・writer・applier）が起動時 + 各書き込みフェーズ直前 + commit 直前 + **本番搬送（§5.3 e2）の各段直前（少なくとも e2 突入直前と restart 直前 — レビュー Opus F20・改定 #47）**に再チェック。観測コレクタと鏡は継続（検知系は止めない） |
| in-flight | チェック時点で ON を検知した夜間ジョブは即中断・全 revert・通知 |
| 設定 | 誰でも可（オーナー・Alpha・鏡の赤旗を見た家族の誰でも。安全側に倒す操作のため） |
| **解除** | **オーナーのみ**（ファイル削除）。鏡週次レポートに killswitch 状態変化（mtime/hash）を記録し解除の追跡可能性を担保 |

## 11. snapshot + rollback（R13 定量）

- **snapshot = overlay home（§1）への git commit 実体 + tag** `overlay-snap-<face>-YYYYMMDD-N`。
  2面が同一 repo に同居するため1つの tag が両面を覆い、**内容は git から完全復元可能**。
  luca の tag が覆うのは applier 専用 clone [overlay home] の `persona-engine/...` + `growth/...` であり、
  git 外の staging install root（`pack/`・`build/`）や VPS 本番 build は tag へ含めない。
  台帳 history に記録する **source SHA + build content_hash の対**（非 engine 面は render ファイル sha256）は
  復元後の**検証用**（hash 自体は復元手段ではない — 復元手段は commit 実体）。
  差分があった日だけ生成される（差分なし = snapshot なし）
- **tag semantics（luca 面・改定 #47）**: luca 面の tag は**「本番で実ターン検証済みの状態」を指す**
  （§5.3 の f 後置により、tag は e2 の受け入れ成功後にのみ打たれる — レビュー Opus F28）。
  alpha 面の tag semantics（ローカル repo の snapshot）は従来どおり
- **rollback**: `bin/pgl-rollback <face> <tag>` の **1コマンド**で、overlay home 内の2面パスを
  `git checkout <tag> -- <2面パス>` で復元 → engine 面は `persona build` 再実行 + content_hash 照合
  （非 engine 面は render sha256 照合）→ commit。**所要 ≤5分**（R13 の M = 5）。
  **luca 面では rollback は本番搬送段（§5.5 luca 搬送段条項 — restore/deploy → 受け入れ）まで含めて
  1操作であり、R13 の所要は「本番の注入が戻るまで」で再定義し、ドリルで実測する**
  （設計 = docs/luca-lane-v1.md §3.4・改定 #47。実測記録の様式は下表のまま — **ただし表の
  「≤5分」は alpha 面の M であり、luca 面の M は本ドリルの実測値をもって本契約の改定手続きで
  確定する**〔設計 §3.4 は実測のみを定め M を再規定していない — #47 delta〕）
- **実測検証の様式**（CP-3a の前提・記録先 = `docs/records/rollback-drills.md`）:

  | 項目 | 内容 |
  |---|---|
  | 日付 / 実行者 | |
  | コマンド全文 | 1行であること自体が検証項目 |
  | 所要時間 | 実測。≤5分 |
  | 戻し先 tag / before・after の content_hash | 対で記載 |
  | 検証者 | 実行者と別（Alpha 実行ならオーナーか別席が hash を突合） |

- **挙動的非可逆性の正直な記載**: rollback は overlay の**状態**を戻すが、既に交わした会話・相手の記憶に
  与えた影響は戻せない。だからこそ入口側（遅延証拠・稀少試用・deny 文法）を厚くしている

## 12. 予算・cap 表（実測数値入り・凍結）

計数器 = `pe-count-v1`（ceil(UTF-8 バイト長 / 3)・SPEC §4.1）。

| 対象 | 実測/上限 | 備考 |
|---|---|---|
| Warmth Core v0（B〜E 共通コア） | **実測 3,611 B = 1,204 pe-tokens** | 既定予算 600 / starter 400 を**超過** → warmth-core を載せる mode は `budget_tokens` の引き上げ宣言が必須（黙った切り詰めは SPEC が禁止 = `E_BUDGET_EXCEEDED` で止まるのが正） |
| ルカ vocabulary.yml | 実測 22.7 KB ≈ **7,579 pe-tokens** | v1→v2 移行（wip#75）の予算設計の前提値 |
| overlay adopted（面ごと） | **≤ 1,800 B（600 pe-tokens）かつ ≤ 40 entries** | applier enforcement + tripwire 二重チェック |
| overlay candidates（面ごと） | **≤ 720 B（240 pe-tokens）かつ ≤ 12 entries** | 同上。**hard cap**（運用上は同時試用 ≤3 × 日次トグル〔evidence-rules §2/§3〕により通常 ≤3 entries — cap は異常検知の天井） |
| phrase 1件 | ≤ 24字（§2） | |
| 1夜あたり | 新規 candidates ≤ 3 / staged→adopted 昇格 ≤ 2 / **降格・blocklist は無制限**（安全側は絞らない） | |

- 予算宣言例 `budget_tokens: 2400`（Core 1,204 + overlay 840 + 余白 356）は **warmth-core のみを載せる mode の例**。
  ルカのように vocabulary.yml（≈7,579 pe-tokens）等を併載する mode はこの例に該当せず、最終予算は wip#75 で設計。
  install 側最終予算（SPEC §4.1: 有効予算 = min(install, mode)）との整合は各面の Issue で検証
- cap の変更は本契約の改定手続き（S/M quorum）を要する。**引き下げは即日可・引き上げはレビュー必須**（非対称）。
- cap 検証・tripwire の計数対象は **render 成果物の実 UTF-8 バイト**（バケット帰属は面別の実装定数 —
  engine 面 = render ファイル別・非 engine 面 = overlay.md の行頭 prefix 判定で、テンプレ地の文は
  adopted 側に算入される — 実態の明文化 #26 2026-08-09）。
  **引き上げはさらに council + オーナー承認を要する**（governance-R12a guardrail-loosening 条項・CP-2 整合 2026-08-02。
  緩和か強化か争いがある場合は緩和扱い）

## 13. schema version と移行規則

- 台帳 `schema_version` は int。applier は**既知 version のみ処理**し、未知 version は fail-closed
  （停止 + 通知。書き込み・「読めた分だけ処理」をしない）
- version bump の要件: 移行スクリプト + dry-run 差分提示 + 移行前 snapshot + S/M quorum レビュー
- render 成果物には version を埋めない（SPEC §5。version は台帳と commit メッセージが持つ）

## 14. CP-2 発効対象物の定義

- **対象物** = flh `docs/governance-rules.md`（R1–R14 を版付き転記・SYNTHESIS.md の R 番号と名前空間分離）
  の改定 commit（R12a / R12b / 迎合キルスイッチ条文を含む）
- **発効の定義** = 上記 commit + **同一 PR 群での4箇所同期**:
  sgl README / sgl ledger-spec / pgl README / pgl INTEGRATION.md
  + **オーナーの CP-2 決裁を governance-rules.md 版表の v1.1 セル flip commit で記録**
  （record of record = 同版表。そのセル以外何も変えない単一 commit）。**要素の欠落 = 未発効**
- wiki 設計正本（alpha-wiki warmth-logic-persona-design）の同時更新と **flh#26 への改定記録コメント**も
  CP-2 の完了条件（**欠落 = CP-2 未完了 = 未発効**）
- **発効前**: overlay への書き込みは**手動含め**全面禁止（唯一の例外 = §6 の空ファイル bootstrap 設置。
  本契約の applier は CP-2 発効 + CP-3（面ごと GO）まで起動しない。§5.5 の deletion 免除は
  **発効後の実行ゲートのみ**を対象とし、発効前の本全面禁止には及ばない — flh governance の
  Effectiveness marker convention〔例外 = 空ファイル bootstrap のみ〕に契約が追従する）
- **CP-2 発効後・CP-3 未 GO の面**: 手動 deletion（§5.5）は履行可 — CP-3 は注入の**許諾**であり
  除去の許諾ではない（§0.4/§14 の「applier は CP-3 GO まで起動しない」は admission 側の夜間 applier
  起動の規定であり、手動削除3操作を塞ぐものではない。改定 2026-08-04）
- **発効後の CP 検証不能状態**（gates.yml 欠損・破損・CP-3 撤回。改定 2026-08-04・council F-C の最小明文化）:
  admission は fail-closed で**全面停止**（**例外 = luca 面の `pgl-rollback` 搬送段** — §5.5 luca
  搬送段条項・凍結設計 v1.2 §3.4 の緊急復旧脱出路。有人トリガー前提 — #47 delta）。
  deletion 方向（§5.5）は**停止しない** — CP は注入の**許諾**で
  あって、注入の**除去**には許諾を要さない（§10 設定行「安全側に倒す操作」・§12「降格・blocklist は
  無制限」「引き下げは即日可」と同一原則）。実行は §5.5 の監査担保に従って記録する
- sgl の承認フローは不変（flh#123 本文 2026-08-02 追記が条文要件）
- **R10 整合**: overlay レーンは R10 の T0 ADOPT-NOW **ではなく**、新設の明示レーンとして条文化する
  （条文の家 = flh#123 governance 正本。本契約はレーンの技術的中身を凍結する側）

## 15. unlearn 経路

| トリガー | 動作 |
|---|---|
| 明示拒否（「その言い方やめて」等・検出定義 = evidence-rules §4） | 次の夜間サイクルで**即降格 + blocklist**（台帳 state=blocked・render から除去・rebuild）。手動 `bin/pgl-block <face> <phrase-id>` で即時も可 |
| 発話・phrase の削除要求（オーナー・対話相手から） | `bin/pgl-forget <face> <pattern>`: Tier L ログ該当断片 + 台帳 evidence + render の**両層から除去**。実行記録は history に残す（何を消したかのメタは残す・本文は残さない） |

- blocklist（`growth/blocklist.txt`・面ごと）掲載 phrase は再候補化しない（ハーベスターが照合）
- blocklist からの復帰はオーナー承認のみ
- §15 の削除経路のうち**手動操作**（`pgl-block`・`pgl-forget`。§10 の `pgl-eject` も同様）は §5.5 の
  **deletion 区分**に属し、§14 の CP ゲートおよび §5.3 a2 に従属しない。CP 検証不能状態でも
  **手動削除**の履行は妨げられない。**luca 面ではこれらの手動削除は本番搬送段を含めて完結する**
  （§5.5 luca 搬送段条項 — 改定 #47）。一方、明示拒否トリガーの**夜間サイクル内の自動 即降格 + blocklist
  は夜間パイプ（admission 区分・§5.5）のゲートに従い、CP 検証不能時は当夜停止する**（council F-3 =
  v1 では見送り・将来課題の記録のみ。その間の即時代替 = 手動 `pgl-block`）
  （改定 2026-08-04・オーナー決裁 G1/G2）

## 附則A 失敗マトリクス（luca 面・閉集合）

§5.4 の luca 面適用の**正本**（設計 = docs/luca-lane-v1.md §3.5・様式 = レビュー Grok M1・附則化 #47）。
本表は**閉集合**であり、行の追加・変更は本契約の改定手続き（S/M quorum）を要する。
e2 内の killswitch 再チェックで ON を検知した場合は本表の行ではなく §10 in-flight（即中断・全 revert・
通知）に従い、**backup 取得後の検知は加えて §5.4 の無条件復元**を実行する（#47 delta・閉集合の
取りこぼし封じ）。

| # | 失敗点 | 本番の状態 | repo の状態 | 自動動作 | エスカレーション |
|---|---|---|---|---|---|
| 1 | g0 reconciliation 不一致/UNAVAILABLE | 不明 | HEAD | 当夜停止 | [RED]・有人調査（自動で追いつかない） |
| 2 | e2 backup 取得失敗 | 旧のまま | working tree revert | 停止 | [RED] |
| 3 | e2 **install.yml 等価検査〜**rsync〜build〜版ゲート〜equality 失敗（restart 前） | **backup 復元（無条件）**・旧 build 稼働 | 全 revert + staging 再生成 | 復元後に旧 hash 等値確認 | [RED] |
| 4 | restart 失敗 | 復元 + 再 restart 試行 | 全 revert | 失敗継続なら停止 | [RED]・**ルカ停止の可能性を通知に明記** |
| 5 | 受け入れ失敗（mode 復元失敗含む。**受け入れの当夜 skip〔活動検知 hold〕は本行を準用・f 非実行・digest は skip 理由行** — §5.4） | backup 復元 + 再 restart + 旧受け入れ | 全 revert | 停止 | [RED]（skip 準用時は skip 行） |
| 6 | 受け入れ成功 → commit 前クラッシュ | 新（検証済み） | working tree dirty | 翌夜 g0 が**意図ジャーナルで自己クラッシュと判別**→ [RED] 停止 | 既定復旧 = 本番を直近 tag へ戻す（rollback 方向）or 有人で commit 完遂。手順は runbook に固定 |
| 7 | commit 後 push 失敗 | 新 | local 新 / origin 旧 | 停止（revert しない — §5.4 対象外） | [RED]・手動 push |
| 8 | 復元（#3–5）自体の失敗 | 不定 | revert 済み | 停止 | [RED]・有人（二次失敗も沈黙禁止） |
| 9 | backup が失敗 resume で踏み潰される | — | — | **resume で backup 段を再実行しない**（#82 教訓を機械化） | — |
| 10 | 受け入れ失敗・クラッシュ時の台帳/ジャーナル残務 | acceptance-window open のまま | — | close 未記録 = コレクタが窓末尾まで api_server 不採用（**意図ジャーナル窓 = 第一層の遮断**。台帳は第二層。詳細 = docs/luca-lane-v1.md §1.2-5 — ラベル訂正 #47 delta・3席収束） | [RED]・有人確認 = 「台帳回復の要否」（accept 応答を得ていれば追記・得ていなければ窓遮断で十分＝追記不能。回復追記は operator seed 手続き〔`origin:"seed"`・作業記録必須〕で行う） |
| 11 | e2 の backup 前段の失敗（意図ジャーナル「deploy 開始」記録失敗等） | 旧のまま | working tree revert | 停止 | [RED] |
