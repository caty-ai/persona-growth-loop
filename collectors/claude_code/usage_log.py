"""Minimal assistant phrase-usage scanner for observation schema §4."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from growthlane.ledger import LedgerError, load_ledger
except ImportError:  # pragma: no cover - direct collector script compatibility
    LedgerError = ValueError  # type: ignore[misc,assignment]
    load_ledger = None  # type: ignore[assignment]


_JST = timezone(timedelta(hours=9))


def _matching_views_cached(
    text: str,
    cache: dict[str, tuple[str, ...]],
    canonicalize: Callable[[str], tuple[str, ...]],
) -> tuple[str, ...]:
    """Memoize the exact canonical result by immutable raw-text value."""

    try:
        return cache[text]
    except KeyError:
        views = canonicalize(text)
        cache[text] = views
        return views


def _open_transcript_nofollow(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "transcript is not a regular file", str(path))
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _is_nofollow_error(exc: OSError) -> bool:
    return exc.errno in {errno.ELOOP, errno.EMLINK}


def _timestamp_in_jst(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_JST).isoformat()


def load_phrases(faces_root: str | Path, face: str) -> tuple[bool, list[dict[str, str]], int]:
    """Load staged/adopted phrases from schema-v1 ledger, all-or-nothing."""

    face_dir = Path(faces_root).expanduser() / face
    candidates = (face_dir / "overlay-ledger.yml", face_dir / "growth" / "overlay-ledger.yml")
    ledger_path = next((path for path in candidates if path.is_file()), None)
    if ledger_path is None or load_ledger is None:
        return False, [], 0
    try:
        ledger = load_ledger(ledger_path, face)
    except LedgerError:
        return False, [], 1
    phrases = [
        {"id": item["id"], "text": item["text"], "state": item["state"]}
        for item in ledger["phrases"]
        if item["state"] in {"staged", "adopted"}
    ]
    return True, phrases, 0


def scan_usage(
    transcript_paths: list[Path],
    run_date: str | date,
    face: str,
    phrases: list[dict[str, str]],
    report: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Scan assistant text blocks and retain only phrase-match metadata."""

    from growthlane.guard import matching_views

    bucket = date.fromisoformat(run_date) if isinstance(run_date, str) else run_date
    phrase_views = {
        phrase["id"]: tuple(view for view in matching_views(phrase["text"]) if view)
        for phrase in phrases
    }
    assistant_view_cache: dict[str, tuple[str, ...]] = {}
    records: list[dict[str, str]] = []
    stats = {
        "usage_invalid_lines": 0,
        "usage_invalid_timestamps": 0,
        "files_read_errors": 0,
    }
    # Persist the runtime UCD version beside each usage match so downstream
    # readers can reason about admission-era matching semantics without
    # widening Tier S or retaining extra transcript content.
    provenance = unicodedata.unidata_version

    for path in sorted(transcript_paths):
        default_session: str | None = None
        file_records: list[dict[str, str]] = []
        try:
            with _open_transcript_nofollow(path) as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        stats["usage_invalid_lines"] += 1
                        continue
                    if not isinstance(entry, dict):
                        stats["usage_invalid_lines"] += 1
                        continue
                    session_value = entry.get("sessionId")
                    if default_session is None and isinstance(session_value, str) and session_value:
                        default_session = session_value
                    if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
                        continue
                    timestamp = _timestamp_in_jst(entry.get("timestamp"))
                    if timestamp is None:
                        stats["usage_invalid_timestamps"] += 1
                        continue
                    if date.fromisoformat(timestamp[:10]) != bucket:
                        continue
                    session_id = session_value if isinstance(session_value, str) and session_value else default_session
                    if session_id is None:
                        continue
                    message = entry.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        assistant_text = block.get("text")
                        if not isinstance(assistant_text, str):
                            continue
                        assistant_views = _matching_views_cached(
                            assistant_text, assistant_view_cache, matching_views
                        )
                        for phrase in phrases:
                            if any(
                                phrase_view in assistant_view
                                for phrase_view in phrase_views[phrase["id"]]
                                for assistant_view in assistant_views
                            ):
                                # Never add assistant text or context to this schema.
                                file_records.append(
                                    {
                                        "ts": timestamp,
                                        "session": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12],
                                        "face": face,
                                        "phrase_id": phrase["id"],
                                        "state": phrase["state"],
                                        "ucd": provenance,
                                    }
                                )
        except OSError as exc:
            if _is_nofollow_error(exc):
                stats["files_read_errors"] += 1
                if report is not None:
                    report(f"transcript skipped: symlinked file: {path}")
            else:
                stats["files_read_errors"] += 1
                if report is not None:
                    report(f"transcript skipped: unreadable file during usage scan: {path}: {exc}")
            continue
        records.extend(file_records)
    return records, stats


def assistant_echo_days(
    transcript_paths: list[Path],
    start: date,
    end: date,
    phrases: list[str],
    report: Callable[[str], None] | None = None,
) -> dict[str, set[str]]:
    """Return only per-phrase assistant-match days; never retain transcript text."""

    from growthlane.guard import matching_views

    normalized_phrases = {
        phrase: tuple(view for view in matching_views(phrase) if view)
        for phrase in phrases
    }
    matches = {phrase: set() for phrase in phrases}
    readable_files = 0
    for path in sorted(transcript_paths):
        file_matches = {phrase: set() for phrase in phrases}
        try:
            with _open_transcript_nofollow(path) as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if (
                        not isinstance(entry, dict)
                        or entry.get("type") != "assistant"
                        or entry.get("isSidechain") is True
                    ):
                        continue
                    timestamp = _timestamp_in_jst(entry.get("timestamp"))
                    if timestamp is None:
                        continue
                    bucket = date.fromisoformat(timestamp[:10])
                    if bucket < start or bucket > end:
                        continue
                    message = entry.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        raw_text = block.get("text")
                        if not isinstance(raw_text, str):
                            continue
                        text_views = matching_views(raw_text)
                        for phrase, phrase_views in normalized_phrases.items():
                            if any(
                                phrase_view in text_view
                                for phrase_view in phrase_views
                                for text_view in text_views
                            ):
                                file_matches[phrase].add(bucket.isoformat())
        except OSError as exc:
            if report is not None:
                if _is_nofollow_error(exc):
                    report(f"transcript skipped: symlinked file: {path}")
                else:
                    report(f"transcript skipped: unreadable file during echo scan: {path}: {exc}")
            continue
        readable_files += 1
        for phrase in phrases:
            matches[phrase].update(file_matches[phrase])
    if readable_files == 0:
        raise OSError("zero transcript files were readable during echo scan")
    return matches


def write_usage_records(path: Path, records: list[dict[str, str]]) -> None:
    """Atomically rewrite a usage JSONL file with Tier L permissions."""

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
