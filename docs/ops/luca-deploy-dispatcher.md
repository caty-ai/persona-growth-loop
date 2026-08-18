# Luca deploy dispatcher — operator runbook (private)

The operational wiring runbook for the Luca deploy dispatcher (forced-command SSH
key setup, host-specific install checklist, operator boundaries for rollback and
manual operations) lives in the private ops repository.

The dispatcher implementation itself ships in this repository
(`vps/pgl-luca-dispatch`, `growthlane/deploy.py`) together with its test suite;
only the host-specific operating instructions are private.

## Observable acceptance semantics

Acceptance opens a fresh conversation with `stream: true` for every turn. It
validates the session header without reading response-body bytes, holds the
stream open until `status.json` contains a persist owned by that session's
`turn_key`, and then closes the stream. The switch is accepted only when the
owned record reports `mode-b` with manifest-matching bytes and SHA-256 data.
Restoration requires the same owned convergence proof for the previous mode.
A normal run returns three session UUIDs; a restore retry can add a fourth.
Failure returns no response body.

## Failure reason codes

The dispatcher may append one fixed code to its otherwise detail-free
rejection line:

| Code | Meaning |
|---|---|
| `accept-warmup-failed` | The warm-up stream could not be opened or its session header was invalid. |
| `accept-warmup-timeout` | No warm-up persist owned by the session appeared within the available window. |
| `accept-switch-request-failed` | The mode-switch stream failed to open for a non-timeout transport reason. |
| `accept-switch-request-timeout` | The mode-switch stream did not return headers within its bounded open timeout. |
| `accept-switch-not-applied` | The switch window ended without an owned `mode-b` record. |
| `accept-switch-metadata-mismatch` | An owned `mode-b` record did not match the manifest metadata. |
| `accept-switch-failed` | Warm-up state, status data, or manifest data made the switch unverifiable. |
| `accept-restore-failed` | Restoration did not obtain owned convergence proof, so runtime mode is unknown. |
| `accept-restore-retried` | Restoration converged only after a retry, so acceptance still failed closed. |
| `restart-command-failed` | The fixed restart command failed to execute or exited nonzero. |
| `restart-command-timeout` | The fixed restart command exceeded its timeout. |
| `restart-units-not-active` | At least one fixed unit reported not-active during verification. |
| `restart-verification-failed` | Per-unit verification failed or timed out. |

Every emitted rejection line is an author-time literal. Runtime values,
exception text, paths, commands, HTTP status phrases, and other foreign data
are never interpolated into it. A stream closed before its owned persist is
observed is a failed acceptance, even if an unowned target-mode record exists.

## UNKNOWN manual recovery

UNKNOWN means an unresolved attempt contains `recovery-started` without a
later matching `recovery-completed`. It can be cleared only by an attended
recovery of that same attempt that completes restoration, restart, previous
hash verification, previous-state acceptance, and the journal writer's
`recovery-completed` append.

Repeated `recovery-started` events are valid while an operator retries an
unresolved recovery. Neither retry events nor `recovery-completed` may be
hand-appended: all lifecycle changes must pass through the journal writer so
ordering, durable writes, and acceptance-window handling remain enforced.
