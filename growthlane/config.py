"""Strict runtime and threshold configuration loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


DEFAULT_THRESHOLDS = {
    "window": 30,
    "min_count": 8,
    "min_days": 5,
    "staged_min_days": 14,
    "min_uses": 3,
    "decay_days": 90,
    "echo_ratio": 0.5,
}

STRICTER_DIRECTIONS = {
    "window": "smaller",
    "min_count": "larger",
    "min_days": "larger",
    "staged_min_days": "larger",
    "min_uses": "larger",
    "decay_days": "smaller",
    "echo_ratio": "smaller",
}
RELAXABLE_THRESHOLDS = frozenset({"window", "min_count", "min_days", "decay_days", "echo_ratio"})
HARD_FLOOR_THRESHOLDS = frozenset({"staged_min_days", "min_uses"})


class Thresholds(dict[str, object]):
    def __init__(self, values: dict[str, object], deviations: list[str]) -> None:
        super().__init__(values)
        self.deviations = tuple(deviations)


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConfigError(f"duplicate evidence config key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid JSON config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"config must be an object: {path}")
    for key in ("writer_argv", "reviewer_argv", "classifier_argv"):
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(item, str) or not item for item in value[key])
        ):
            raise ConfigError(f"{key} must be a string array")
    writer = value.get("writer_argv")
    reviewer = value.get("reviewer_argv")
    if isinstance(writer, list) and writer and writer == reviewer:
        raise ConfigError("writer_argv and reviewer_argv must be distinct")
    return value


def parse_scalar_yaml(path: Path) -> Thresholds:
    """Load thresholds and enforce the frozen one-way governance ratchet."""
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("evidence config must be a mapping")
    allowed = set(DEFAULT_THRESHOLDS) | {"relaxation_approval"}
    if set(value) - allowed or not set(DEFAULT_THRESHOLDS).issubset(value):
        raise ConfigError("evidence config keys do not exactly match schema v1")
    result = {key: value[key] for key in DEFAULT_THRESHOLDS}
    for key in DEFAULT_THRESHOLDS:
        expected = DEFAULT_THRESHOLDS[key]
        if type(expected) is int and (type(result[key]) is not int or result[key] <= 0):
            raise ConfigError(f"invalid positive integer threshold: {key}")
    ratio = result["echo_ratio"]
    if type(ratio) not in {int, float} or not 0 <= float(ratio) <= 1:
        raise ConfigError("echo_ratio must be between 0 and 1")
    approval = value.get("relaxation_approval")
    approval_valid = (
        isinstance(approval, dict)
        and set(approval) == {"decided_by", "ref"}
        and all(isinstance(approval.get(key), str) and approval[key].strip() for key in ("decided_by", "ref"))
    )
    deviations: list[str] = []
    for key, default in DEFAULT_THRESHOLDS.items():
        current = result[key]
        if current == default:
            continue
        direction = STRICTER_DIRECTIONS[key]
        tighter = current < default if direction == "smaller" else current > default
        if tighter:
            deviations.append(f"threshold {key} tightened {default}->{current}")
            continue
        if key in HARD_FLOOR_THRESHOLDS:
            raise ConfigError(f"frozen threshold cannot be loosened: {key}")
        if key not in RELAXABLE_THRESHOLDS or not approval_valid:
            raise ConfigError(f"threshold relaxation requires approval: {key}")
        deviations.append(
            f"threshold {key} relaxed {default}->{current} approval_ref={approval['ref']}"
        )
    if approval is not None and not approval_valid:
        raise ConfigError("invalid relaxation_approval")
    return Thresholds(result, deviations)
