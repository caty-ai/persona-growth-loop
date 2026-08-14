"""Strict JSON-in/JSON-out seam for optional nightly signal classification."""

from __future__ import annotations

from typing import Mapping, Sequence

from writerd.propose import AdapterError, run_json_adapter


def classify(
    argv: Sequence[object], payload: Mapping[str, object], observation_count: int
) -> list[dict[str, object]]:
    result = run_json_adapter(argv, payload)
    if set(result) != {"results"} or not isinstance(result["results"], list):
        raise AdapterError("classifier output schema mismatch")
    decisions = result["results"]
    if len(decisions) != observation_count:
        raise AdapterError("classifier result count mismatch")
    validated: list[dict[str, object]] = []
    for index, item in enumerate(decisions):
        if (
            not isinstance(item, dict)
            or set(item) != {"index", "negative", "mention"}
            or item.get("index") != index
            or type(item.get("negative")) is not bool
            or item.get("mention") is not None and type(item.get("mention")) is not bool
        ):
            raise AdapterError(f"classifier result invalid at index {index}")
        validated.append(item)
    return validated
