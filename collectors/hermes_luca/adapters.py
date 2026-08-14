from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


ALLOWED_SOURCES = frozenset({"telegram", "slack", "api_server"})
ALLOWED_ROLES = frozenset({"user", "assistant"})
ALLOWED_OWNER_PLATFORMS = frozenset({"telegram", "slack"})


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class Turn:
    session_id: str
    source: str
    chat_type: str | None
    user_id: str | None
    session_key: str | None
    content: str | None
    timestamp: float
    role: str


class LocalSQLiteAdapter:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()

    def fetch_turns(
        self,
        start_epoch: int | float,
        end_epoch: int | float,
        sources: Iterable[str],
    ) -> list[Turn]:
        start_value, end_value = _validate_epoch_window(start_epoch, end_epoch)
        source_values = _normalize_sources(sources)
        placeholders = ",".join("?" for _ in source_values)
        query = f"""
            SELECT
                s.id AS session_id,
                s.source AS source,
                s.chat_type AS chat_type,
                s.user_id AS user_id,
                s.session_key AS session_key,
                m.content AS content,
                m.timestamp AS timestamp,
                m.role AS role,
                m.id AS message_id
            FROM sessions AS s
            JOIN messages AS m ON m.session_id = s.id
            WHERE m.role IN ('user', 'assistant')
              AND m.timestamp >= ?
              AND m.timestamp < ?
              AND s.source IN ({placeholders})
            ORDER BY m.timestamp ASC, s.id ASC, m.id ASC
        """
        try:
            with closing(sqlite3.connect(self._connection_uri(), uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, [start_value, end_value, *source_values]).fetchall()
        except sqlite3.Error as exc:
            raise AdapterError(f"sqlite schema/query failure: {exc}") from exc
        turns: list[Turn] = []
        for row in rows:
            turns.append(_row_to_turn(row))
        return turns

    def _connection_uri(self) -> str:
        return f"file:{quote(str(self._db_path), safe='/')}?mode=ro"


class SSHAdapter:
    def __init__(self, ssh_host: str) -> None:
        if not isinstance(ssh_host, str) or not ssh_host:
            raise AdapterError("ssh_host must be a non-empty string")
        if ssh_host.startswith("-") or any(character.isspace() for character in ssh_host):
            raise AdapterError("ssh_host must be a host alias, not an ssh option")
        self._ssh_host = ssh_host

    def load_owner_directory(self) -> set[tuple[str, str]]:
        payload = self._run_command("read-owners")
        text = _decode_payload(payload, "read-owners")
        if not text.strip():
            raise AdapterError("read-owners returned empty output")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return _parse_owner_jsonl(text)
        if isinstance(parsed, list):
            # This is the real pgl-luca-dispatch `read-owners` shape: a single
            # JSON array of {"platform","id","type"} objects, dm-only,
            # sorted by (platform, id). The {"platforms": {...}} object form
            # below stays supported for the raw channel_directory.json fixture
            # shape and local tooling.
            return _parse_owner_array(parsed)
        return _parse_owner_document(parsed)

    def validate_owner_directory(
        self,
        expected_dm_entries: Mapping[str, Iterable[str]],
        sources: Iterable[str],
    ) -> None:
        expected = _normalize_expected_dm_entries(expected_dm_entries)
        checked_platforms = _normalize_dm_checked_platforms(sources)
        actual = self.load_owner_directory()
        _validate_dm_directory(expected, actual, checked_platforms)

    def fetch_turns(
        self,
        start_epoch: int | float,
        end_epoch: int | float,
        sources: Iterable[str],
    ) -> list[Turn]:
        start_value, end_value = _validate_epoch_window(start_epoch, end_epoch)
        source_values = _normalize_sources(sources)
        payload = self._run_command(
            "read-sessions",
            str(start_value),
            str(end_value),
        )
        records = _parse_session_stream(payload)
        turns = [_mapping_to_turn(record) for record in records]
        return [turn for turn in turns if turn.source in source_values]

    def _run_command(self, *argv: str) -> bytes:
        try:
            completed = subprocess.run(
                ["ssh", self._ssh_host, *argv],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                check=False,
            )
        except OSError as exc:
            raise AdapterError(f"{argv[0]} execution failed: {exc}") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise AdapterError(f"{argv[0]} exited {completed.returncode}: {stderr}")
        return completed.stdout


def _row_to_turn(row: sqlite3.Row) -> Turn:
    required_columns = {
        "session_id",
        "source",
        "chat_type",
        "user_id",
        "session_key",
        "content",
        "timestamp",
        "role",
        "message_id",
    }
    missing = sorted(required_columns - set(row.keys()))
    if missing:
        raise AdapterError(f"sqlite row is missing columns: {', '.join(missing)}")
    return _turn_from_fields(
        session_id=row["session_id"],
        source=row["source"],
        chat_type=row["chat_type"],
        user_id=row["user_id"],
        session_key=row["session_key"],
        content=row["content"],
        timestamp=row["timestamp"],
        role=row["role"],
    )


def _parse_owner_document(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, dict) or set(value) != {"platforms"}:
        raise AdapterError("read-owners JSON must be {'platforms': {...}}")
    platforms = value["platforms"]
    if not isinstance(platforms, dict) or not platforms:
        raise AdapterError("read-owners platforms must be a non-empty object")
    actual: set[tuple[str, str]] = set()
    for platform, entries in platforms.items():
        if platform not in ALLOWED_OWNER_PLATFORMS:
            raise AdapterError(f"read-owners contains unsupported platform: {platform}")
        if not isinstance(entries, list):
            raise AdapterError(f"read-owners platform {platform} must be a list")
        for entry in entries:
            pair = _parse_owner_entry(entry, platform=platform)
            if pair in actual:
                raise AdapterError(f"duplicate owner directory entry: {pair}")
            actual.add(pair)
    return actual


def _parse_owner_array(value: list[Any]) -> set[tuple[str, str]]:
    actual: set[tuple[str, str]] = set()
    for index, entry in enumerate(value, 1):
        if not isinstance(entry, dict) or set(entry) != {"platform", "id", "type"}:
            raise AdapterError(f"invalid read-owners array entry at index {index}")
        pair = _parse_owner_entry(entry, platform=entry["platform"])
        if pair in actual:
            raise AdapterError(f"duplicate owner directory entry at index {index}: {pair}")
        actual.add(pair)
    if not actual:
        raise AdapterError("read-owners array returned no DM entries")
    return actual


def _parse_owner_jsonl(text: str) -> set[tuple[str, str]]:
    actual: set[tuple[str, str]] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"invalid read-owners JSONL at line {number}") from exc
        if not isinstance(entry, dict) or set(entry) != {"platform", "id", "type"}:
            raise AdapterError(f"invalid read-owners JSONL entry at line {number}")
        pair = _parse_owner_entry(entry, platform=entry["platform"])
        if pair in actual:
            raise AdapterError(f"duplicate owner directory entry at line {number}: {pair}")
        actual.add(pair)
    if not actual:
        raise AdapterError("read-owners JSONL returned no DM entries")
    return actual


def _parse_owner_entry(entry: Mapping[str, Any], *, platform: str) -> tuple[str, str]:
    if platform not in ALLOWED_OWNER_PLATFORMS:
        raise AdapterError(f"unsupported owner platform: {platform}")
    entry_type = entry.get("type")
    if entry_type != "dm":
        raise AdapterError(f"owner entry must have type 'dm': {platform}")
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise AdapterError(f"owner entry id must be a non-empty string: {platform}")
    return (platform, entry_id)


def _parse_session_stream(payload: bytes) -> list[Mapping[str, Any]]:
    text = _decode_payload(payload, "read-sessions")
    if not text.strip():
        return []
    return _parse_session_jsonl(text)


def _parse_session_jsonl(text: str) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"invalid read-sessions JSONL at line {number}") from exc
        records.append(_validate_session_record(entry, index=number))
    return records


def _validate_session_record(value: Any, *, index: int) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AdapterError(f"read-sessions record {index} must be an object")
    allowed_keys = {
        "session_id",
        "source",
        "chat_type",
        "user_id",
        "session_key",
        "content",
        "timestamp",
        "role",
    }
    required_keys = set(allowed_keys)
    actual_keys = set(value)
    unknown = sorted(actual_keys - allowed_keys)
    missing = sorted(required_keys - actual_keys)
    if unknown:
        raise AdapterError(f"read-sessions record {index} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise AdapterError(f"read-sessions record {index} is missing keys: {', '.join(missing)}")
    return value


def _mapping_to_turn(value: Mapping[str, Any]) -> Turn:
    return _turn_from_fields(
        session_id=value["session_id"],
        source=value["source"],
        chat_type=value["chat_type"],
        user_id=value["user_id"],
        session_key=value["session_key"],
        content=value["content"],
        timestamp=value["timestamp"],
        role=value["role"],
    )


def _turn_from_fields(
    *,
    session_id: Any,
    source: Any,
    chat_type: Any,
    user_id: Any,
    session_key: Any,
    content: Any,
    timestamp: Any,
    role: Any,
) -> Turn:
    normalized_source = _require_nonempty_string(source, "source")
    if normalized_source not in ALLOWED_SOURCES:
        raise AdapterError(f"unsupported source: {normalized_source}")
    normalized_role = _require_nonempty_string(role, "role")
    if normalized_role not in ALLOWED_ROLES:
        raise AdapterError(f"unsupported role: {normalized_role}")
    normalized_user_id = _require_optional_string(user_id, "user_id")
    normalized_session_id = _normalize_session_id(session_id)
    normalized_chat_type = _require_optional_string(chat_type, "chat_type")
    normalized_session_key = _require_optional_string(session_key, "session_key")
    normalized_content = _normalize_content(content)
    normalized_timestamp = _normalize_timestamp(timestamp)
    return Turn(
        session_id=normalized_session_id,
        source=normalized_source,
        chat_type=normalized_chat_type,
        user_id=normalized_user_id,
        session_key=normalized_session_key,
        content=normalized_content,
        timestamp=normalized_timestamp,
        role=normalized_role,
    )


def _validate_epoch_window(start_epoch: int | float, end_epoch: int | float) -> tuple[int, int]:
    start_value = _normalize_epoch_argument(start_epoch, "start_epoch")
    end_value = _normalize_epoch_argument(end_epoch, "end_epoch")
    if end_value <= start_value:
        raise AdapterError("end_epoch must be greater than start_epoch")
    return start_value, end_value


def _normalize_sources(sources: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sources)
    if not values:
        raise AdapterError("sources must be a non-empty iterable")
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        source = _require_nonempty_string(value, "sources entry")
        if source not in ALLOWED_SOURCES:
            raise AdapterError(f"unsupported source filter: {source}")
        if source in seen:
            raise AdapterError(f"duplicate source filter: {source}")
        seen.add(source)
        normalized.append(source)
    return tuple(normalized)


def _normalize_expected_dm_entries(
    expected_dm_entries: Mapping[str, Iterable[str]]
) -> dict[str, tuple[str, ...]]:
    if set(expected_dm_entries) != ALLOWED_OWNER_PLATFORMS:
        raise AdapterError("expected_dm_entries must contain exactly telegram and slack")
    normalized: dict[str, tuple[str, ...]] = {}
    for platform, entries in expected_dm_entries.items():
        if platform not in ALLOWED_OWNER_PLATFORMS:
            raise AdapterError(f"unsupported expected_dm_entries platform: {platform}")
        values = list(entries)
        if not values:
            raise AdapterError(f"expected_dm_entries.{platform} must be non-empty")
        seen: set[str] = set()
        items: list[str] = []
        for value in values:
            entry_id = _require_nonempty_string(value, f"expected_dm_entries.{platform}")
            if entry_id in seen:
                raise AdapterError(f"duplicate expected_dm_entries entry: {(platform, entry_id)}")
            seen.add(entry_id)
            items.append(entry_id)
        normalized[platform] = tuple(items)
    return normalized


def _normalize_dm_checked_platforms(sources: Iterable[str]) -> frozenset[str]:
    return frozenset(source for source in sources if source in ALLOWED_OWNER_PLATFORMS)


def _dm_entry_matches(platform: str, entry_id: str, expected_ids: tuple[str, ...]) -> bool:
    if platform == "slack":
        return any(
            entry_id == expected_id or entry_id.startswith(f"{expected_id}:")
            for expected_id in expected_ids
        )
    return entry_id in expected_ids


def _validate_dm_directory(
    expected: Mapping[str, tuple[str, ...]],
    actual: set[tuple[str, str]],
    checked_platforms: Iterable[str],
) -> None:
    counts: dict[str, int] = {platform: 0 for platform in ALLOWED_OWNER_PLATFORMS}
    for platform, entry_id in actual:
        counts[platform] = counts.get(platform, 0) + 1
        expected_ids = expected.get(platform, ())
        if not _dm_entry_matches(platform, entry_id, expected_ids):
            raise AdapterError(
                f"owner directory mismatch: unexpected DM entry {(platform, entry_id)!r}"
            )
    for platform in checked_platforms:
        if counts.get(platform, 0) == 0:
            raise AdapterError(
                f"owner directory mismatch: no DM entries found for platform {platform!r}"
            )


def _normalize_session_id(value: Any) -> str:
    if isinstance(value, bool):
        raise AdapterError("session_id must be a string or integer")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    raise AdapterError("session_id must be a non-empty string or integer")


def _normalize_timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError("timestamp must be numeric epoch seconds")
    return float(value)


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{label} must be a non-empty string")
    return value


def _require_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{label} must be a non-empty string or null")
    return value


def _normalize_content(value: Any) -> str | None:
    # Real state.db `messages.content` is nullable (PRAGMA table_info
    # notnull=0) and, separately, real production data has 1,962 assistant
    # rows with LENGTH(content)=0 (tool-call-only assistant turns). Both
    # shapes must parse; empty/null content is filtered downstream instead
    # of rejected here, so this stream must not die on either shape.
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdapterError("content must be a string or null")
    return value


def _normalize_epoch_argument(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError(f"{label} must be an integer epoch")
    if isinstance(value, float) and not value.is_integer():
        raise AdapterError(f"{label} must be an integer epoch")
    return int(value)


def _decode_payload(payload: bytes, command_name: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError(f"{command_name} output is not valid UTF-8") from exc
