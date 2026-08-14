"""Harvest deterministic new-candidate proposals from Tier L."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from collectors.claude_code.usage_log import assistant_echo_days
from growthlane.guard import (
    canonicalize_for_matching,
    canonicalize_for_storage,
    lint_phrase,
    matching_views,
)
from growthlane.holdout import proposal_id
from growthlane.ledger import next_phrase_id


class HarvestError(RuntimeError):
    pass


class TranscriptUnavailable(HarvestError):
    pass


_JST = timezone(timedelta(hours=9))
_TIER_L_KEYS = {"ts", "host", "face", "session", "project", "speaker", "text", "len"}
_TIER_L_OPTIONAL_KEYS = frozenset({"ucd"})
_MASKED_PII_MARKERS = ("***@***", "0**-****-****")


def matching_bucket(text: str) -> frozenset[str]:
    """Return every matching-only view used for blocklist comparison."""

    return frozenset(matching_views(text))


def normalize_blocklist(lines: Iterable[str]) -> list[str]:
    """Normalize, bucket matching-equivalent lines, and keep the cleanest."""

    groups: list[tuple[set[str], list[str]]] = []
    for line in lines:
        stored = canonicalize_for_storage(line)
        if not stored:
            continue
        views = set(matching_bucket(stored))
        overlaps = [
            index for index, (group_views, _items) in enumerate(groups)
            if not views.isdisjoint(group_views)
        ]
        if not overlaps:
            groups.append((views, [stored]))
            continue
        first = overlaps[0]
        merged_views = set(views)
        merged_items = [stored]
        for index in reversed(overlaps):
            group_views, items = groups.pop(index)
            merged_views.update(group_views)
            merged_items.extend(items)
        groups.insert(first, (merged_views, merged_items))

    def cleanliness_key(text: str) -> tuple[int, str]:
        penalty = sum(
            unicodedata.normalize("NFKC", char) != char
            or unicodedata.category(char).startswith("C")
            for char in text
        )
        return penalty, text

    return [min(items, key=cleanliness_key) for _views, items in groups]


def append_blocked_text(blocklist: list[str], text: str) -> None:
    stored = canonicalize_for_storage(text)
    if not stored:
        return
    blocklist[:] = normalize_blocklist((*blocklist, stored))


def normalized_hash(text: str) -> str:
    matching_text = canonicalize_for_matching(text)
    return hashlib.sha256(matching_text.encode("utf-8")).hexdigest()


def _read_blocklist(path: Path) -> set[str]:
    try:
        return {
            view
            for line in normalize_blocklist(path.read_text(encoding="utf-8").splitlines())
            for view in matching_bucket(line)
        }
    except (OSError, UnicodeError) as exc:
        raise HarvestError(f"missing or invalid blocklist: {path}") from exc


def transcript_inputs(
    config: Mapping[str, object], report: Callable[[str], None] | None = None
) -> tuple[list[Path], str | None]:
    """Resolve transcript candidates without a separate readability probe."""

    value = config.get("transcripts_root")
    if not isinstance(value, str) or not value.strip():
        return [], "transcript root is not configured"
    root = Path(value).expanduser()
    try:
        if not root.is_dir():
            return [], f"transcript root is missing: {root}"
        directories = [root]
        paths: list[Path] = []
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(path)
                    elif entry.name.endswith(".jsonl") and (
                        entry.is_file(follow_symlinks=False) or entry.is_symlink()
                    ):
                        paths.append(path)
        paths.sort()
        if not paths:
            return [], f"transcript root yielded zero readable transcript files: {root}"
    except OSError as exc:
        return [], f"transcript source is unreadable: {exc}"
    return paths, None


def _tier_l_records(obs_dir: Path, start: date, end: date, face: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(obs_dir.glob("*.jsonl")):
        if path.name.startswith("usage-"):
            continue
        if path.is_symlink():
            raise HarvestError(f"symlinked Tier L file rejected: {path}")
        try:
            bucket = date.fromisoformat(path.stem)
        except ValueError:
            raise HarvestError(f"unexpected Tier L filename: {path.name}")
        if bucket < start or bucket > end:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise HarvestError(f"cannot read Tier L file: {path}") from exc
        for number, line in enumerate(lines, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HarvestError(f"invalid Tier L JSON {path}:{number}") from exc
            if not isinstance(item, dict):
                raise HarvestError(f"invalid Tier L record {path}:{number}")
            keys = set(item)
            if (
                not _TIER_L_KEYS.issubset(keys)
                or not keys.issubset(_TIER_L_KEYS | _TIER_L_OPTIONAL_KEYS)
                or any(
                not isinstance(item.get(key), str) for key in _TIER_L_KEYS - {"len"}
                )
                or type(item.get("len")) is not int
                or item["len"] != len(item["text"])
                or ("ucd" in item and not isinstance(item.get("ucd"), str))
            ):
                raise HarvestError(f"invalid Tier L record fields {path}:{number}")
            if item["face"] != face:
                raise HarvestError(f"Tier L face mismatch {path}:{number}")
            try:
                timestamp = datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise HarvestError(f"invalid Tier L timestamp {path}:{number}") from exc
            if timestamp.tzinfo is None or timestamp.astimezone(_JST).date() != bucket:
                raise HarvestError(f"Tier L timestamp bucket mismatch {path}:{number}")
            item = dict(item)
            item["_day"] = bucket.isoformat()
            records.append(item)
    return records


def harvest(
    pgl_home: Path,
    face: str,
    run_date: str,
    config: Mapping[str, object],
    ledger: Mapping[str, object],
    thresholds: Mapping[str, object],
    blocklist_path: Path,
    transcript_paths: list[Path] | None = None,
    transcript_report: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    if transcript_paths is None:
        raise HarvestError("transcript_paths must not be None")
    speaker = config.get("speaker")
    if not isinstance(speaker, str) or not speaker:
        raise HarvestError("face config requires speaker")
    end = date.fromisoformat(run_date)
    start = end - timedelta(days=int(thresholds["window"]) - 1)
    records = _tier_l_records(pgl_home / "obslog" / face, start, end, face)
    blocked = _read_blocklist(blocklist_path)
    existing = {
        canonicalize_for_matching(str(item["text"]))
        for item in ledger["phrases"]
        if isinstance(item, dict) and item.get("state") != "demoted"
    }
    demoted_by_text = {
        canonicalize_for_matching(str(item["text"])): str(item["id"])
        for item in ledger["phrases"]
        if isinstance(item, dict) and item.get("state") == "demoted"
    }
    retired_hashes = set(str(item) for item in ledger.get("retired_text_hashes", []))
    days: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    variant_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    first_seen: dict[str, str] = {}
    for item in records:
        if item["speaker"] != speaker:
            continue
        stored = canonicalize_for_storage(item["text"])
        text_key = canonicalize_for_matching(item["text"])
        if not stored or not text_key or lint_phrase(item["text"]):
            continue
        if any(marker in item["text"] for marker in _MASKED_PII_MARKERS):
            continue
        if (
            not matching_bucket(item["text"]).isdisjoint(blocked)
            or text_key in existing
            or normalized_hash(item["text"]) in retired_hashes
        ):
            continue
        counts[text_key] += 1
        variant_counts[text_key][stored] += 1
        days[text_key].add(item["_day"])
        first_seen[text_key] = min(
            first_seen.get(text_key, item["_day"]), item["_day"]
        )
    representatives = {
        text_key: min(
            variants,
            key=lambda stored: (-variants[stored], stored),
        )
        for text_key, variants in variant_counts.items()
    }
    eligible = [
        text_key
        for text_key in counts
        if counts[text_key] >= int(thresholds["min_count"])
        and len(days[text_key]) >= int(thresholds["min_days"])
    ]
    eligible.sort(
        key=lambda text_key: (
            -counts[text_key],
            first_seen[text_key],
            representatives[text_key],
            text_key,
        )
    )
    eligible_texts = [representatives[text_key] for text_key in eligible]
    try:
        echoed_days = assistant_echo_days(
            transcript_paths, start, end, eligible_texts, transcript_report
        )
    except OSError as exc:
        raise TranscriptUnavailable(str(exc)) from exc
    proposals: list[dict[str, object]] = []
    scratch = {"phrases": list(ledger["phrases"]), "retired_ids": list(ledger.get("retired_ids", []))}
    for text_key in eligible[:3]:
        text = representatives[text_key]
        phrase_id = next_phrase_id(scratch)
        proposals.append(
            {
                "face": face,
                "phrase_id": phrase_id,
                "transition": "->candidate",
                "text": text,
                "source": {
                    "first_seen": first_seen[text_key],
                    "window_count": counts[text_key],
                    "distinct_days": len(days[text_key]),
                    "echo_ratio": sum(
                        1 for item in records
                        if item["speaker"] == speaker
                        and canonicalize_for_matching(item["text"]) == text_key
                        and item["_day"] in echoed_days[text]
                    ) / counts[text_key],
                },
                "proposal_id": proposal_id(face, phrase_id, "->candidate", run_date),
                "predecessor_id": demoted_by_text.get(text_key),
            }
        )
        scratch["phrases"].append({"id": phrase_id})
    return proposals
