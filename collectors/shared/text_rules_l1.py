"""Shared first-layer transcript fragment filtering rules."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import MutableMapping

_SYSTEM_REMINDER = re.compile(
    r"<system-reminder>.*?(?:</system-reminder>|$)", re.DOTALL
)
_COMMAND_TAG = re.compile(
    r"</?(?:command-name|command-message|command-args|local-command-stdout)(?:\s[^>]*)?>"
)
_FENCED_CODE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{40,}")
_EYJ_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9])eyJ")
_KEY_MARKERS = ("AKIA", "sk-", "ghp_", "github_pat_", "xox", "-----BEGIN")
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
)
_PHONE = re.compile(
    r"(?<!\d)(?:"
    r"\+81(?:[- ]?(?:\(0\))?[- ]?)?"
    r"(?:"
    r"\(?[789]0\)?[- ]?\d{4}[- ]?\d{4}"
    r"|\(?[1-9]\d{0,3}\)?[- ]?\d{2,4}[- ]?\d{4}"
    r"|[1-9]\d{8,9}"
    r")"
    r"|"
    r"0[789]0[- ]?\d{4}[- ]?\d{4}"
    r"|0\d[- ]?\d{4}[- ]?\d{4}"
    r"|0\d{2}[- ]?\d{3,4}[- ]?\d{4}"
    r")(?!\d)"
)
_SECRET_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_PATH_TRANSLATION = str.maketrans({"〜": "~", "～": "~"})
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:\\")
_YEN_PATH = re.compile(r"(?:[A-Za-z]:¥|¥[A-Za-z0-9_]+¥)")
_MASKED_EMAIL_RESIDUE = re.compile(
    r"(?:"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+(?=\*\*\*@\*\*\*)"
    r"|(?<=\*\*\*@\*\*\*)(?:"
    r"\.?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"| *+\.? *+[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r")"
    r")"
)
_RESIDUE_IGNORED_CATEGORIES = frozenset({"Mn", "Me", "Zl", "Zp"})


def increment(stats: MutableMapping[str, int], key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def contains_secret_like(text: str) -> bool:
    """Return whether text matches the first-layer §2.2-8 patterns."""

    from growthlane.guard import matching_views

    candidates = tuple(
        candidate.translate(_SECRET_DASH_TRANSLATION)
        for candidate in (text, *matching_views(text))
    )
    return any(
        any(marker in candidate for marker in _KEY_MARKERS)
        or _EYJ_TOKEN.search(candidate) is not None
        or _BASE64_RUN.search(candidate) is not None
        for candidate in candidates
    )


def _mask_pii_raw(text: str) -> str:
    masked = _EMAIL.sub("***@***", text)
    return _PHONE.sub("0**-****-****", masked)


def _contains_pii_residue_in_matching_views(text: str) -> bool:
    from growthlane.guard import matching_views

    def residue_views(candidate: str) -> tuple[str, ...]:
        def remove_matching_ignored(text: str) -> str:
            return "".join(
                char
                for char in text
                if not (
                    unicodedata.category(char).startswith("C")
                    or unicodedata.category(char) in _RESIDUE_IGNORED_CATEGORIES
                )
            )

        compose_first = remove_matching_ignored(unicodedata.normalize("NFKC", candidate))
        strip_first = unicodedata.normalize("NFKC", remove_matching_ignored(candidate))
        if strip_first == compose_first:
            return (compose_first,)
        return (compose_first, strip_first)

    return any(
        _EMAIL.search(candidate) is not None or _PHONE.search(candidate) is not None
        for candidate in matching_views(text)
    ) or any(
        _MASKED_EMAIL_RESIDUE.search(candidate) is not None
        for candidate in residue_views(text)
    )


def _remove_tag_lines(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    kept = [line for line in lines if _COMMAND_TAG.search(line) is None]
    return "\n".join(kept), len(kept) != len(lines)


def _is_path_like_line(line: str) -> bool:
    folded = unicodedata.normalize("NFKC", line).translate(_PATH_TRANSLATION)
    stripped = line.strip()
    folded_stripped = folded.strip()
    return (
        "://" in line
        or "://" in folded
        or stripped.startswith("~/")
        or folded_stripped.startswith("~/")
        or stripped.startswith("/")
        or stripped.startswith("\\")
        or "/Users/" in line
        or _WINDOWS_DRIVE_PATH.match(folded_stripped) is not None
        or _YEN_PATH.search(folded_stripped) is not None
    )


def _remove_path_lines(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    kept = [line for line in lines if not _is_path_like_line(line)]
    return "\n".join(kept), len(kept) != len(lines)


def filter_fragment(text: str, stats: MutableMapping[str, int]) -> str | None:
    """Apply §2.2 rules 3--8 to one candidate fragment, in order."""

    filtered, count = _SYSTEM_REMINDER.subn("", text)
    if count:
        increment(stats, "drop_rule_3_system_reminder")

    filtered, changed = _remove_tag_lines(filtered)
    if changed:
        increment(stats, "drop_rule_4_command_tags")

    filtered, count = _FENCED_CODE.subn("", filtered)
    if count:
        increment(stats, "drop_rule_5_code_fence")

    filtered, changed = _remove_path_lines(filtered)
    if changed:
        increment(stats, "drop_rule_6_path_like")

    filtered = filtered.strip()
    if not filtered:
        increment(stats, "drop_empty")
        return None

    if len(filtered) > 240:
        increment(stats, "drop_rule_7_length")
        return None

    if contains_secret_like(filtered):
        increment(stats, "drop_rule_8_secret_like")
        return None

    masked = _mask_pii_raw(filtered)
    if _contains_pii_residue_in_matching_views(masked):
        increment(stats, "drop_rule_8_secret_like")
        return None
    if masked != filtered:
        return masked

    return filtered
