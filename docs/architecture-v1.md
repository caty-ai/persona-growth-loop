# Warmth Persona Architecture v1 — 確定アーキテクチャ（凍結版）

> Status: **FROZEN**（Epic pgl#3・lane #0 = pgl#4 — 番号は公開前の非公開トラッカーの史料）
> 凍結根拠: L1-9 異種レビュー3席（Kimi K3 / Opus 5 / GLM 5.2）全 CONDITIONAL-GO → 統合裁定
> `reviews/2026-08-02-architecture-v1/10-adjudication.md`（席レビュー原文は非公開 ops リポに保管）反映済み。
> 設計の思想的正本: alpha-wiki `wiki/2026-08-01-warmth-logic-persona-design.md`（v5）。
> 本書と契約3文書（[overlay-contract](contracts/overlay-contract.md) /
> [observation-log-schema](contracts/observation-log-schema.md) /
> [evidence-rules](contracts/evidence-rules.md)）が実装の SoT。食い違いは契約3文書が勝つ。
> **用語注記**: `flh` = fable-loop-harness（[caty-agent-harness](https://github.com/caty-ai/caty-agent-harness) の公開前の非公開系譜）。
> `flh#NN`・`pgl#NN`・`wip#NN` は公開前の非公開トラッカーの決裁史料番号（来歴参照として本文に保持）。
> `wip-persona-engine`（エンジン面の非公開開発リポ）・`alpha-wiki`（非公開設計 wiki）への言及も同じ非公開来歴の参照。

## 1. 目的

人が AI を「賢い・心地いい・好き」と感じる構造（本質理解 × 自己肯定感 × 完遂可視化）を、
**迎合ではなく誠実さ**で実装する。

- 会話の **fast path**（リアルタイム層 = 読むだけ・自己学習しない）と
  学習の **slow path**（夜間系 = 遅延報酬のみで学習）を経路分離+遅延ゲートで分ける
- **soul（その人らしさの核）は凍結**し、成長は **overlay（専用の引き出し）だけ**に書く
- 学習判定は**遅延シグナルのみ**（その場の「ありがとう」では動かない = 迎合マシン化の構造的防止）

## 2. 全体データフロー（凍結）

```
会話（毎ラリー）
 → ①リアルタイム層（プロンプト層: Warmth Core v0 B〜E。read-only by construction）
 → 観測ログ Tier L（per-host・content-block フィルタ・書き出し時 scrub・speaker 付き・30日 prune）
 → ②persona-growth-loop 夜間系: ハーベスター（頻度候補化）→ 遅延証拠集計（holdout+負シグナル）
 → ③writer は「採用提案」生成のみ → 異種モデル diff レビュー → 決定論 applier（path-scoped 資格）が
    台帳更新+render+persona build+検証+commit/tag を原子実行 → 採用通知
 → ④pack overlay（soul は凍結・render 成果物のみ注入・candidates=稀少制約付き試用注入）
 → 次の会話で自然に使われる（ループが閉じる）

安全網 = 差分 snapshot（source+build hash 対）+ オーナーロールバック（≤1コマンド・実測検証済み）
       + ドリフト鏡（週次ライト+月次ディープ・固定 eval probe・soul ハッシュ照合）
       + 迎合キルスイッチ（解除はオーナーのみ）
```

## 3. 役割6（コンポーネント境界）

| # | コンポーネント | 役割 | 本アーキでの変更 |
|---|---|---|---|
| 1 | persona-engine（caty-ai/persona-engine） | 表現の器。pack を人格として注入する装置 | **エンジン機構は無改修**。SPEC v2 の catalogs 不透明資産 + `catalog_refs` + budget 検証に overlay を載せるだけ |
| 2 | pack・人格面（ルカ=wip-persona-engine / Alpha=CLAUDE.md 配下 ほか） | soul 層（凍結・基線タグ）+ overlay 層（成長の書き込み先）の2層 | overlay 新設。**engine 面と非 engine 面で凍結の実装強度は同型ではない**（§5） |
| 3 | persona-growth-loop（本リポ） | 成長系一式: 観測コレクタ・ハーベスター・遅延証拠集計・writer 提案・applier・夜間蒸留・ドリフト鏡 | Epic の主実装先 |
| 4 | self-growth-loop | 汎用採用パイプライン | **変更なし**。overlay レーンは sgl 非経由（R12b の構造条件が per-item 承認を代替）。soul 級・T1+ は従来通り council+オーナー承認 |
| 5 | [caty-agent-harness](https://github.com/caty-ai/caty-agent-harness) | ガバナンス正本の家（R1–R14）+ 部品提供元（tripwire・蒸留 cron パターン） | エンジン変更なし。R12 改定 = flh#123（発効 = CP-2） |
| 6 | リアルタイム層 | Warmth Core v0 B〜E 注入テキスト（プロンプト層構築物） | 専用リポなし。hook は将来の任意増幅器（v1 スコープ外） |

## 4. 接続点4（決定と根拠）

1. **リアルタイム層の住処 = プロンプト層**。
   engine runtime 機能化は SPEC 不変条件7（不透明ペイロード原則）違反。hook は思考を形成できず
   nudge のみ = 将来の増幅器。fast/slow の分離は「経路分離 + 遅延ゲート」で保証する
   （観測ログ経由の汚染可能性は鏡の観測信頼度チェック対象）。
2. **観測ログ = 二層 data plane**。
   Tier L（per-host raw・同期禁止・content-block フィルタ・30日 prune）/
   Tier S（vault・**集計値のみ・phrase 本文なし**・secret-scan 必須）。
   使用ログ（assistant 発話への phrase 出現マッチのみ）を「user 発話のみ」原則の
   **明示的限定緩和**として追加（帰属と holdout の前提）。詳細 = observation-log-schema.md。
3. **遅延報酬の判定 = pgl 自前の夜間 cron**。
   FLH 6拍子は会話に不適用・sgl 重レーンは overlay に過剰。
   証拠 = 事後の明示的言及（主）+ holdout 非劣後 + 負シグナルなし。翌日再訪は弱い補助に降格。
   詳細 = evidence-rules.md。
4. **R12 改定 = flh に governance 正本ファイルを新設した上で R12a/R12b + キルスイッチを条文化**。
   発効 = CP-2（正本 commit + 4箇所同期。定義は overlay-contract.md §14）。
   **発効前の overlay 自動書き込みは全面禁止**。

## 5. soul 凍結の実装強度（面ごとに異なる — 正直な規定）

| 面 | 凍結の実装 | 強度 |
|---|---|---|
| engine 面（ルカ等 pack） | build/budget/applier の硬構造: soul 層ファイルは applier の path allowlist 外 + `persona build` の all-or-nothing + `E_BUDGET_EXCEEDED` + content_hash 照合 | 構造的（バイパスには allowlist 定数の改変が必要） |
| 非 engine 面（Alpha CLAUDE.md 配下等） | **三層防御**: ①path-scoped 仲介（決定論 applier 経由のみ）②soul 定点ハッシュ監視（即時系検知）③規律（R12a・writer プロンプト制約） | 検知的+規律的（**engine 面と同型ではない**） |

- Alpha の CLAUDE.md 直接編集権（オーナー既定）は本 Epic では変えない。R12a の規律対象として存置。
- writer には**強制 hard cap**（cap 超過提案は applier が書き込み拒否）。tripwire（独立サイズ監視）は
  二重チェックとして別系統で走る。数値 = overlay-contract.md §12。

## 6. トレードオフ（検討し却下した代替案）

| 代替案 | 却下理由 |
|---|---|
| リアルタイム層を engine runtime 機能に | SPEC 不変条件7 違反（エンジンは内容を解釈しない） |
| リアルタイム層を hook に | hook は生成の前後にしか介入できず思考を形成できない。将来の増幅器として保留 |
| 学習判定を FLH 6拍子に載せる | 6拍子はタスク実行ループ用で会話に不適用 |
| overlay 採用を sgl 経由に | per-item 承認は自律成長の哲学と衝突し速度律速。R12b 構造条件が代替（発効=CP-2） |
| overlay を別リポ化（Opus F5 三択の一つ） | SPEC §2.2 の `catalog_refs` は pack/catalogs/ 配下の実ファイル必須（symlink 脱出拒否）→ 結局 pack への copy step に write 資格が要り境界が移動するだけ。比較詳細 = overlay-contract.md §6 |
| deploy key による資格分離（同三択） | remote push のスコープ制御であり、ローカル書き込み（SPEC 不変条件5: state はホスト置き）を制約しない |
| 即時センチメントでの学習 | 迎合マシン化の主経路。遅延報酬のみ採用（設計原則） |
| ブロックへの日付・カウンタ埋め込み（不在検知の初案） | SPEC §5 が動的要素を禁止（バイト安定性）→ 夜間 render トグル方式に変更 |

## 7. 残余リスクの明文化（凍結事項）

**内容クラス制限（R12b 条件2）を per-item 承認なしで100%保証する手段は存在しない。**

phrase スロット制約・deny 文法・guard lint・異種モデル diff レビューは強い緩和策だが、
自然言語の意味空間を字面規則で完全に閉じることは原理的に不可能である
（例: deny 語彙に載らない婉曲な誘導表現）。この残余リスクを引き受ける前提で、安全網を多層化する:

1. **CP ゲート**（CP-2 発効前は自動書き込み全面禁止 / CP-3a・3b で面ごとにオーナー GO）
2. **迎合キルスイッチ**（解除はオーナーのみ・overlay-contract.md §10）
3. **ドリフト鏡**（週次ライト+月次ディープ・固定 eval probe = 押し戻しが正解のホールドアウト・
   迎合方向の合意率上昇で自動 HOLD 提案+赤旗）
4. **差分 snapshot + ロールバック**（≤1コマンド・≤5分・実測検証必須 = R13 定量）

GLM 席の少数意見（非 engine 面の自動採用は v1 全停止案）は CP-3a の判断材料として
Epic #3 に記録済み。

## 8. チェックポイント（Epic #3 の表が正本・ここは参照のみ）

CP-1 キックオフ（承認済み 2026-08-02）/ ◆CP-2 R12 発効 / ◆CP-3a 非 engine パイロット（Alpha）/
◆CP-3b engine 面（ルカ）/ ◆CP-4 共有・公開可視範囲 / CP-5 Epic close。

依存: #4（本書）→ {flh#123 ∥ wip#74 ∥ wip#75 ∥ pgl#5 ∥ pgl#8} → {wip#76 ∥ pgl#6} → pgl#7（鏡）
→ CP-3a/3b → レーン有効化 → pgl#9。
