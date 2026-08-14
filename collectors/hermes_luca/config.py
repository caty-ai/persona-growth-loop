from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPECTED_FACE = "luca"
EXPECTED_HOST = "vps-hermes"
EXPECTED_SPEAKER = "owner"
EXPECTED_SOURCE_KIND = "hermes-state-db"
ALLOWED_SOURCE_PLATFORMS = ("telegram", "slack", "api_server")
OWNER_UID_PLATFORMS = ("telegram", "slack")
EXPECTED_DM_ENTRY_PLATFORMS = ("telegram", "slack")
TOP_LEVEL_KEYS = (
    "face",
    "host",
    "speaker",
    "obs_root",
    "source",
    "denylist_path",
)
SOURCE_REQUIRED_KEYS = (
    "ssh_host",
    "kind",
    "db_path",
    "owner_uids",
    "expected_dm_entries",
    "sources",
    "dm_only",
    "exclude_session_prefixes",
)
SOURCE_OPTIONAL_KEYS = ("voice_enabled", "exclude_session_ledger")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    ssh_host: str
    kind: str
    db_path: str
    owner_uids: dict[str, tuple[str, ...]]
    expected_dm_entries: dict[str, tuple[str, ...]]
    sources: tuple[str, ...]
    dm_only: bool
    exclude_session_prefixes: tuple[str, ...]
    voice_enabled: bool = False
    exclude_session_ledger: Path | None = None


@dataclass(frozen=True)
class LucaConfig:
    face: str
    host: str
    speaker: str
    obs_root: Path
    denylist_path: Path
    source: SourceConfig


def load_config(config_path: str | Path) -> LucaConfig:
    path = Path(config_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("config root must be a JSON object")
    if "transcripts_root" in payload:
        raise ConfigError("transcripts_root is not allowed for Hermes Luca")
    _require_exact_keys(payload, TOP_LEVEL_KEYS, "config")

    face = _require_exact_string(payload["face"], "config.face", expected=EXPECTED_FACE)
    host = _require_exact_string(payload["host"], "config.host", expected=EXPECTED_HOST)
    speaker = _require_exact_string(payload["speaker"], "config.speaker", expected=EXPECTED_SPEAKER)
    obs_root = _resolve_local_path(
        _require_nonempty_string(payload["obs_root"], "config.obs_root"),
        base_dir=path.parent,
    )
    denylist_path = _resolve_local_path(
        _require_nonempty_string(payload["denylist_path"], "config.denylist_path"),
        base_dir=path.parent,
    )
    source = _parse_source_config(payload["source"], base_dir=path.parent)
    return LucaConfig(
        face=face,
        host=host,
        speaker=speaker,
        obs_root=obs_root,
        denylist_path=denylist_path,
        source=source,
    )


def _parse_source_config(value: Any, *, base_dir: Path) -> SourceConfig:
    if not isinstance(value, dict):
        raise ConfigError("config.source must be an object")
    allowed_keys = set(SOURCE_REQUIRED_KEYS) | set(SOURCE_OPTIONAL_KEYS)
    _require_exact_keys(value, allowed_keys, "config.source", required_keys=set(SOURCE_REQUIRED_KEYS))

    ssh_host = _require_ssh_host(value["ssh_host"])
    kind = _require_exact_string(value["kind"], "config.source.kind", expected=EXPECTED_SOURCE_KIND)
    db_path = _require_nonempty_string(value["db_path"], "config.source.db_path")
    owner_uids = _parse_platform_id_map(
        value["owner_uids"], OWNER_UID_PLATFORMS, "config.source.owner_uids"
    )
    expected_dm_entries = _parse_platform_id_map(
        value["expected_dm_entries"],
        EXPECTED_DM_ENTRY_PLATFORMS,
        "config.source.expected_dm_entries",
    )
    sources = _require_unique_string_list(
        value["sources"],
        "config.source.sources",
        allowed=set(ALLOWED_SOURCE_PLATFORMS),
    )
    dm_only = _require_bool(value["dm_only"], "config.source.dm_only")
    exclude_session_prefixes = _require_unique_string_list(
        value["exclude_session_prefixes"],
        "config.source.exclude_session_prefixes",
    )
    voice_enabled = _require_bool(value.get("voice_enabled", False), "config.source.voice_enabled")
    ledger_required = voice_enabled or "api_server" in sources
    if ledger_required and "exclude_session_ledger" not in value:
        raise ConfigError(
            "config.source is missing keys: exclude_session_ledger"
        )
    exclude_session_ledger = None
    if "exclude_session_ledger" in value:
        exclude_session_ledger = _resolve_ledger_path(
            _require_nonempty_string(
                value["exclude_session_ledger"],
                "config.source.exclude_session_ledger",
            ),
            base_dir=base_dir,
        )
    if dm_only is not True:
        raise ConfigError("config.source.dm_only must be true")
    if "pgl-verify-" not in exclude_session_prefixes:
        raise ConfigError("config.source.exclude_session_prefixes must include 'pgl-verify-'")
    return SourceConfig(
        ssh_host=ssh_host,
        kind=kind,
        db_path=db_path,
        owner_uids=owner_uids,
        expected_dm_entries=expected_dm_entries,
        sources=sources,
        dm_only=dm_only,
        exclude_session_prefixes=exclude_session_prefixes,
        voice_enabled=voice_enabled,
        exclude_session_ledger=exclude_session_ledger,
    )


def _parse_platform_id_map(
    value: Any, platforms: tuple[str, ...], label: str
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    _require_exact_keys(value, platforms, label)
    return {
        platform: _require_unique_string_list(
            value[platform],
            f"{label}.{platform}",
        )
        for platform in platforms
    }


def _require_exact_keys(
    value: dict[str, Any],
    allowed_keys: set[str] | tuple[str, ...],
    label: str,
    *,
    required_keys: set[str] | None = None,
) -> None:
    allowed = set(allowed_keys)
    required = allowed if required_keys is None else set(required_keys)
    actual = set(value)
    unknown = sorted(actual - allowed)
    missing = sorted(required - actual)
    if unknown:
        raise ConfigError(f"{label} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise ConfigError(f"{label} is missing keys: {', '.join(missing)}")


def _require_exact_string(value: Any, label: str, *, expected: str) -> str:
    text = _require_nonempty_string(value, label)
    if text != expected:
        raise ConfigError(f"{label} must be {expected!r}")
    return text


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _require_ssh_host(value: Any) -> str:
    host = _require_nonempty_string(value, "config.source.ssh_host")
    if host.startswith("-") or any(character.isspace() for character in host):
        raise ConfigError(
            "config.source.ssh_host must be a host alias, not an ssh option"
        )
    return host


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _require_unique_string_list(
    value: Any,
    label: str,
    *,
    allowed: set[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty string list")
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{label} must contain only non-empty strings")
        if allowed is not None and item not in allowed:
            raise ConfigError(f"{label} contains unsupported value: {item}")
        if item in seen:
            raise ConfigError(f"{label} contains duplicate value: {item}")
        seen.add(item)
        items.append(item)
    return tuple(items)


def _resolve_local_path(raw: str, *, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _resolve_ledger_path(raw: str, *, base_dir: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir.absolute() / path
    return path
