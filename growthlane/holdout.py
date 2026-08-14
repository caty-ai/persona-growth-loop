"""Deterministic daily exposure and proposal identifiers."""

from __future__ import annotations

import hashlib
from typing import Mapping, Sequence


def exposed(face: str, phrase_id: str, render_date: str) -> bool:
    value = (face + phrase_id + render_date).encode("utf-8")
    return hashlib.sha256(value).digest()[-1] % 2 == 0


def proposal_id(face: str, phrase_id: str, transition: str, run_date: str) -> str:
    value = (face + phrase_id + transition + run_date).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def rotation(phrases: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    staged = [item for item in phrases if item.get("state") == "staged"]
    return sorted(staged, key=lambda item: (str(item.get("staged_at", "")), str(item["id"])))[:3]


def exposed_rotation(
    face: str, phrases: Sequence[Mapping[str, object]], render_date: str
) -> list[Mapping[str, object]]:
    return [item for item in rotation(phrases) if exposed(face, str(item["id"]), render_date)]

