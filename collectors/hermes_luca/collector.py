"""Nightly, read-only Hermes Luca observation collector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections import OrderedDict
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from collectors.claude_code.usage_log import load_phrases
    from collectors.hermes_luca.adapters import AdapterError, LocalSQLiteAdapter, SSHAdapter, Turn
    from collectors.hermes_luca.config import ConfigError, LucaConfig, load_config
    from collectors.hermes_luca import journal, ledger, rules, storage
    from collectors.hermes_luca.journal import JournalError, JournalState
    from collectors.hermes_luca.ledger import LedgerError, LedgerState
    from collectors.shared.text_rules_l2 import scrub_record
    from growthlane.guard import matching_views
    from growthlane.locking import acquire_lock, contention_message, lock_path, release_lock
    from growthlane.notify import Digest
    from growthlane.ucd_runtime import runtime_status
else:
    from collectors.claude_code.usage_log import load_phrases
    from collectors.shared.text_rules_l2 import scrub_record
    from growthlane.guard import matching_views
    from growthlane.locking import acquire_lock, contention_message, lock_path, release_lock
    from growthlane.notify import Digest
    from growthlane.ucd_runtime import runtime_status

    from . import journal, ledger, rules, storage
    from .adapters import AdapterError, LocalSQLiteAdapter, SSHAdapter, Turn
    from .config import ConfigError, LucaConfig, load_config
    from .journal import JournalError, JournalState
    from .ledger import LedgerError, LedgerState


_JST = timezone(timedelta(hours=9))
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "obs-collector-luca.json"
_PROJECT = "hermes-luca"
_FACE = "luca"
_HOST = "vps-hermes"
_SPEAKER = "owner"
_OWNER_PLATFORMS = ("telegram", "slack")
# These detection-only markers are a frozen contract surface. Changing them
# requires contract review and emitter-fixture synchronization.
WARMUP_MARKER = "deployment warm-up"
PERSONA_MARKER_PREFIX = "/persona "


def jst_epoch_window(run_date: str | Date) -> tuple[int, int]:
    """Return the inclusive/exclusive JST epoch window for one bucket date."""

    bucket = Date.fromisoformat(run_date) if isinstance(run_date, str) else run_date
    start = datetime.combine(bucket, time.min, tzinfo=_JST)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _resolve_obs_root(config: LucaConfig, override: str | Path | None) -> Path:
    if override is None:
        return config.obs_root
    path = Path(override).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _ensure_private_obs_root(path: Path) -> Path:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        created = False
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            created = True
            mode = os.lstat(current).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(f"private directory has unsafe shape: {current}")
        if created:
            os.chmod(current, 0o700)
    os.chmod(path, 0o700)
    return path


def _load_prefix_denylist(path: Path, extra_prefixes: Iterable[str]) -> tuple[str, ...]:
    prefixes: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            prefixes.append(value)

    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = line.strip()
            if value and not value.startswith("#"):
                add(value)
    for prefix in extra_prefixes:
        add(prefix)
    return tuple(prefixes)


def _hash12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _timestamp_in_jst(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).astimezone(_JST).isoformat()


def _runtime_ucd() -> str:
    status = runtime_status()
    if status.drifted:
        raise ValueError(
            "UCD drift runtime={runtime} corpus={corpus} direction={direction}; "
            "collector run refused before observation write".format(
                runtime=status.runtime_version,
                corpus=status.corpus_version,
                direction=status.direction,
            )
        )
    return status.runtime_version


def _normalize_expected_dm_entries(
    expected_dm_entries: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    if set(expected_dm_entries) != set(_OWNER_PLATFORMS):
        raise ValueError("expected_dm_entries must contain exactly telegram and slack")
    normalized: dict[str, tuple[str, ...]] = {}
    for platform in _OWNER_PLATFORMS:
        values = expected_dm_entries[platform]
        if not values:
            raise ValueError(f"expected_dm_entries.{platform} must be non-empty")
        seen: set[str] = set()
        items: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError(f"expected_dm_entries.{platform} must contain only non-empty strings")
            if value in seen:
                raise ValueError(f"duplicate expected_dm_entries entry: {(platform, value)}")
            seen.add(value)
            items.append(value)
        normalized[platform] = tuple(items)
    return normalized


def _dm_entry_matches(platform: str, entry_id: str, expected_ids: tuple[str, ...]) -> bool:
    if platform == "slack":
        return any(
            entry_id == expected_id or entry_id.startswith(f"{expected_id}:")
            for expected_id in expected_ids
        )
    return entry_id in expected_ids


def _validate_dm_directory(
    expected: dict[str, tuple[str, ...]],
    actual: set[tuple[str, str]],
    checked_platforms: frozenset[str],
) -> None:
    counts: dict[str, int] = {platform: 0 for platform in _OWNER_PLATFORMS}
    for platform, entry_id in actual:
        counts[platform] = counts.get(platform, 0) + 1
        expected_ids = expected.get(platform, ())
        if not _dm_entry_matches(platform, entry_id, expected_ids):
            raise ValueError(
                f"owner directory mismatch: unexpected DM entry {(platform, entry_id)!r}"
            )
    for platform in checked_platforms:
        if counts.get(platform, 0) == 0:
            raise ValueError(
                f"owner directory mismatch: no DM entries found for platform {platform!r}"
            )


def _load_local_owner_fixture(path: str | Path) -> set[tuple[str, str]]:
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"owners fixture not found: {source}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read owners fixture: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"owners fixture is not valid JSON: {source}") from exc

    if isinstance(payload, dict):
        if set(payload) != {"platforms"}:
            raise ValueError("owners fixture object must be {'platforms': {...}}")
        platforms = payload["platforms"]
        if not isinstance(platforms, dict) or set(platforms) != set(_OWNER_PLATFORMS):
            raise ValueError("owners fixture platforms must contain exactly telegram and slack")
        pairs: set[tuple[str, str]] = set()
        for platform in _OWNER_PLATFORMS:
            entries = platforms[platform]
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"owners fixture platforms.{platform} must be a non-empty list")
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"id", "type"}:
                    raise ValueError(f"owners fixture {platform} entries must be {{'id','type'}}")
                if entry["type"] != "dm":
                    raise ValueError(f"owners fixture {platform} entry type must be 'dm'")
                owner_id = entry["id"]
                if not isinstance(owner_id, str) or not owner_id:
                    raise ValueError(f"owners fixture {platform} entry id must be a non-empty string")
                pair = (platform, owner_id)
                if pair in pairs:
                    raise ValueError(f"duplicate owners fixture entry: {pair}")
                pairs.add(pair)
        return pairs

    if isinstance(payload, list):
        pairs = set()
        for index, entry in enumerate(payload, 1):
            if not isinstance(entry, list) or len(entry) != 3:
                raise ValueError(f"owners fixture triple {index} must be [platform, id, type]")
            platform, owner_id, entry_type = entry
            if platform not in _OWNER_PLATFORMS:
                raise ValueError(f"owners fixture triple {index} has unsupported platform: {platform}")
            if not isinstance(owner_id, str) or not owner_id:
                raise ValueError(f"owners fixture triple {index} id must be a non-empty string")
            if entry_type != "dm":
                raise ValueError(f"owners fixture triple {index} type must be 'dm'")
            pair = (platform, owner_id)
            if pair in pairs:
                raise ValueError(f"duplicate owners fixture entry: {pair}")
            pairs.add(pair)
        if not pairs:
            raise ValueError("owners fixture triples must not be empty")
        return pairs

    raise ValueError("owners fixture must be either {'platforms': ...} or a JSON list of triples")


def _validate_local_owner_fixture(
    expected_dm_entries: dict[str, tuple[str, ...]],
    sources: tuple[str, ...],
    owners_json: str | Path | None,
) -> None:
    if owners_json is None:
        raise ValueError("--owners-json is required with --sqlite")
    expected = _normalize_expected_dm_entries(expected_dm_entries)
    checked_platforms = frozenset(source for source in sources if source in _OWNER_PLATFORMS)
    actual = _load_local_owner_fixture(owners_json)
    _validate_dm_directory(expected, actual, checked_platforms)


def _build_user_record(turn: Turn, ucd: str) -> dict[str, object]:
    text = rules.filter_user_text(turn.content)
    if text is None:
        return {}
    return {
        "ts": _timestamp_in_jst(turn.timestamp),
        "host": _HOST,
        "face": _FACE,
        "session": _hash12(turn.session_id),
        "project": _PROJECT,
        "speaker": _SPEAKER,
        "text": text,
        "len": len(text),
        "ucd": ucd,
    }


def _usage_records_for_turn(
    turn: Turn,
    face: str,
    ucd: str,
    phrase_views: tuple[tuple[dict[str, str], tuple[str, ...]], ...],
) -> list[dict[str, str]]:
    assistant_views = tuple(view for view in matching_views(turn.content) if view)
    if not assistant_views:
        return []
    timestamp = _timestamp_in_jst(turn.timestamp)
    session_hash = _hash12(turn.session_id)
    records: list[dict[str, str]] = []
    for phrase, views in phrase_views:
        if views and any(view in assistant_view for view in views for assistant_view in assistant_views):
            records.append(
                {
                    "ts": timestamp,
                    "session": session_hash,
                    "face": face,
                    "phrase_id": phrase["id"],
                    "state": phrase["state"],
                    "ucd": ucd,
                }
            )
    return records


def collect(
    run_date: str | Date,
    *,
    config_path: str | Path = _DEFAULT_CONFIG,
    sqlite_path: str | Path | None = None,
    owners_json: str | Path | None = None,
    obs_root: str | Path | None = None,
) -> dict[str, object]:
    """Collect one JST date and write Luca Tier L observations plus marker."""

    bucket = Date.fromisoformat(run_date) if isinstance(run_date, str) else run_date
    config = load_config(config_path)
    resolved_obs_root = _resolve_obs_root(config, obs_root)
    _ensure_private_obs_root(resolved_obs_root)
    usage_enabled, phrases, invalid_phrases = load_phrases(resolved_obs_root / "faces", config.face)
    denylist_prefixes = _load_prefix_denylist(
        config.denylist_path,
        config.source.exclude_session_prefixes,
    )
    ucd = _runtime_ucd()
    start_epoch, end_epoch = jst_epoch_window(bucket)
    voice_active = config.source.voice_enabled or "api_server" in config.source.sources
    run_at = datetime.now(_JST).replace(microsecond=0).isoformat()

    journal_state: JournalState | None = None
    ledger_state: LedgerState | None = None
    voice_failures: list[str] = []
    if voice_active:
        try:
            journal_state = journal.load_journal(resolved_obs_root)
        except JournalError as exc:
            voice_failures.append(f"journal: {exc}")
        try:
            if config.source.exclude_session_ledger is None:
                raise LedgerError("acceptance ledger path is not configured")
            ledger_state = ledger.load_ledger(
                config.source.exclude_session_ledger,
                resolved_obs_root,
            )
        except LedgerError as exc:
            voice_failures.append(f"ledger: {exc}")
        if ledger_state is not None:
            try:
                ledger.write_line_count(
                    resolved_obs_root,
                    ledger_state.line_count,
                    run_at,
                )
            except (OSError, ValueError) as exc:
                voice_failures.append(f"ledger baseline update: {exc}")

    if sqlite_path is not None:
        adapter: LocalSQLiteAdapter | SSHAdapter = LocalSQLiteAdapter(sqlite_path)
        _validate_local_owner_fixture(
            config.source.expected_dm_entries, config.source.sources, owners_json
        )
    else:
        adapter = SSHAdapter(config.source.ssh_host)
        adapter.validate_owner_directory(config.source.expected_dm_entries, config.source.sources)

    turns = adapter.fetch_turns(start_epoch, end_epoch, config.source.sources)
    indexed_turns = sorted(
        enumerate(turns),
        key=lambda item: (item[1].timestamp, item[1].session_id, item[1].role, item[0]),
    )
    raw_session_count = len({turn.session_id for _, turn in indexed_turns})

    voice_rows_window_excluded = 0
    voice_sessions_ledger_excluded = 0
    voice_fail_closed_rows = 0
    if voice_active and voice_failures:
        voice_fail_closed_rows = sum(
            1 for _, turn in indexed_turns if turn.source == "api_server"
        )
        indexed_turns = [
            item for item in indexed_turns if item[1].source != "api_server"
        ]
    elif voice_active:
        if journal_state is None:
            raise JournalError("intent journal state missing after successful load")
        if ledger_state is None:
            raise LedgerError("acceptance ledger state missing after successful load")
        window_filtered: list[tuple[int, Turn]] = []
        for item in indexed_turns:
            turn = item[1]
            if turn.source == "api_server" and journal_state.excludes(turn.timestamp):
                voice_rows_window_excluded += 1
                continue
            window_filtered.append(item)
        ledger_session_ids = {
            turn.session_id
            for _, turn in window_filtered
            if turn.session_id in ledger_state.session_ids
        }
        voice_sessions_ledger_excluded = len(ledger_session_ids)
        indexed_turns = [
            item
            for item in window_filtered
            if item[1].session_id not in ledger_state.session_ids
        ]

    raw_voice_user_contents = [
        turn.content
        for _, turn in indexed_turns
        if (
            voice_active
            and turn.source == "api_server"
            and turn.role == "user"
            and turn.content is not None
        )
    ]

    session_turns: OrderedDict[str, list[Turn]] = OrderedDict()
    session_allowed: dict[str, bool] = {}
    outsider_sessions = 0
    prefixed_nonvoice_sessions: set[str] = set()

    for _, turn in indexed_turns:
        session_turns.setdefault(turn.session_id, []).append(turn)

    for session_id, bucket_turns in session_turns.items():
        first = bucket_turns[0]
        excluded = rules.session_is_excluded(session_id, first.session_key, denylist_prefixes)
        if excluded:
            if any(turn.source in {"telegram", "slack"} for turn in bucket_turns):
                prefixed_nonvoice_sessions.add(session_id)
            session_allowed[session_id] = False
            continue
        allowed, outsider, _reason = rules.session_attribution(
            bucket_turns,
            config.source.owner_uids,
            config.source.voice_enabled,
        )
        if outsider:
            outsider_sessions += 1
        session_allowed[session_id] = allowed

    phrase_views: tuple[tuple[dict[str, str], tuple[str, ...]], ...] = ()
    if usage_enabled:
        phrase_views = tuple(
            (phrase, tuple(view for view in matching_views(phrase["text"]) if view))
            for phrase in phrases
        )

    records: list[dict[str, object]] = []
    usage_records: list[dict[str, str]] = []
    warmup_marker_count = 0
    persona_marker_count = 0
    for _, turn in indexed_turns:
        if not session_allowed.get(turn.session_id, False):
            continue
        if not turn.content:
            # Real state.db has null and LENGTH(content)=0 assistant rows
            # (tool-call-only turns). They must not raise and must not
            # contribute to either the observation log or the usage log.
            continue
        if turn.role == "user":
            raw_record = _build_user_record(turn, ucd)
            if not raw_record:
                continue
            scrubbed = scrub_record(raw_record)
            if scrubbed is not None:
                records.append(scrubbed)
            continue
        if usage_enabled and phrase_views:
            usage_records.extend(_usage_records_for_turn(turn, config.face, ucd, phrase_views))

    # `/persona ...` is intentionally removed from obslog by the shared
    # path-like filter. Detection runs afterward but compares the retained raw
    # content so that structurally surviving voice markers remain observable.
    warmup_marker_count = sum(
        content == WARMUP_MARKER for content in raw_voice_user_contents
    )
    persona_marker_count = sum(
        content.startswith(PERSONA_MARKER_PREFIX)
        for content in raw_voice_user_contents
    )

    records.sort(key=lambda record: (str(record["ts"]), str(record["session"]), str(record["speaker"])))
    usage_records.sort(
        key=lambda record: (
            record["ts"],
            record["session"],
            record["face"],
            record["phrase_id"],
            record["state"],
            record["ucd"],
        )
    )

    face_dir = resolved_obs_root / "obslog" / config.face
    output_path = face_dir / f"{bucket.isoformat()}.jsonl"
    usage_path = face_dir / f"usage-{bucket.isoformat()}.jsonl"
    storage.write_jsonl(output_path, records)
    if usage_enabled and usage_records:
        storage.write_jsonl(usage_path, usage_records)
    else:
        storage.remove_file(usage_path)

    pruned = storage.prune_luca(resolved_obs_root, bucket)
    # records/usage_records/outsider_sessions/pruned are Luca-specific counts
    # that do not appear in the shared 9-key marker contract (see
    # storage.write_marker's docstring); they stay observable here, through
    # collect()'s return value, instead of leaking a face-only key into that
    # shared schema.
    digest_lines: list[str] = []
    voice_dropped_reason: str | None = None
    if voice_failures:
        voice_dropped_reason = "; ".join(voice_failures)
        digest_lines.append(
            f"[RED] {_FACE}: collector: voice fail-closed; "
            f"api_server rows rejected={voice_fail_closed_rows}; {voice_dropped_reason}"
        )
    elif voice_active and journal_state is not None and journal_state.has_open_window:
        digest_lines.append(
            f"[RED] {_FACE}: collector: unresolved acceptance window; "
            "api_server rows remain excluded until close/resolved; operator follow-up required"
        )
    if warmup_marker_count:
        digest_lines.append(
            f"[RED] {_FACE}: collector: surviving deployment warm-up markers="
            f"{warmup_marker_count}; operator investigation and recovery record required"
        )
    if voice_active and not voice_failures:
        digest_lines.append(
            f"[INFO] {_FACE}: collector: surviving /persona markers={persona_marker_count}"
        )
    if prefixed_nonvoice_sessions:
        digest_lines.append(
            f"[WARN] {_FACE}: collector: telegram/slack prefix-denylisted sessions="
            f"{len(prefixed_nonvoice_sessions)}; route invariant investigation required"
        )

    stats: dict[str, object] = {
        "records": len(records),
        "usage_records": len(usage_records),
        "outsider_sessions": outsider_sessions,
        "pruned": len(pruned),
        "voice_dropped_reason": voice_dropped_reason,
        "voice_rows_window_excluded": voice_rows_window_excluded,
        "voice_sessions_ledger_excluded": voice_sessions_ledger_excluded,
        "persona_marker_count": persona_marker_count,
        "warmup_marker_count": warmup_marker_count,
        "prefix_warn_sessions": len(prefixed_nonvoice_sessions),
        "digest_lines": tuple(digest_lines),
    }
    # sources_scanned = number of distinct sessions the dispatcher returned
    # for this JST bucket, before allow/deny filtering. This is Luca's
    # analogue of the alpha collector's per-transcript-file sources_scanned:
    # both count the raw units the collector touched, not the ones that
    # survived attribution/scrubbing.
    sources_scanned = raw_session_count
    # errors: most failure modes in this collector (config, adapter,
    # owner-directory validation, UCD drift) raise and abort collect() before
    # this point, so they can never reach the marker. But load_phrases() is a
    # deliberate soft-failure boundary: an unparseable overlay-ledger does not
    # raise, it returns invalid_phrases=1 (see
    # collectors/claude_code/usage_log.py::load_phrases) so a corrupt ledger
    # degrades usage-phrase collection instead of aborting the whole run.
    # That soft failure must still surface as a marker error, or the mirror's
    # liveness check (mirror/weekly.py::_collector_liveness, mirrored by
    # collectors/claude_code/collector.py's _ERROR_STAT_KEYS /
    # _collector_error_count on the alpha face) would read a broken ledger
    # night as healthy. invalid_phrases is the only such soft-failure count
    # load_phrases/collect() produce today -- every other helper collect()
    # calls (rules.session_is_excluded, rules.session_attribution,
    # rules.filter_user_text, the adapters, scrub_record, matching_views)
    # either raises on failure or returns pure classification data, not an
    # error tally, so there is nothing else to add here.
    errors = invalid_phrases
    storage.write_marker(
        resolved_obs_root,
        run_at=run_at,
        run_date=bucket,
        sources_scanned=sources_scanned,
        records_written=len(records),
        errors=errors,
        usage_enabled=usage_enabled,
        ucd=ucd,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Hermes Luca observations")
    parser.add_argument(
        "--date",
        help="JST bucket date (YYYY-MM-DD); defaults to yesterday in JST",
    )
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--sqlite", help="Read from a local SQLite file instead of SSH")
    parser.add_argument(
        "--owners-json",
        help="Owner directory fixture JSON (required with --sqlite)",
    )
    parser.add_argument("--obs-root", help="Override PGL observation root")
    args = parser.parse_args(argv)

    run_date = args.date or (datetime.now(_JST).date() - timedelta(days=1)).isoformat()

    try:
        config = load_config(args.config)
        resolved_obs_root = _resolve_obs_root(config, args.obs_root)
        _ensure_private_obs_root(resolved_obs_root)
    except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[RED] {_FACE}: collector: {exc}", file=sys.stderr)
        return 1

    try:
        acquired_lock = acquire_lock(resolved_obs_root, config.face)
    except OSError as exc:
        Digest(resolved_obs_root, run_date).emit(
            f"[RED] {_FACE}: collector: skipped: lock acquisition failed "
            f"error={exc.strerror or str(exc)}"
        )
        print(f"[RED] {_FACE}: collector: lock acquisition failed: {exc}", file=sys.stderr)
        return 1
    if acquired_lock is None:
        Digest(resolved_obs_root, run_date).emit(contention_message(resolved_obs_root, config.face, "collector"))
        return 1

    try:
        stats = collect(
            run_date,
            config_path=args.config,
            sqlite_path=args.sqlite,
            owners_json=args.owners_json,
            obs_root=resolved_obs_root,
        )
    except (ConfigError, AdapterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[RED] {_FACE}: collector: {exc}", file=sys.stderr)
        return 1
    finally:
        if not release_lock(acquired_lock):
            print(f"[RED] {_FACE}: collector: lock cleanup failed: {lock_path(resolved_obs_root, config.face)}", file=sys.stderr)

    if stats["outsider_sessions"] > 0:
        print(
            f"[RED] {_FACE}: collector: outsider sessions rejected={stats['outsider_sessions']}",
            file=sys.stderr,
        )
    digest = Digest(resolved_obs_root, run_date)
    for line in stats["digest_lines"]:
        digest.emit(line)
        if line.startswith("[RED]"):
            print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
