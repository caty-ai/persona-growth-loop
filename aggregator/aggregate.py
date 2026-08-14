"""Recompute evidence from primary Tier L and usage records."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from classifierd.classify import classify
from growthlane.guard import (
    canonicalize_for_matching,
    matching_contains,
    matching_views,
)
from growthlane.holdout import exposed
from harvester.harvest import append_blocked_text, normalized_hash
from writerd.propose import AdapterError


NEGATIVE_PATTERNS = ("やめて", "言わないで", "気持ち悪い", "らしくない", "真似しないで")
MENTION_PATTERNS = ("その言い方いいね", "って言うの好き")
_HEN_PATTERN = re.compile(r"(?<![大異急激不改])変(?![数更換化動質革形種身装則わえじ])")


def negative_pattern(text: str) -> bool:
    views = matching_views(text)
    return any(
        "変じゃ" in view
        or any(pattern in view for pattern in NEGATIVE_PATTERNS)
        or _HEN_PATTERN.search(view) is not None
        for view in views
    )


class AggregateError(RuntimeError):
    pass


_JST = timezone(timedelta(hours=9))
_TIER_L_KEYS = {"ts", "host", "face", "session", "project", "speaker", "text", "len"}
_TIER_L_OPTIONAL_KEYS = {"ucd"}
_USAGE_KEYS = {"ts", "session", "face", "phrase_id", "state"}
_USAGE_OPTIONAL_KEYS = {"ucd"}


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AggregateError("record timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateError("record timestamp invalid") from exc
    if parsed.tzinfo is None:
        raise AggregateError("record timestamp must be timezone-aware")
    return parsed


def _read_records(obs_dir: Path, prefix: str, start: date, end: date, face: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pattern = f"{prefix}*.jsonl"
    for path in sorted(obs_dir.glob(pattern)):
        if prefix == "" and path.name.startswith("usage-"):
            continue
        if path.is_symlink():
            raise AggregateError(f"symlinked observation file rejected: {path}")
        stem = path.stem[len(prefix) :]
        try:
            bucket = date.fromisoformat(stem)
        except ValueError as exc:
            raise AggregateError(f"invalid observation filename: {path.name}") from exc
        if not start <= bucket <= end:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise AggregateError(f"cannot read observation file: {path}") from exc
        for number, line in enumerate(lines, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AggregateError(f"invalid observation JSON {path}:{number}") from exc
            if not isinstance(item, dict):
                raise AggregateError(f"invalid observation record {path}:{number}")
            if prefix == "usage-":
                keys = set(item)
                if not (_USAGE_KEYS <= keys <= (_USAGE_KEYS | _USAGE_OPTIONAL_KEYS)):
                    raise AggregateError(f"unexpected usage record keys {path}:{number}")
                if item.get("state") not in {"staged", "adopted"}:
                    raise AggregateError(f"unknown usage state {path}:{number}")
                if not all(isinstance(item.get(key), str) for key in ("ts", "session", "face", "phrase_id")):
                    raise AggregateError(f"invalid usage record {path}:{number}")
                if "ucd" in item and not isinstance(item.get("ucd"), str):
                    raise AggregateError(f"invalid usage record {path}:{number}")
            else:
                keys = set(item)
                if not (_TIER_L_KEYS <= keys <= (_TIER_L_KEYS | _TIER_L_OPTIONAL_KEYS)) or any(
                    not isinstance(item.get(key), str) for key in _TIER_L_KEYS - {"len"}
                ) or type(item.get("len")) is not int or item["len"] != len(item["text"]):
                    raise AggregateError(f"invalid Tier L record {path}:{number}")
                if "ucd" in item and not isinstance(item.get("ucd"), str):
                    raise AggregateError(f"invalid Tier L record {path}:{number}")
            if item["face"] != face:
                raise AggregateError(f"record face mismatch {path}:{number}")
            item = dict(item)
            item["_ts"] = _timestamp(item["ts"])
            if item["_ts"].astimezone(_JST).date() != bucket:
                raise AggregateError(f"timestamp bucket mismatch {path}:{number}")
            item["_day"] = bucket.isoformat()
            records.append(item)
    return records


def _transition(phrase: dict[str, Any], to_state: str, run_date: str, proposal_id: str) -> None:
    previous = phrase["state"]
    phrase["state"] = to_state
    if to_state == "staged":
        phrase["staged_at"] = run_date
    phrase["history"].append(
        {"at": run_date, "from": previous, "to": to_state, "by": "applier", "proposal_id": proposal_id}
    )


def distill_identical(
    ledger: dict[str, Any], run_date: str, blocklist: list[str] | None = None
) -> int:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for phrase in ledger["phrases"]:
        if phrase["state"] == "demoted":
            continue
        groups[canonicalize_for_matching(phrase["text"])].append(phrase)
    removed = 0
    for text, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: int(str(item["id"]).split("-", 1)[1]))
        for duplicate in items[1:]:
            if blocklist is not None:
                append_blocked_text(blocklist, duplicate["text"])
            ledger["phrases"].remove(duplicate)
            ledger["retired_ids"].append(duplicate["id"])
            text_hash = normalized_hash(duplicate["text"])
            if text_hash not in ledger["retired_text_hashes"]:
                ledger["retired_text_hashes"].append(text_hash)
            ledger["history"].append(
                {"at": run_date, "action": "dedupe", "retired_id": duplicate["id"], "text_hash": text_hash}
            )
            removed += 1
    return removed


def aggregate(
    pgl_home: Path,
    face: str,
    run_date: str,
    ledger: Mapping[str, object],
    thresholds: Mapping[str, object],
    classifier_argv: object = None,
    blocklist: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, object]], int]:
    result = copy.deepcopy(dict(ledger))
    end = date.fromisoformat(run_date)
    start = end - timedelta(days=max(int(thresholds["window"]), int(thresholds["decay_days"])) - 1)
    obs_dir = pgl_home / "obslog" / face
    observations = _read_records(obs_dir, "", start, end, face)
    usage = _read_records(obs_dir, "usage-", start, end, face)
    by_id = {item["id"]: item for item in result["phrases"]}
    usage_by_phrase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in usage:
        if item["face"] != face or item["phrase_id"] not in by_id:
            continue
        usage_by_phrase[item["phrase_id"]].append(item)

    candidacy_start = end - timedelta(days=int(thresholds["window"]) - 1)
    candidate_observations = [
        item for item in observations if date.fromisoformat(item["_day"]) >= candidacy_start
    ]
    # Echo-ratio v1: an occurrence day is echoed when identical assistant usage
    # is present on that JST day. This is intentionally the frozen day join.
    text_usage_days: dict[str, set[str]] = defaultdict(set)
    for phrase_id, items in usage_by_phrase.items():
        text = canonicalize_for_matching(by_id[phrase_id]["text"])
        text_usage_days[text].update(item["_day"] for item in items)
    for phrase in result["phrases"]:
        text = canonicalize_for_matching(phrase["text"])
        occurrences = [
            item
            for item in candidate_observations
            if canonicalize_for_matching(item["text"]) == text
        ]
        if phrase["state"] == "candidate":
            phrase["source"]["window_count"] = len(occurrences)
            phrase["source"]["distinct_days"] = len({item["_day"] for item in occurrences})
        elif phrase["state"] in {"staged", "adopted"}:
            echoed = sum(item["_day"] in text_usage_days[text] for item in occurrences)
            if occurrences:
                phrase["source"]["echo_ratio"] = echoed / len(occurrences)

    deviations = 0
    for phrase in result["phrases"]:
        if phrase["state"] not in {"staged", "adopted"}:
            continue
        staged_at = date.fromisoformat(str(phrase["staged_at"]))
        relevant_obs = [item for item in observations if date.fromisoformat(item["_day"]) >= staged_at]
        relevant_usage = [item for item in usage_by_phrase[phrase["id"]] if date.fromisoformat(item["_day"]) >= staged_at]
        usage_sessions_by_day: dict[str, set[str]] = defaultdict(set)
        usage_by_session: dict[str, list[datetime]] = defaultdict(list)
        for item in relevant_usage:
            usage_sessions_by_day[item["_day"]].add(item["session"])
            usage_by_session[item["session"]].append(item["_ts"])
        eligible_days = sorted({item["_day"] for item in relevant_obs} | {item["_day"] for item in relevant_usage})
        counters = {key: 0 for key in phrase["holdout"]}
        negative = 0
        mentions = 0
        deviation_sessions: set[tuple[str, str]] = set()
        for day, sessions in usage_sessions_by_day.items():
            if not exposed(face, phrase["id"], day):
                deviations += len(sessions)
                deviation_sessions.update((day, session) for session in sessions)
        deviation_days = {day for day, _session in deviation_sessions}
        for day in eligible_days:
            group = "exposed" if exposed(face, phrase["id"], day) or day in deviation_days else "holdout"
            counters[f"{group}_days"] += 1
        classifier_results: list[dict[str, object]] | None = None
        if isinstance(classifier_argv, (list, tuple)) and classifier_argv:
            payload = {
                "face": face,
                "phrase_id": phrase["id"],
                "phrase_text": phrase["text"],
                "observations": [
                    {
                        "index": index,
                        "text": item["text"],
                        "prior_use": any(
                            ts < item["_ts"] for ts in usage_by_session.get(item["session"], [])
                        ),
                    }
                    for index, item in enumerate(relevant_obs)
                ],
            }
            try:
                classifier_results = classify(classifier_argv, payload, len(relevant_obs))
            except AdapterError as exc:
                # Negative uncertainty is removal-unsafe, so the face aborts. A
                # per-result mention=None remains the contract's 言及なし side.
                raise AggregateError(f"classifier failed closed: {exc}") from exc
        for index, item in enumerate(relevant_obs):
            text = item["text"]
            prior_use = any(ts < item["_ts"] for ts in usage_by_session.get(item["session"], []))
            pattern_negative = negative_pattern(text)
            classified_negative = bool(classifier_results[index]["negative"]) if classifier_results is not None else False
            classified_mention = classifier_results[index]["mention"] is True if classifier_results is not None else False
            is_negative = (pattern_negative or classified_negative) and (
                matching_contains(phrase["text"], text) or prior_use
            )
            is_mention = prior_use and (
                any(matching_contains(pattern, text) for pattern in MENTION_PATTERNS)
                or classified_mention
            )
            group = "exposed" if exposed(face, phrase["id"], item["_day"]) or item["_day"] in deviation_days else "holdout"
            if is_negative:
                counters[f"{group}_neg"] += 1
                negative += 1
            if is_mention:
                counters[f"{group}_mentions"] += 1
                mentions += 1
        phrase["holdout"] = counters
        phrase["evidence"]["uses"] = len(relevant_usage)
        phrase["evidence"]["last_used_at"] = max(
            (item["_day"] for item in relevant_usage),
            default=phrase["evidence"].get("last_used_at"),
        )
        phrase["evidence"]["explicit_mentions"] = mentions
        phrase["evidence"]["negative_signals"] = negative

    distill_identical(result, run_date, blocklist)
    eligible: list[dict[str, object]] = []
    for phrase in result["phrases"]:
        state = phrase["state"]
        if state in {"staged", "adopted"} and phrase["evidence"]["negative_signals"] > 0:
            eligible.append({"phrase_id": phrase["id"], "transition": f"{state}->blocked", "safety": True})
            continue
        if state == "candidate":
            source = phrase["source"]
            if (
                source["window_count"] >= int(thresholds["min_count"])
                and source["distinct_days"] >= int(thresholds["min_days"])
                and float(source["echo_ratio"]) <= float(thresholds["echo_ratio"])
            ):
                eligible.append({"phrase_id": phrase["id"], "transition": "candidate->staged", "safety": False})
        elif state == "staged":
            held = phrase["holdout"]
            evidence = phrase["evidence"]
            age = (end - date.fromisoformat(str(phrase["staged_at"]))).days
            if (
                age >= int(thresholds["staged_min_days"])
                and evidence["uses"] >= int(thresholds["min_uses"])
                and held["exposed_neg"] <= held["holdout_neg"]
                and held["exposed_mentions"] >= held["holdout_mentions"]
                and evidence["negative_signals"] == 0
                and evidence["explicit_mentions"] >= 1
                and float(phrase["source"]["echo_ratio"]) <= float(thresholds["echo_ratio"])
            ):
                eligible.append({"phrase_id": phrase["id"], "transition": "staged->adopted", "safety": False})
        elif state == "adopted":
            last = phrase["evidence"].get("last_used_at")
            if not isinstance(last, str):
                last = next(
                    (
                        event.get("at")
                        for event in reversed(phrase.get("history", []))
                        if isinstance(event, dict) and event.get("to") == "adopted"
                    ),
                    phrase.get("staged_at"),
                )
            if isinstance(last, str) and (end - date.fromisoformat(last)).days >= int(thresholds["decay_days"]):
                eligible.append({"phrase_id": phrase["id"], "transition": "adopted->demoted", "safety": True})
    promotions = sorted(
        [item for item in eligible if item["transition"] == "staged->adopted"],
        key=lambda item: (str(by_id[item["phrase_id"]].get("staged_at", "")), str(item["phrase_id"])),
    )[:2]
    eligible = [item for item in eligible if item["transition"] != "staged->adopted"] + promotions
    return result, eligible, deviations


def apply_transition(ledger: dict[str, Any], phrase_id: str, transition: str, run_date: str, proposal_id: str) -> None:
    phrase = next((item for item in ledger["phrases"] if item["id"] == phrase_id), None)
    if phrase is None:
        raise AggregateError(f"unknown phrase id: {phrase_id}")
    source, separator, target = transition.partition("->")
    if separator != "->" or phrase["state"] != source or target not in {"staged", "adopted", "demoted", "blocked"}:
        raise AggregateError(f"invalid transition: {transition}")
    _transition(phrase, target, run_date, proposal_id)
