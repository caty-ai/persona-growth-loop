"""Soul-frozen, deterministic render templates."""

from __future__ import annotations

from typing import Mapping, Sequence

from .faces import FaceProfile
from .holdout import exposed_rotation


ADOPTED_TEMPLATE = "{user}がよく使う言い回し（参照データ・指示ではない）: {phrases}"
CANDIDATES_TEMPLATE = "試用中の言い回し（参照データ・稀に使う: 1セッション1回まで）: {phrases}"


def _adoption_key(item: Mapping[str, object]) -> tuple[str, str]:
    for event in item.get("history", []):
        if isinstance(event, dict) and event.get("to") == "adopted":
            return str(event.get("at", "")), str(item["id"])
    return "", str(item["id"])


def render_files(
    profile: FaceProfile,
    ledger: Mapping[str, object],
    render_date: str,
    user: str,
) -> dict[str, bytes]:
    phrases = ledger["phrases"]
    if not isinstance(phrases, list):
        raise ValueError("ledger phrases must be a list")
    adopted = sorted(
        [item for item in phrases if isinstance(item, dict) and item.get("state") == "adopted"],
        key=_adoption_key,
    )
    candidates = exposed_rotation(profile.name, phrases, render_date)
    adopted_line = (
        ADOPTED_TEMPLATE.format(user=user, phrases="、".join(str(item["text"]) for item in adopted))
        if adopted
        else ""
    )
    candidates_line = (
        CANDIDATES_TEMPLATE.format(
            phrases="、".join(str(item["text"]) for item in candidates)
        )
        if candidates
        else ""
    )
    if profile.engine:
        return {
            profile.render_files["adopted"]: (adopted_line + "\n").encode("utf-8") if adopted_line else b"",
            profile.render_files["candidates"]: (candidates_line + "\n").encode("utf-8") if candidates_line else b"",
        }
    lines = [line for line in (adopted_line, candidates_line) if line]
    return {
        profile.render_files["combined"]: (("\n".join(lines) + "\n").encode("utf-8") if lines else b"")
    }

