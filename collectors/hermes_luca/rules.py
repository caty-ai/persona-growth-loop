from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from collectors.shared.text_rules_l1 import filter_fragment as _shared_filter_fragment


# Hermes injects `<memory-context>...</memory-context>` host suffixes
# (documented in the private engine repo's deploy runbook) and
# `<system-reminder>...</system-reminder>` prompt blocks
# (`docs/contracts/observation-log-schema.md`); Luca strips only complete
# known blocks, then fails closed on any surviving marker-like fragment.
TG_GROUP_PREFIX_RE = re.compile(r"^\[.+\|\d+\]\n")
MEMORY_CONTEXT_BLOCK_RE = re.compile(
    r"(?:\n\n)?<memory-context>.*?</memory-context>",
    re.IGNORECASE | re.DOTALL,
)
SYSTEM_REMINDER_BLOCK_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>",
    re.IGNORECASE | re.DOTALL,
)
INJECTION_MARKER_FRAGMENT_RE = re.compile(
    r"<\s*/?\s*(?:memory-context|system-reminder)\b",
    re.IGNORECASE,
)


class SessionTurn(Protocol):
    session_id: str
    source: str
    chat_type: str | None
    user_id: str | None
    session_key: str | None


def strip_and_validate_injection(text: str) -> str | None:
    stripped = MEMORY_CONTEXT_BLOCK_RE.sub("", text)
    stripped = SYSTEM_REMINDER_BLOCK_RE.sub("", stripped)
    if INJECTION_MARKER_FRAGMENT_RE.search(stripped) is not None:
        return None
    return stripped


def session_is_excluded(
    session_id: str,
    session_key: str | None,
    prefixes: Sequence[str],
) -> bool:
    return any(
        session_id.startswith(prefix)
        or (session_key is not None and session_key.startswith(prefix))
        for prefix in prefixes
    )


def session_attribution(
    turns: Iterable[SessionTurn],
    owner_uids: Mapping[str, Iterable[str]],
    voice_enabled: bool,
) -> tuple[bool, bool, str]:
    items = tuple(turns)
    if not items:
        return False, False, "empty_session"

    if len({turn.session_id for turn in items}) != 1:
        return False, False, "mixed_session_id"
    if len({turn.session_key for turn in items}) != 1:
        return False, False, "mixed_session_key"
    if len({turn.source for turn in items}) != 1:
        return False, False, "mixed_source"
    if len({turn.chat_type for turn in items}) != 1:
        return False, False, "mixed_chat_type"

    source = items[0].source

    if source == "api_server":
        # The frozen spike defines api_server attribution at the owner-level
        # Bearer boundary. Unlike Telegram/Slack, this route has no uid or
        # normative chat_type discriminator; voice_enabled is its explicit
        # fail-closed operator switch.
        if not voice_enabled:
            return False, False, "voice_disabled"
        if any(turn.user_id not in (None, "") for turn in items):
            return False, False, "unexpected_voice_uid"
        return True, False, "voice_enabled"

    if source not in {"telegram", "slack"}:
        return False, False, "unsupported_source"

    if items[0].chat_type != "dm":
        return False, False, "not_dm"

    user_ids = {turn.user_id for turn in items}
    if any(user_id in (None, "") for user_id in user_ids):
        return False, False, "missing_uid"

    normalized_owner_uids = {
        platform: {value for value in values if value}
        for platform, values in owner_uids.items()
    }
    allowed_uids = normalized_owner_uids.get(source, set())
    normalized_user_ids = {user_id for user_id in user_ids if isinstance(user_id, str) and user_id}
    outsider = any(user_id not in allowed_uids for user_id in normalized_user_ids)

    if len(normalized_user_ids) != 1:
        return False, outsider, "mixed_user_id"

    user_id = next(iter(normalized_user_ids))
    if user_id not in allowed_uids:
        return False, True, "outsider_uid"

    return True, False, "owner_dm"


def filter_user_text(text: str) -> str | None:
    if TG_GROUP_PREFIX_RE.match(text) is not None:
        return None

    stripped = strip_and_validate_injection(text)
    if stripped is None:
        return None

    return _shared_filter_fragment(stripped, {})
