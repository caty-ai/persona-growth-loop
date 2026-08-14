"""First-layer transcript fragment filtering.

The order in :func:`filter_user_turn` mirrors observation-log-schema §2.2.
Sidechain user turns are rejected before extraction because they are
orchestrator-to-subagent prompts, not speech by the configured human speaker.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from collectors.shared.text_rules_l1 import (
    contains_secret_like,
    filter_fragment,
    increment as _increment,
)


def filter_user_turn(
    entry: dict[str, Any], stats: MutableMapping[str, int]
) -> list[str]:
    """Extract and filter human text fragments from one transcript entry."""

    if entry.get("type") != "user":
        _increment(stats, "drop_rule_1_non_user_turn")
        return []

    # Enforces speaker provenance from overlay-contract/observation schema §3:
    # sidechain "user" entries are orchestration prompts, not the human speaker.
    if entry.get("isSidechain") is True:
        _increment(stats, "sidechain_turns_skipped")
        return []

    message = entry.get("message")
    if not isinstance(message, dict):
        _increment(stats, "drop_malformed_user_turn")
        return []
    content = message.get("content")

    # A top-level toolUseResult identifies a tool-result user turn. Fail closed:
    # no array text from that turn may become an observation.
    if "toolUseResult" in entry:
        candidate_count = 1 if isinstance(content, str) else sum(
            1
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ) if isinstance(content, list) else 0
        _increment(stats, "drop_rule_2_tool_result", max(candidate_count, 1))
        return []

    fragments: list[str] = []
    if isinstance(content, str):
        fragments.append(content)
    elif isinstance(content, list):
        has_tool_result = any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
        if has_tool_result:
            candidate_count = sum(
                1
                for block in content
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
            )
            _increment(stats, "drop_rule_2_tool_result", max(candidate_count, 1))
            return []
        for block in content:
            if not isinstance(block, dict):
                _increment(stats, "drop_malformed_content_block")
                continue
            block_type = block.get("type")
            if block_type != "text":
                continue
            block_text = block.get("text")
            if not isinstance(block_text, str):
                _increment(stats, "drop_malformed_content_block")
                continue
            fragments.append(block_text)
    else:
        _increment(stats, "drop_malformed_user_turn")
        return []

    surviving: list[str] = []
    for fragment in fragments:
        _increment(stats, "population")
        filtered = filter_fragment(fragment, stats)
        if filtered is not None:
            surviving.append(filtered)
    return surviving
