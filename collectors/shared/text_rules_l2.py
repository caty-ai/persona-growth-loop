"""Shared second-layer observation scrub rules."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_SCRUB_KEY_MARKERS = (
    "AKIA",
    "sk-",
    "ghp_",
    "github_pat_",
    "xox",
    "-----BEGIN",
)
_SCRUB_EYJ_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9])eyJ")
_SCRUB_BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{40,}")
_SECRET_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
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


def _contains_secret_like(text: str) -> bool:
    from growthlane.guard import matching_views

    candidates = tuple(
        candidate.translate(_SECRET_DASH_TRANSLATION)
        for candidate in (text, *matching_views(text))
    )
    return any(
        any(marker in candidate for marker in _SCRUB_KEY_MARKERS)
        or _SCRUB_EYJ_TOKEN.search(candidate) is not None
        or _SCRUB_BASE64_RUN.search(candidate) is not None
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


def scrub_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Drop secret-like records, mask PII, and recompute code-point length."""

    text = record.get("text")
    if not isinstance(text, str) or _contains_secret_like(text):
        return None
    masked = _mask_pii_raw(text)
    if _contains_pii_residue_in_matching_views(masked):
        return None
    scrubbed = dict(record)
    scrubbed["text"] = masked
    scrubbed["len"] = len(masked)
    return scrubbed
