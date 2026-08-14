# INTEGRATION.md — engine seam declaration

Per [plugin-convention.md](https://github.com/caty-ai/caty-agent-harness/blob/main/docs/plugin-convention.md) rule 4 (dual bookkeeping). The matching registry entry lives in the **[caty-agent-harness](https://github.com/caty-ai/caty-agent-harness) repo's** `docs/plugins.md` — not persona-engine's ("engine" elsewhere in this Epic's docs means persona-engine; the plugin registry belongs to the harness).

## Engine pin

```
HARNESS_VERSION=v0.2.2
```

## Seams used (Warmth Persona Architecture v1 — contracts frozen at pgl#4)

| Seam | Usage |
|---|---|
| 1. Enqueue | **Not used.** Growth adoption runs on pgl's own nightly pipeline (the harness six-beat loop does not apply to conversations), not on the engine task loop. |
| 2. Results | Not used. |
| 3. Templates | Not used. |
| 4. Data plane | **Tier S aggregates only** (weekly counters: candidate/adopted counts, exposure/negative-signal counts, cap usage, soul-hash check result — full list frozen in [docs/contracts/observation-log-schema.md](docs/contracts/observation-log-schema.md) §5). **Deviation from seam-4 default, recorded per convention: Tier L local raw logs are never synced** (per-host, 0600, 30-day prune). No phrase bodies ever reach the vault. |

## Growth-write contract (NOT a harness seam — dual bookkeeping note)

The overlay write path (nightly deterministic applier → pack repos' `catalogs/overlay/*` /
Alpha render file) is a **new contract between persona-growth-loop and the pack repos**,
not a caty-agent-harness seam. It is recorded here and in the harness `docs/plugins.md` (pgl row)
so both ledgers point at the same source of truth:

- Contract: [docs/contracts/overlay-contract.md](docs/contracts/overlay-contract.md)
  (two-plane split, deterministic path-scoped applier, atomic pipe, caps, killswitch, rollback)
- Governance: R12a/R12b + kill switch live in the harness
  [`docs/governance-rules.md`](https://github.com/caty-ai/caty-agent-harness/blob/main/docs/governance-rules.md)
  (decision record: issue #123 on the pre-publication private tracker, kept as historical
  reference only). **CP-2 approved by the owner 2026-08-02** — the record of record for
  in-force status is that file's version-history table.
  Overlay writes (manual or automated; empty-file bootstrap excepted) stay forbidden until the
  per-face CP-3 GO.
- sgl is **not** on this path (overlay lane bypasses sgl by design; soul-tier changes remain
  council + owner approval as before). **The exemption is the overlay lane only (governance-R12b,
  rendered phrase data); the sgl approval flow is unchanged.**

## Target runtimes

- Observation collector: Alpha runtime (MBP), read-only over Claude Code transcripts
  (content-block filter + scrub, [docs/contracts/observation-log-schema.md](docs/contracts/observation-log-schema.md)).
- Nightly pipeline (harvester / evidence aggregation / writer proposals / applier): MBP launchd,
  per-face single-writer mkdir lock (`~/.persona-growth-loop/lock-<face>.d`; a failed acquisition
  skips that face loudly — `[RED]` digest line + non-zero exit).
- Applier write scope (path allowlist, per face — both planes co-located in one "overlay home" git repo
  per overlay-contract §1): luca = applier clone `~/.persona-growth-loop/faces/luca-repo/`
  `persona-engine/catalogs/overlay/{candidates,adopted}.txt` + `growth/overlay-ledger.yml`;
  alpha = `~/.persona-growth-loop/faces/alpha/` (`overlay.md` + `overlay-ledger.yml`);
  plus snapshot commits/tags in those repos. Nothing else.

## Cron entries

- Observation collector: the pgl#5 launchd template
  `templates/launchd/ai.caty.pgl.obs-collector.plist` runs nightly at 23:45 local
  time. It is shipped but not installed; the operator renders its checkout/home
  placeholders and loads it manually per `docs/ops/collector-wiring.md`.
- The combined nightly growth pipeline still lands with pgl#6 and remains
  **disabled until CP-2 + CP-3 per-face GO**. Its synchronous entrypoint is:

  ```sh
  PGL_HOME="$HOME/.persona-growth-loop" bin/pgl-nightly --date YYYY-MM-DD
  ```

  `--date` is a JST bucket injection seam for deterministic drills/tests. With no
  flag, the command reads the current JST date once. The mkdir lock is held across
  harvest, aggregation, writer, review, apply, and the final tripwire pass; the
  process never launches a detached waiter.

## Governance and mirror marker contracts

`$PGL_HOME/gates.yml` is human-owned and is never created or modified by this
pipeline. The minimum accepted shape is:

```yaml
cp2_in_force: true
decided_by: <non-empty governance actor>
ref: <governance record pointer>
faces:
  alpha:
    cp3_go: true
    decided_by: <non-empty governance actor>
    ref: <per-face GO record pointer>
```

Each enabled face also requires pgl#7 to atomically publish
`$PGL_HOME/reports/weekly/latest-<face>.json` with an ISO `generated_at` date or
timestamp no more than 14 JST dates old. Missing, invalid, future-dated, or stale
markers close the lane. This marker is the stable producer/consumer contract for
pgl#7; report contents outside `generated_at` are not authorization inputs.

The killswitch is `$PGL_HOME/KILLSWITCH`; file existence is ON and malformed
contents fail to `freeze`. `mode: eject` authorizes only one explicit
`bin/pgl-eject <face>` empty-render transaction. `bin/pgl-block` and
`bin/pgl-forget` remain available while the killswitch is ON because they only
reduce or erase learned state. Per contract §5.5 (direction bifurcation, amended
2026-08-04), the manual deletion-direction operations (block/forget/eject — a
closed set) are exempt from post-effectiveness CP gates and from the detection
mirror's liveness marker — this resolves the former self-contradiction where
this document declared urgent deletion ungated by the mirror while contract §10
still routed eject through §5.3 a2. Nightly automatic demotion stays on the
nightly pipe's admission gates (council F-3 deferred in v1). Manual deletion
ops still require the global lock, a clean allowlist, soul verification, §12
cap checks, build/hash verification, commit-on-success, and the §5.5 monotone
non-increase check (post-run candidate/staged/adopted sets and render phrase
content must be subsets of their pre-run state, blocklist a superset); eject
degrades to render-only verification when the ledger is unreadable. Gate- or
ledger-unverified runs are recorded with [RED] digest lines and commit trailers
instead of being blocked.

## Nightly batching and adapters

One face produces at most one snapshot commit per JST date/run. That commit batches
current-window evidence-counter recomputation, the deterministic daily holdout
render, up to three new ledger-only candidates, up to two reviewed promotions, and
unlimited safety demotions/blocks. Evidence maintenance and daily toggle rendering
are the contract §5 exception and therefore do not create proposal history entries;
state transitions do.

`config/growth-<face>.json` may provide only host/runtime values such as display
name, speaker, transcript root, Luca's overlay repo root, and writer/reviewer/
classifier argv arrays. It cannot alter allowlists, caps, templates, gate paths, or
commands. Adapters receive canonical JSON on stdin and must return schema-valid
JSON on stdout within 120 seconds. Writer and reviewer argv must differ exactly;
code cannot prove that their commands select different models, so model identity
remains an operator duty. Empty shipped argv arrays deliberately fail closed.

The optional classifier adapter is the seam for evidence-rules §4's second-stage
nightly classification. With no classifier configured, precise literal patterns
remain active and every nightly digest warns that the two-stage requirement is not
fully met. Once configured, adapter failure or an invalid negative classification
aborts the face; an explicit indeterminate mention is counted as no mention.
Shipping and validating full two-stage classification is a **CP-3a precondition**,
alongside mirror v1 and the rollback drill; this repository does not ship a live
model adapter.

`config/evidence.yml` is a one-way ratchet relative to the frozen defaults.
Tightening is accepted and digested. Relaxing `window`, `min_count`, `min_days`,
`decay_days`, or `echo_ratio` requires an in-file
`relaxation_approval: {decided_by, ref}` record; `staged_min_days` and `min_uses`
can never be loosened. Every non-default value is reported in the nightly digest.

New snapshot records use `parent_sha` for the pre-transaction commit and record the
created `tag` beside `content_hash`. Schema-v1 readers continue to accept historical
records that used the legacy `source_sha` field.

Assistant usage scanning now reads the face's schema-v1 `overlay-ledger.yml` and
matches only `staged`/`adopted` entries against raw assistant transcript turns.
Absent, invalid, or unknown-version ledgers disable the whole usage scan for that
bucket and increment the collector invalid-phrase stat; no readable subset is used.

## Integration test

No harness seam is exercised (enqueue/results/templates unused), so plugin-convention rule 2's
engine-tag integration test is N/A. In its place, the frozen contracts require:

- collector fixture tests (observation-log-schema §6) — CI-mandatory from pgl#5
- guard-lint test vectors + empty-overlay build regression (overlay-contract §2, §6) — from pgl#6 / wip#76
- rollback drill record (overlay-contract §11) — precondition of CP-3a
- mirror v1 producer and full two-stage signal classifier — preconditions of CP-3a

## Drift mirror v1 producer

The detection-only mirror owns the weekly liveness producer consumed by
`growthlane.gates.check_mirror`: after its private weekly report is durable it
atomically replaces `$PGL_HOME/reports/weekly/latest-<face>.json` with a valid
`generated_at` JST date. That marker records producer liveness only. A soul mismatch,
active killswitch, or absent governance gates does not suppress it and does not turn
the marker into authorization. Health findings remain in the owner report and digest.

The module entry points are `python3 -m mirror.weekly`,
`python3 -m mirror.monthly`, and `python3 -m mirror.baseline`. Exact injected-byte
snapshots and probe records remain mirror-local. The weekly Tier S file contains only
the closed field families in the observation-log contract and remains local staging
until CP-4 supplies a separately reviewed vault-emission path.
