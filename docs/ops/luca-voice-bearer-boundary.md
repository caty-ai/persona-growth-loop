# Luca voice Bearer trust boundary

> Note: `pgl#NN` / `wip#NN` references are records from the pre-publication private tracker.

This note fixes the operator boundary for the owner-level Bearer used when the
`api_server` voice route is admitted into Hermes Luca observation intake.

## Holder set

The owner-level Bearer is trusted only while it exists in exactly these two
places:

1. CatyPhone on the owner's device, as the active credential that can call the voice
   conversation API.
2. The VPS-resident dispatcher copy, used only for read-only ownership
   verification and route metadata checks required by the observation collector.

Any additional copy, cache, export, transcript embedding, shell history entry,
or ad hoc operator paste is outside the allowed boundary.

## Why exposure is high impact

If this Bearer leaks, the attacker gets two operator-relevant capabilities at
once:

1. Conversation API execution as the owner-level voice caller.
2. Observation contamination, because forged or replayed owner-looking voice
   sessions can enter the same intake boundary that the collector trusts.

This is not only a privacy leak. It can poison evidence used by Luca growthlane
judgment and usage accounting.

## Shipped state: voice intake is OFF (P2 prerequisite unmet)

`config/obs-collector-luca.json` ships with `voice_enabled: false`, and the
observe phase starts that way. This is not a change of the approved three-route
decision; it is the frozen design's own precondition applied fail-closed.

Design `docs/luca-lane-v1.md` §1.2 admits voice **only** once two conditions
hold: (a) this Bearer boundary is documented, and (b) the P2 acceptance
conversations are structurally excluded — since v1.2 via the verify-session
ledger (previously via the `pgl-verify-*` session prefix, which measurement
showed cannot exist on this route).
Condition (b) does **not** hold today. Measured 2026-08-07 over the read-only
dispatcher: Hermes assigns `api_server` sessions bare UUID ids with no
`session_key`, so no prefix exists to exclude. In the same database, **36
`api_server` user turns carry pgl-generated deploy markers** (persona-mode
switches and warm-up utterances from the wip#82 deployment acceptance), 8 of
them inside a single current-day window. With voice enabled, those turns would
enter obslog as the owner's speech — exactly the self-contamination principle P2
forbids.

Re-enable path (revised 2026-08-08, the owner's decision, pgl#44, r2 after the
four-seat design review): a conversation-id prefix cannot reach `state.db`
(measured: Hermes maps `conversation` to an opaque per-conversation UUID;
`X-Hermes-Session-Key` is not forwarded), and the UUID is server-assigned, so
no ledger write can strictly precede the contaminating rows. The structural
exclusion is therefore two-layered (luca-lane v1.2 §1.2-5):

1. **Intent-journal window (first line, true write-ahead)**: any pgl-origin
   `api_server` speech must be preceded by an fsynced `acceptance-window open`
   journal entry on the MBP; the collector unconditionally drops `api_server`
   rows inside recorded open/close windows, and a missing close (crash) drops
   rows in every bucket until a close or an operator resolution is recorded,
   + digest RED. A journal that is missing, unreadable, or has any malformed
   line is treated exactly like a broken ledger: drop all `api_server` rows
   this run + digest RED ("unreadable journal = zero windows" is
   non-conforming). The window margin must be at least the measured MBP↔VPS
   clock skew.
2. **Verify-session ledger (second line, precision/history)**:
   `$PGL_HOME/state/luca-verify-sessions.jsonl` (append-only, 0600,
   `origin ∈ {"seed","acceptance"}`). The dispatcher `accept` response returns
   the generated session UUIDs and the MBP pipe appends them, fail-closed
   (append failure fails the acceptance — wip-persona-engine#90); orphan
   sessions are covered by layer 1. The historical wip#82 acceptance sessions
   are seeded once by the operator, identified by audit/deploy-record
   correlation (not by content matching — mixed sessions measured 2026-08-08
   must not be blanket-seeded), with a written record. The collector excludes
   ledger-listed sessions before obslog/usage and treats a missing, unreadable,
   malformed (any bad line = whole ledger invalid), or — with voice enabled —
   empty/seedless ledger as "drop all `api_server` rows this run + digest RED".
   Detection-only alerts (never content-based exclusion) run after exclusion:
   exact `deployment warm-up` = RED (operator follow-up per the failure-matrix
   recovery row is mandatory), `/persona ` prefix = a digest count line only
   (it overlaps the owner's legitimate voice UX and would fire on normal nights).

Flipping `voice_enabled` back to `true` requires: the ledger + fixture proofs
in place, the seed completed and machine-verified (correlated set ⊆ ledger),
and **wip-persona-engine#90 closed** (the acceptance-side recording duty —
tracked outside this repo, so it is named here explicitly). Until then Telegram
and Slack DM carry the observation, and the voice sample — the largest single
source at 621 user turns — stays out.
Before that flip, the operator initializes the empty 0600
`<obs_root>/state/luca-intent-journal.jsonl` journal file and the
`<obs_root>/state/collector/luca.ledger-lines.json` baseline file so the
fail-closed checks do not start from a missing baseline. The flip edits are
exactly: set `voice_enabled` to `true` and add `api_server` to `sources`.
`line_count` is the ledger line count at seed completion, written with a
single command such as
`PGL_HOME=${PGL_HOME:-"$HOME/.persona-growth-loop"}; SEED_LINE_COUNT=$(wc -l < "$PGL_HOME/state/luca-verify-sessions.jsonl"); PGL_HOME="$PGL_HOME" SEED_LINE_COUNT="$SEED_LINE_COUNT" python3 -c 'import os; from datetime import datetime; from collectors.hermes_luca import ledger; ledger.write_line_count(os.environ["PGL_HOME"], int(os.environ["SEED_LINE_COUNT"]), datetime.now().astimezone().replace(microsecond=0).isoformat())'`.
An overlarge baseline is permanently RED; an underlarge baseline weakens
truncation detection until the first successful run refreshes it. Both the
journal and ledger files must append exactly one newline per entry, with no
trailing blank line, because a blank line in either file is invalid and fails
closed.

## Required mitigations

1. Keep the intent-journal window and verify-session ledger exclusions in
   force for voice, and the `pgl-verify-*` prefix exclusion for sources where
   a prefix survives, so verification traffic cannot be re-ingested as
   operator evidence. Voice stays disabled until the journal wiring, the
   ledger, their fixture proofs, and the historical seed exist (see above).
2. Use read-only transport for ownership verification. The collector may read
   dispatcher-exposed metadata, but must not gain a write path or a reusable raw
   secret export path.
3. Do not persist raw voice payloads or the Bearer itself on MBP storage. The
   allowed durable output is scrubbed obslog/usage data only.
4. On suspected leak, respond operationally by rotating or disabling the voice
   Bearer before resuming intake. Treat this as an incident on the same boundary
   as forged owner traffic.

## Residual risk

Hermes does not attach a user uid to `api_server` voice turns. For v1, the
runtime-derived `source == "api_server"` route plus the owner-level Bearer is
the attribution boundary; the collector never tries to infer ownership from
message content. This leaves a deliberate residual risk: if the Bearer leaks,
an attacker can create turns that are indistinguishable from the owner's voice route
and contaminate observation evidence until the credential is disabled or
rotated. `voice_enabled` is the fail-closed operator switch for that incident;
Telegram and Slack continue to require exact uid allowlist matches.
