"""Fail-closed exact-APPROVE diff review."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from writerd.propose import AdapterError, run_json_adapter


def review(argv: Sequence[object], payload: Mapping[str, object]) -> dict[str, Any]:
    result = run_json_adapter(argv, payload)
    if set(result) != {"verdict", "reason"} or result.get("verdict") != "APPROVE" or not isinstance(result.get("reason"), str):
        raise AdapterError(f"review blocked: {result.get('verdict', 'missing verdict')}")
    return result
