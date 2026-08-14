"""Fail-closed subprocess writer adapter."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping, Sequence


class AdapterError(RuntimeError):
    pass


def run_json_adapter(argv: Sequence[object], payload: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise AdapterError("adapter argv must be a non-empty string array")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        completed = subprocess.run(
            list(argv), input=encoded, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"adapter execution failed: {exc}") from exc
    if completed.returncode != 0:
        raise AdapterError(f"adapter exited {completed.returncode}")
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError("adapter output is invalid JSON") from exc
    if not isinstance(value, dict):
        raise AdapterError("adapter output must be an object")
    return value


def propose(argv: Sequence[object], evidence: Mapping[str, object]) -> dict[str, Any]:
    proposal = run_json_adapter(argv, evidence)
    required = {"proposal_id", "face", "phrase_id", "transition", "evidence", "generated_at"}
    if set(proposal) != required:
        raise AdapterError("writer proposal schema mismatch")
    if not all(isinstance(proposal[key], str) and proposal[key] for key in ("proposal_id", "face", "phrase_id", "transition", "generated_at")):
        raise AdapterError("writer proposal identifiers are invalid")
    if not isinstance(proposal["evidence"], dict):
        raise AdapterError("writer evidence must be an object")
    return proposal

