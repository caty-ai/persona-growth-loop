"""Strict schema-versioned overlay ledger IO."""

from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml


SCHEMA_VERSION = 1
STATES = frozenset({"candidate", "staged", "adopted", "demoted", "blocked"})
PHRASE_ID = re.compile(r"^p-(\d{4,})$")
HOLDOUT_FIELDS = (
    "exposed_days",
    "holdout_days",
    "exposed_neg",
    "holdout_neg",
    "exposed_mentions",
    "holdout_mentions",
)
EVIDENCE_FIELDS = ("uses", "last_used_at", "explicit_mentions", "negative_signals")
HEX_HASH = re.compile(r"^[0-9a-f]{64}$")


class LedgerError(ValueError):
    pass


class LedgerVersionError(LedgerError):
    pass


def empty_ledger(face: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "face": face,
        "phrases": [],
        "retired_ids": [],
        "retired_text_hashes": [],
        "history": [],
        "snapshots": [],
    }


def _require_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be a mapping")
    return value


def _canonical_date(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise LedgerError(f"invalid date: {label}")


def validate_ledger(value: object, expected_face: str | None = None) -> dict[str, Any]:
    ledger = _require_dict(value, "ledger")
    version = ledger.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise LedgerVersionError(f"unsupported ledger schema_version: {version!r}")
    face = ledger.get("face")
    if not isinstance(face, str) or not face or (expected_face is not None and face != expected_face):
        raise LedgerError("ledger face is missing or mismatched")
    phrases = ledger.get("phrases")
    if not isinstance(phrases, list):
        raise LedgerError("ledger phrases must be a list")
    ids: set[str] = set()
    for index, raw in enumerate(phrases):
        phrase = _require_dict(raw, f"phrases[{index}]")
        phrase_id = phrase.get("id")
        if not isinstance(phrase_id, str) or PHRASE_ID.fullmatch(phrase_id) is None or phrase_id in ids:
            raise LedgerError(f"invalid or duplicate phrase id: {phrase_id!r}")
        ids.add(phrase_id)
        if not isinstance(phrase.get("text"), str) or not phrase["text"]:
            raise LedgerError(f"invalid phrase text: {phrase_id}")
        if phrase.get("state") not in STATES:
            raise LedgerError(f"invalid phrase state: {phrase_id}")
        source = _require_dict(phrase.get("source"), f"source {phrase_id}")
        for key in ("first_seen", "window_count", "distinct_days", "echo_ratio"):
            if key not in source:
                raise LedgerError(f"missing source.{key}: {phrase_id}")
        source["first_seen"] = _canonical_date(source["first_seen"], f"source.first_seen {phrase_id}")
        if type(source["window_count"]) is not int or source["window_count"] < 0:
            raise LedgerError(f"invalid source.window_count: {phrase_id}")
        if type(source["distinct_days"]) is not int or source["distinct_days"] < 0:
            raise LedgerError(f"invalid source.distinct_days: {phrase_id}")
        if type(source["echo_ratio"]) not in {int, float} or not 0 <= float(source["echo_ratio"]) <= 1:
            raise LedgerError(f"invalid source.echo_ratio: {phrase_id}")
        source["echo_ratio"] = float(source["echo_ratio"])
        phrase["staged_at"] = _canonical_date(
            phrase.get("staged_at"), f"staged_at {phrase_id}", nullable=True
        )
        if phrase["state"] in {"staged", "adopted"} and phrase["staged_at"] is None:
            raise LedgerError(f"staged_at required for {phrase_id}")
        holdout = _require_dict(phrase.get("holdout"), f"holdout {phrase_id}")
        if any(type(holdout.get(key)) is not int or holdout[key] < 0 for key in HOLDOUT_FIELDS):
            raise LedgerError(f"invalid holdout counters: {phrase_id}")
        evidence = _require_dict(phrase.get("evidence"), f"evidence {phrase_id}")
        if any(key not in evidence for key in EVIDENCE_FIELDS):
            raise LedgerError(f"missing evidence fields: {phrase_id}")
        if any(type(evidence[key]) is not int or evidence[key] < 0 for key in ("uses", "explicit_mentions", "negative_signals")):
            raise LedgerError(f"invalid evidence counters: {phrase_id}")
        evidence["last_used_at"] = _canonical_date(
            evidence["last_used_at"], f"evidence.last_used_at {phrase_id}", nullable=True
        )
        constraints = _require_dict(phrase.get("constraints"), f"constraints {phrase_id}")
        if constraints.get("max_per_session") != 1:
            raise LedgerError(f"invalid constraints: {phrase_id}")
        history = phrase.get("history")
        if not isinstance(history, list):
            raise LedgerError(f"invalid history: {phrase_id}")
        for event in history:
            item = _require_dict(event, f"history event {phrase_id}")
            required = {"at", "from", "to", "by", "proposal_id"}
            if (
                set(item) != required
                or item["to"] not in STATES
                or item["from"] not in STATES | {""}
                or not isinstance(item["by"], str)
                or not item["by"]
                or not isinstance(item["proposal_id"], str)
                or not item["proposal_id"]
            ):
                raise LedgerError(f"invalid history event: {phrase_id}")
            item["at"] = _canonical_date(item["at"], f"history.at {phrase_id}")
    ledger.setdefault("retired_ids", [])
    ledger.setdefault("retired_text_hashes", [])
    ledger.setdefault("history", [])
    ledger.setdefault("snapshots", [])
    retired = ledger["retired_ids"]
    if not isinstance(retired, list) or any(not isinstance(item, str) or PHRASE_ID.fullmatch(item) is None for item in retired):
        raise LedgerError("invalid retired_ids")
    if ids.intersection(retired) or len(set(retired)) != len(retired):
        raise LedgerError("active and retired ids must be disjoint")
    for key in ("retired_text_hashes", "history", "snapshots"):
        if not isinstance(ledger.get(key, []), list):
            raise LedgerError(f"invalid top-level {key}")
    if any(not isinstance(value, str) or HEX_HASH.fullmatch(value) is None for value in ledger["retired_text_hashes"]):
        raise LedgerError("invalid retired_text_hashes")
    for snapshot in ledger["snapshots"]:
        item = _require_dict(snapshot, "snapshot")
        old_shape = set(item) == {"at", "source_sha", "content_hash"}
        new_shape = set(item) == {"at", "parent_sha", "tag", "content_hash"}
        if not old_shape and not new_shape:
            raise LedgerError("invalid snapshot record")
        item["at"] = _canonical_date(item["at"], "snapshot.at")
        sha_key = "source_sha" if old_shape else "parent_sha"
        if not isinstance(item[sha_key], str) or re.fullmatch(r"[0-9a-f]{7,64}", item[sha_key]) is None:
            raise LedgerError(f"invalid snapshot {sha_key}")
        if new_shape and (
            not isinstance(item["tag"], str)
            or re.fullmatch(rf"overlay-snap-{re.escape(face)}-\d{{8}}-\d+", item["tag"]) is None
        ):
            raise LedgerError("invalid snapshot tag")
        if not isinstance(item["content_hash"], str) or HEX_HASH.fullmatch(item["content_hash"]) is None:
            raise LedgerError("invalid snapshot content_hash")
    for event in ledger["history"]:
        item = _require_dict(event, "top-level history event")
        if item.get("action") not in {"snapshot", "dedupe", "forget", "eject", "reharvest"}:
            raise LedgerError("unknown top-level history action")
        item["at"] = _canonical_date(item.get("at"), "top-level history.at")
        if item.get("action") == "reharvest" and (
            set(item) != {"at", "action", "predecessor_id", "new_id"}
            or not isinstance(item.get("predecessor_id"), str)
            or PHRASE_ID.fullmatch(item["predecessor_id"]) is None
            or not isinstance(item.get("new_id"), str)
            or PHRASE_ID.fullmatch(item["new_id"]) is None
        ):
            raise LedgerError("invalid reharvest history event")
    return ledger


def load_ledger(path: Path, expected_face: str | None = None) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LedgerError(f"cannot load ledger {path}: {exc}") from exc
    return validate_ledger(value, expected_face)


def dump_ledger(ledger: Mapping[str, object]) -> bytes:
    validated = validate_ledger(copy.deepcopy(dict(ledger)))
    return yaml.safe_dump(
        validated,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    ).encode("utf-8")


def next_phrase_id(ledger: Mapping[str, object]) -> str:
    numbers: list[int] = []
    for phrase in ledger.get("phrases", []):
        match = PHRASE_ID.fullmatch(str(phrase.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    for phrase_id in ledger.get("retired_ids", []):
        match = PHRASE_ID.fullmatch(str(phrase_id))
        if match:
            numbers.append(int(match.group(1)))
    return f"p-{(max(numbers, default=0) + 1):04d}"


def save_ledger(
    ledger: Mapping[str, object],
    rel_path: str,
    writer: Callable[[str, bytes], None],
) -> None:
    writer(rel_path, dump_ledger(ledger))


def new_phrase(phrase_id: str, text: str, source: Mapping[str, object]) -> dict[str, Any]:
    return {
        "id": phrase_id,
        "text": text,
        "state": "candidate",
        "source": {
            "first_seen": str(source["first_seen"]),
            "window_count": int(source["window_count"]),
            "distinct_days": int(source["distinct_days"]),
            "echo_ratio": float(source.get("echo_ratio", 0.0)),
        },
        "staged_at": None,
        "holdout": {key: 0 for key in HOLDOUT_FIELDS},
        "evidence": {
            "uses": 0,
            "last_used_at": None,
            "explicit_mentions": 0,
            "negative_signals": 0,
        },
        "constraints": {"max_per_session": 1},
        "history": [],
    }
