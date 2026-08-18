"""Hardened Luca production-anchor persistence shared by deploy and mirror."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from mirror.common import MirrorError, atomic_json, read_json_nofollow


_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


def read_production_anchor(pgl_home: Path) -> str:
    path = pgl_home / "state" / "luca-prod-anchor.json"
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"production anchor unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"production anchor has unsafe shape: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"production anchor must have mode 0600: {path}")
    try:
        value = read_json_nofollow(path)
    except MirrorError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(value, dict) or set(value) != {"content_hash"}:
        raise ValueError("production anchor schema must contain only content_hash")
    content_hash = value.get("content_hash")
    if not isinstance(content_hash, str) or _CONTENT_HASH.fullmatch(content_hash) is None:
        raise ValueError(
            "production anchor content_hash must be 64 lowercase hex characters"
        )
    return content_hash


def write_production_anchor(pgl_home: Path, content_hash: str) -> None:
    if _CONTENT_HASH.fullmatch(content_hash) is None:
        raise ValueError("production anchor must be 64 lowercase hex characters")
    atomic_json(
        pgl_home / "state/luca-prod-anchor.json",
        {"content_hash": content_hash},
    )


__all__ = ["read_production_anchor", "write_production_anchor"]
