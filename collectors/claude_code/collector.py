"""Nightly, read-only Claude Code transcript observation collector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from collectors.claude_code.filter import filter_user_turn
    from collectors.claude_code.prune import prune
    from collectors.claude_code.scrub import scrub_record
    from collectors.claude_code.usage_log import (
        load_phrases,
        scan_usage,
        write_usage_records,
    )
    from growthlane.ucd_runtime import runtime_status
else:
    from .filter import filter_user_turn
    from .prune import prune
    from .scrub import scrub_record
    from .usage_log import load_phrases, scan_usage, write_usage_records
    from growthlane.ucd_runtime import runtime_status

from growthlane.locking import acquire_lock, contention_message, lock_path, release_lock
from growthlane.notify import Digest


_JST = timezone(timedelta(hours=9))
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "obs-collector.json"
_STAT_KEYS = (
    "files_seen",
    "files_read_errors",
    "files_skipped_denylist",
    "invalid_json_lines",
    "invalid_timestamps",
    "missing_session_id",
    "drop_rule_1_non_user_turn",
    "drop_rule_2_tool_result",
    "drop_rule_3_system_reminder",
    "drop_rule_4_command_tags",
    "drop_rule_5_code_fence",
    "drop_rule_6_path_like",
    "drop_rule_7_length",
    "drop_rule_8_secret_like",
    "drop_rule_9_denylist_session",
    "drop_empty",
    "drop_malformed_user_turn",
    "drop_malformed_content_block",
    "sidechain_turns_skipped",
    "population",
    "scrub_dropped",
    "records",
    "usage_records",
    "usage_invalid_lines",
    "usage_invalid_timestamps",
    "usage_invalid_phrases",
    "pruned",
)
_ERROR_STAT_KEYS = (
    "files_read_errors",
    "invalid_json_lines",
    "invalid_timestamps",
    "missing_session_id",
    "drop_malformed_user_turn",
    "drop_malformed_content_block",
    "usage_invalid_lines",
    "usage_invalid_timestamps",
    "usage_invalid_phrases",
)


def _new_stats() -> dict[str, int]:
    return {key: 0 for key in _STAT_KEYS}


def _load_config(config_path: str | Path | None) -> tuple[dict[str, Any], Path]:
    path = Path(config_path).expanduser() if config_path is not None else _DEFAULT_CONFIG
    with path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("collector config must be a JSON object")
    for key in ("host", "face", "speaker"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"collector config requires non-empty {key!r}")
        if config[key] in {".", ".."} or "/" in config[key] or "\\" in config[key]:
            raise ValueError(f"collector config {key!r} must not contain path components")
    return config, path.resolve()


def _resolve_paths(
    config: dict[str, Any],
    config_path: Path,
    transcripts_root: str | Path | None,
    obs_root: str | Path | None,
) -> tuple[Path, Path, Path]:
    configured_transcripts = config.get("transcripts_root", "~/.claude/projects")
    transcript_path = Path(transcripts_root or configured_transcripts).expanduser()
    configured_obs = config.get("obs_root", "~/.persona-growth-loop")
    environment_obs = os.environ.get("PGL_HOME")
    obs_path = Path(obs_root or environment_obs or configured_obs).expanduser()
    deny_value = config.get("denylist_path", "obs-denylist.txt")
    deny_path = Path(deny_value).expanduser()
    if not deny_path.is_absolute():
        deny_path = config_path.parent / deny_path
    return transcript_path, obs_path, deny_path


def _load_denylist(path: Path) -> list[str]:
    entries: list[str] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = line.strip()
            if value and not value.startswith("#"):
                entries.append(value)
    return entries


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


def _is_denied(project_dir: str, cwd: str | None, denylist: list[str]) -> bool:
    project_casefolded = project_dir.casefold()
    cwd_casefolded = cwd.casefold() if cwd is not None else None
    return any(
        denied.casefold() in project_casefolded
        or (cwd_casefolded is not None and denied.casefold() in cwd_casefolded)
        for denied in denylist
    )


def _project_slug(cwd: str | None, fallback: str) -> str:
    if cwd:
        basename = Path(cwd.rstrip("/\\")).name
        if basename:
            return basename
    return fallback


def _parse_line(line: bytes, stats: dict[str, int]) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        stats["invalid_json_lines"] += 1
        return None
    if not isinstance(value, dict):
        stats["invalid_json_lines"] += 1
        return None
    return value


def _record_for_fragment(
    entry: dict[str, Any],
    text: str,
    timestamp: str,
    session_id: str,
    project: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    # Dict insertion order is the frozen §2.3 JSON key order.
    return {
        "ts": timestamp,
        "host": config["host"],
        "face": config["face"],
        "session": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12],
        "project": project,
        "speaker": config["speaker"],
        "text": text,
        "len": len(text),
        # observation-log-schema §5 closes Tier S, while §2.3 does not declare
        # the Tier L record shape closed; ucd is provenance metadata only.
        "ucd": _runtime_ucd(),
    }


def _append_entry_records(
    entry: dict[str, Any],
    bucket: date,
    cwd: str | None,
    fallback_project: str,
    config: dict[str, Any],
    stats: dict[str, int],
    records: list[dict[str, Any]],
    default_session: str | None,
) -> str | None:
    session_value = entry.get("sessionId")
    if default_session is None and isinstance(session_value, str) and session_value:
        default_session = session_value
    if entry.get("type") != "user":
        filter_user_turn(entry, stats)
        return default_session
    timestamp = _timestamp_in_jst(entry.get("timestamp"))
    if timestamp is None:
        stats["invalid_timestamps"] += 1
        return default_session
    if date.fromisoformat(timestamp[:10]) != bucket:
        return default_session
    session_id = session_value if isinstance(session_value, str) and session_value else default_session
    if session_id is None:
        stats["missing_session_id"] += 1
        return default_session
    for fragment in filter_user_turn(entry, stats):
        raw_record = _record_for_fragment(
            entry,
            fragment,
            timestamp,
            session_id,
            _project_slug(cwd, fallback_project),
            config,
        )
        scrubbed = scrub_record(raw_record)
        if scrubbed is None:
            stats["scrub_dropped"] += 1
        else:
            records.append(scrubbed)
    return default_session


def _collect_transcript_file(
    path: Path,
    bucket: date,
    denylist: list[str],
    config: dict[str, Any],
    stats: dict[str, int],
) -> tuple[bool, list[dict[str, Any]]]:
    stats["files_seen"] += 1
    flattened_project = path.parent.name
    if _is_denied(flattened_project, None, denylist):
        stats["files_skipped_denylist"] += 1
        stats["drop_rule_9_denylist_session"] += 1
        return False, []

    entries: list[dict[str, Any]] = []
    first_cwd: str | None = None
    try:
        with path.open("rb") as stream:
            for line in stream:
                entry = _parse_line(line, stats)
                if entry is None:
                    continue
                entries.append(entry)
                cwd_value = entry.get("cwd")
                if first_cwd is None and isinstance(cwd_value, str) and cwd_value:
                    first_cwd = cwd_value
                if isinstance(cwd_value, str) and cwd_value:
                    if _is_denied(flattened_project, cwd_value, denylist):
                        stats["files_skipped_denylist"] += 1
                        stats["drop_rule_9_denylist_session"] += 1
                        return False, []
    except OSError:
        stats["files_read_errors"] += 1
        return False, []
    default_session: str | None = None
    records: list[dict[str, Any]] = []
    for entry in entries:
        default_session = _append_entry_records(
            entry,
            bucket,
            first_cwd,
            flattened_project,
            config,
            stats,
            records,
            default_session,
        )
    return True, records


def _ensure_private_dirs(obs_root: Path, face: str) -> Path:
    obs_root.mkdir(parents=True, exist_ok=True)
    os.chmod(obs_root, 0o700)
    obslog_root = obs_root / "obslog"
    obslog_root.mkdir(exist_ok=True)
    os.chmod(obslog_root, 0o700)
    if face in {".", ".."} or "/" in face or "\\" in face:
        raise ValueError("collector config 'face' must not contain path components")
    face_dir = obslog_root / face
    face_dir.mkdir(exist_ok=True)
    os.chmod(face_dir, 0o700)
    return face_dir


def _atomic_write_records(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _ensure_private_dir(path: Path) -> Path:
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


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    return flags


def _atomic_write_json(path: Path, value: object) -> None:
    _ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    directory = os.open(path.parent, _directory_open_flags())
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _runtime_ucd() -> str:
    return runtime_status().runtime_version


def _collector_error_count(stats: dict[str, int]) -> int:
    return sum(stats[key] for key in _ERROR_STAT_KEYS)


def _write_last_run_marker(
    obs_root: Path,
    face: str,
    run_date: str,
    run_at: str,
    stats: dict[str, int],
    *,
    usage_enabled: bool,
) -> None:
    path = obs_root.resolve() / "state" / "collector" / f"{face}.last-run.json"
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "face": face,
            "run_at": run_at,
            "date": run_date,
            "sources_scanned": stats["files_seen"],
            "records_written": stats["records"],
            "usage_enabled": usage_enabled,
            "errors": _collector_error_count(stats),
            "ucd": _runtime_ucd(),
        },
    )


def _collect_details(
    run_date: str,
    *,
    config_path: str | Path | None = None,
    transcripts_root: str | Path | None = None,
    obs_root: str | Path | None = None,
) -> tuple[dict[str, int], bool]:
    """Collect one JST date and return stats plus usage enablement."""

    bucket = date.fromisoformat(run_date)
    config, resolved_config = _load_config(config_path)
    transcript_root, resolved_obs_root, denylist_path = _resolve_paths(
        config, resolved_config, transcripts_root, obs_root
    )
    denylist = _load_denylist(denylist_path)
    stats = _new_stats()
    records: list[dict[str, Any]] = []
    allowed_paths: list[Path] = []

    # overlay-contract §10: collectors are detection readers and intentionally
    # continue when KILLSWITCH exists. No gate belongs in this function.
    transcript_paths = sorted(transcript_root.glob("**/*.jsonl")) if transcript_root.is_dir() else []
    for transcript_path in transcript_paths:
        if not transcript_path.is_file():
            continue
        allowed, file_records = _collect_transcript_file(
            transcript_path, bucket, denylist, config, stats
        )
        if allowed:
            allowed_paths.append(transcript_path)
            records.extend(file_records)

    face_dir = _ensure_private_dirs(resolved_obs_root, config["face"])
    output_path = face_dir / f"{run_date}.jsonl"
    _atomic_write_records(output_path, records)
    stats["records"] = len(records)

    usage_enabled, phrases, invalid_phrases = load_phrases(
        resolved_obs_root / "faces", config["face"]
    )
    stats["usage_invalid_phrases"] = invalid_phrases
    usage_output_path = face_dir / f"usage-{run_date}.jsonl"
    if usage_enabled:
        usage_records, usage_stats = scan_usage(
            allowed_paths,
            bucket,
            config["face"],
            phrases,
            report=lambda message: print(message, file=sys.stderr),
        )
        stats.update(
            {
                "files_read_errors": stats["files_read_errors"]
                + usage_stats["files_read_errors"],
                "usage_invalid_lines": usage_stats["usage_invalid_lines"],
                "usage_invalid_timestamps": usage_stats["usage_invalid_timestamps"],
                "usage_records": len(usage_records),
            }
        )
        write_usage_records(usage_output_path, usage_records)
    elif usage_output_path.is_file() or usage_output_path.is_symlink():
        # Absent phrase lists mean no usage output for this bucket. Remove only
        # the current date so a previous enabled run cannot leave stale data.
        usage_output_path.unlink()

    stats["pruned"] = len(prune(resolved_obs_root, bucket))
    return stats, usage_enabled


def collect(
    run_date: str,
    *,
    config_path: str | Path | None = None,
    transcripts_root: str | Path | None = None,
    obs_root: str | Path | None = None,
) -> dict[str, int]:
    """Collect one JST date. The caller supplies the date; no clock is read."""

    stats, _usage_enabled = _collect_details(
        run_date,
        config_path=config_path,
        transcripts_root=transcripts_root,
        obs_root=obs_root,
    )
    return stats


def _write_stats(path: Path, stats: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(stats, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Claude Code observations")
    parser.add_argument("--date", help="JST bucket date (YYYY-MM-DD)")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--transcripts-root")
    parser.add_argument("--stats-json")
    args = parser.parse_args(argv)

    # This is the only wall-clock read. Compute the default bucket exactly once.
    run_date = args.date or (datetime.now(_JST).date() - timedelta(days=1)).isoformat()
    config, resolved_config = _load_config(args.config)
    _, obs_root, _ = _resolve_paths(config, resolved_config, args.transcripts_root, None)
    ucd = runtime_status()
    if ucd.drifted:
        print(
            "[RED] {face}: UCD drift runtime={runtime} corpus={corpus} "
            "direction={direction}; collector run refused before observation write".format(
                face=config["face"],
                runtime=ucd.runtime_version,
                corpus=ucd.corpus_version,
                direction=ucd.direction,
            ),
            file=sys.stderr,
        )
        return 1
    obs_root.mkdir(parents=True, exist_ok=True)
    os.chmod(obs_root, 0o700)
    face = config["face"]
    try:
        acquired_lock = acquire_lock(obs_root, face)
    except OSError as error:
        face_lock_path = lock_path(obs_root, face)
        print(
            f"RUN SKIP date={run_date} lock={face_lock_path} error={error.strerror}",
            file=sys.stderr,
        )
        Digest(obs_root, run_date).emit(
            f"[RED] {face}: collector: skipped: lock acquisition failed "
            f"error={error.strerror}"
        )
        return 1
    if acquired_lock is None:
        face_lock_path = lock_path(obs_root, face)
        print(f"RUN SKIP date={run_date} lock={face_lock_path} error=File exists", file=sys.stderr)
        Digest(obs_root, run_date).emit(contention_message(obs_root, face, "collector"))
        return 1

    try:
        stats, usage_enabled = _collect_details(
            run_date,
            config_path=args.config,
            transcripts_root=args.transcripts_root,
            obs_root=obs_root,
        )
        if args.stats_json:
            _write_stats(Path(args.stats_json).expanduser(), stats)
        _write_last_run_marker(
            obs_root,
            config["face"],
            run_date,
            datetime.now(_JST).replace(microsecond=0).isoformat(),
            stats,
            usage_enabled=usage_enabled,
        )
        print(f"RUN OK date={run_date} records={stats['records']}")
        return 0
    finally:
        if not release_lock(acquired_lock):
            print(f"LOCK CLEANUP FAILED lock={acquired_lock}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
