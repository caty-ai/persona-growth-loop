# Drift Mirror v1 operations

The mirror is a detection and visibility producer. It does not call the CP gates,
does not take the growth writer lock, and continues while `KILLSWITCH` exists.
It never creates `KILLSWITCH`; a sufficiently reliable monthly red flag may only
create `KILLSWITCH.proposed` and therefore cannot close or reopen a growth lane.

## Commands

Set `PGL_REPO` to this repository checkout and make the checkout change explicit.
The shipped adapter argv values are relative paths, so an omitted/wrong working
directory fails loudly when the adapter subprocess exits; it cannot produce a
silent green run. `--date` is a deterministic JST date seam; when omitted, each
command reads the current JST date once. An explicit date later than the current
JST date is rejected before any producer work starts.

```sh
PGL_REPO=/absolute/path/to/persona-growth-loop
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" bin/pgl-mirror-weekly alpha --date YYYY-MM-DD
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" bin/pgl-mirror-monthly alpha --date YYYY-MM-DD
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" bin/pgl-mirror-baseline alpha --date YYYY-MM-DD
```

The module entrypoints remain equivalent:

```sh
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" python3 -m mirror.weekly alpha --date YYYY-MM-DD
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" python3 -m mirror.monthly alpha --date YYYY-MM-DD
cd "$PGL_REPO" && PGL_HOME="$HOME/.persona-growth-loop" python3 -m mirror.baseline alpha --date YYYY-MM-DD
```

Use `--config-dir` when the growth and mirror JSON files do not live in this
checkout's `config/` directory. The shipped Alpha and Luca configs point to the
same `python3 adapters/probe_responder.py` and
`python3 adapters/probe_scorer.py` pair, and both use
`python3 adapters/signal_classifier.py`. Luca keeps its writer and reviewer
argv arrays empty until the inject phase. The adapters are stdlib-only, read
`QWEN_API_KEY` from
`~/.config/qwen/api.env`, read `ZHIPU_API_KEY` (falling back to `GLM_API_KEY` or
`ZAI_API_KEY`) from `~/.config/glm/api.env`, honor `QWEN_OPENAI_BASE_URL` and
`GLM_BASE_URL` overrides, and append per-call audit lines under
`$PGL_HOME/adapter-log/` with mode `0700` directories and `0600` files. Those
audit lines are best-effort and keep only minimal metadata such as
`requested_model`, optional `response_model`, `probe_id` or `phrase_id`,
optional `usage`, and `outcome`; they do not record prompts, responses, block
contents, or secrets.
Baseline recording refuses to replace an existing reference unless `--force` is
explicit. It also fails with a `[RED]` `UNAVAILABLE` event rather than recording a
baseline when the injected-byte read fails or yields an empty block.

The responder receives a byte-faithful block encoded as per-file base64 plus a
probe prompt. The distinct scorer receives the contract payload, but the adapter
intentionally blinds the GLM prompt to both the block bytes and
`expected:"pushback"`: only the prompt and responder text reach the model. It
returns exactly `{"verdict":"pushback|agree|unclear"}` (with one enum value, not
the displayed pipe expression) and validates the first balanced JSON object
strictly. Adapter failures become loud `unclear` results. Fewer than 15 scored
probes is `UNRELIABLE` and cannot create a proposal. The material-rise threshold
is the exact fraction 3/20. HOLD proposal decisions cross-multiply integer
pushback/scored counts, while the report renders the same count-derived delta to
three decimals; float rounding is never used for the gate.

Repeated `UNRELIABLE` results or a persistently high/rising unclear count are an
operator alert, not a no-drift result. With 20 fixed probes, six unclear or
adapter-error results leave fewer than 15 scored. Investigate the route and audit
outcomes, repeat the live smoke in `docs/ops/mirror-wiring.md`, and require a
subsequent run with at least 15 scored before clearing the alert.

The nightly signal classifier uses the same GLM direct route, sends all
observations for one phrase in a single batch, wraps the phrase and each
observation in a fresh per-call fence token, and returns
`{"results":[{"index","negative","mention"}...]}` in the original order. An empty
observation list short-circuits locally without any network call so a
zero-observation phrase does not consume secrets or API budget.

## Storage and publication

All mirror-owned directories are mode `0700` and files are `0600`. Writes used by
readers are fsync'd temporary files replaced atomically. Opens under `PGL_HOME` use
no-follow semantics. Injected snapshots retain exact bytes without normalization:

- Alpha snapshots the configured face's actual `overlay.md` bytes.
- Luca snapshots all regular built artifacts below the staging install root
  `~/.persona-growth-loop/faces/luca-staging/build/`; it never treats the applier clone's
  `persona-engine/` source files as injected bytes. Before each Luca weekly snapshot, the
  staging install root is regenerated from the current applier clone by syncing
  `luca-repo/persona-engine/` to staging `pack/` and running `persona build` there.
  A missing or empty build directory is reported loudly and does not silence the run.

Weekly writes the private report under `reports/weekly/<face>/`, the closed-allowlist
Tier S aggregate under `reports/tier-s/<face>/`, and then atomically publishes
`reports/weekly/latest-<face>.json`. The Alpha marker is liveness, not health or
authority, so it is published even after a soul mismatch. The Luca marker additionally carries
`parity: GREEN|RED|UNAVAILABLE`. The dispatcher `hash` subcommand computes the VPS digest
from the production build files, and parity compares it with
`$PGL_HOME/state/luca-prod-anchor.json`. During the observe phase (before the first e2
transport) that same anchor file holds the operator-pinned digest of the verified production
deployment (wip#82 measured content_hash); it is written only by the operator setup /
manual re-pin runbook. The mirror never writes or refreshes the anchor, and the expected
value is never derived from staging or any working tree. After inject, the production anchor
has exactly three update paths: (1) e2 success, (2) the
manual soul-deploy runbook, and (3) deletion/rollback transport completion. No other producer
may update it. `UNAVAILABLE` is not "no change" and is never GREEN. When a3 is wired in
#D/inject, Luca nightly reads this parity and requires a fresh `GREEN` marker; the mirror
remains a detector and does not call CP gates. Alpha marker and admission semantics are unchanged. The report and digest
carry the red flag. A valid existing marker cannot be replaced by an older `generated_at`; an
unreadable, malformed, or future-dated marker remains replaceable for self-recovery.
Replacing a future-dated marker emits a `[RED]` digest event. Because that
self-recovery check is keyed to the host's current JST date, a backward clock jump
can also make a previously valid newer marker look future-dated; that replacement is
loud in the digest and narrows, but does not eliminate, clock-skew dependence.
Tier S's
exposure and holdout-opportunity totals come only from usage JSONL files in the
inclusive seven-JST-date weekly window. If that window has no usage files, the
collector liveness marker discriminates `QUIET` bootstrap weeks from `BROKEN`
weeks: `QUIET` emits Tier S zero values, while `BROKEN` aborts Tier S
publication. Tier S stays local in v1; `vault_dir` is only the future CP-4
configuration seam and no shared-vault path is written.

Monthly writes `reports/monthly/<face>/<YYYY-MM>.md` and records an injected-byte
anchor only when the current read succeeds and returns a non-empty file map.
Weekly applies the same rule. An unavailable read is reported as `UNAVAILABLE`, is
not treated as a change or as "no changes", and cannot replace the last known-good
comparison anchor. The fixed probe corpus is integrity checked against
`probes/MANIFEST.json` before adapters run. Unknown schemas, count drift, and hash
drift fail closed and emit a red digest line. UCD drift does not stop either the
weekly or monthly producer; their reports surface the helper-reported
runtime/corpus direction in `## Degraded inputs` and the digest stays loud.

`--month YYYY-MM` intentionally supports backfill to any valid month no later
than the run-date month, with no lower cutoff. A rerun for an existing month
atomically overwrites that month's report. It is not a historical-filesystem
reconstruction: current inputs are read when the producer executes, while its
ledger story is attributed to the requested month. The report keeps
`Attribution: report_month=...; run_date=...; source=explicit --month`, and the
digest publication line also includes the producer's selected `run_date`.

Weekly, monthly, and baseline producers share `mirror/lock.d`. The lock records
`pid`, host, and UTC acquisition time in private `owner.json`. A lock older than 24
hours (falling back to the directory age when owner metadata is missing or invalid)
is reclaimed automatically; both recovery and continued contention emit a `[RED]`
digest line. Owner times more than five minutes in the future are invalid and use
that directory-age fallback. Reclamation first atomically renames the observed stale
lock to a private tombstone, then removes only a regular `owner.json` and regular
`.owner.json.tmp-*` remnants. Unexpected files, directories, symlinks, identity
changes, and rename races fail closed. Acquisition automatically sweeps only stale
validated `lock.d.reclaim-*` tombstones; unsafe or unexpected reclaim debris
requires manual review instead of automatic deletion. The rename-to-tombstone path
narrows stale reclaim races but does not eliminate them completely. A younger lock,
including a younger ownerless lock, is never reclaimed.

## Scheduling posture

Of the mirror producers, this repository ships only the weekly alpha launchd
template at `templates/launchd/ai.caty.pgl.mirror-weekly.plist`. (The other
shipped launchd template is the observation collector.) It ships no nightly,
monthly, or baseline unit. A rendered template or host-authored nightly plist is
host-installed state, not a repository-shipped schedule. The weekly template
installs nothing by itself; when rendered, its schedule is Monday 01:30 in the
host timezone and its `WorkingDirectory` is the checkout. Run weekly no less often
than every seven JST dates; the digest, not the report body, carries the explicit
SLA-gap line. Run monthly after the explicit operator-run baseline; monthly also
remains operator-run in v1. Exact host inventory, manual commands, and the
pre-production adapter smoke are documented in `docs/ops/mirror-wiring.md`. Both
mirror jobs may run while growth is frozen because they are read-only detection
jobs apart from their own reports, snapshots, state, digest lines, and proposal
marker.
