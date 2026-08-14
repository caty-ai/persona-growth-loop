"""Single deny-grammar implementation shared by runtime and CI."""

from __future__ import annotations

import re
import unicodedata


RULE_LENGTH = "length"
RULE_INVISIBLE_FORMAT = "invisible_format"
RULE_IMPERATIVE = "imperative"
RULE_SECOND_PERSON = "second_person"
RULE_NEGATIVE_IMPERATIVE = "negative_imperative"
RULE_PRIVILEGE = "privilege_vocab"
RULE_PATH = "url_or_path"
RULE_CODE = "code_like"
RULE_DIGITS = "digit_run"

RULE_IDS = (
    RULE_LENGTH,
    RULE_INVISIBLE_FORMAT,
    RULE_IMPERATIVE,
    RULE_SECOND_PERSON,
    RULE_NEGATIVE_IMPERATIVE,
    RULE_PRIVILEGE,
    RULE_PATH,
    RULE_CODE,
    RULE_DIGITS,
)

IMPERATIVE_ENDINGS = ("しろ", "せよ", "して", "してください", "すること")
SECOND_PERSON = re.compile(
    r"^(?:"
    r"あなた(?:たち|達|方)?"
    r"|お前(?:ら|たち|達)?"
    r"|君(?:たち|達|ら)?"
    r")(?:は|が|も)"
)
NEGATIVE_IMPERATIVE = re.compile(r"(?:するな|しないで|禁止)")
PRIVILEGE_JA = ("承認", "許可", "権限", "実行", "削除", "パスワード", "秘密")
PRIVILEGE_ASCII = (
    "sudo",
    "rm",
    "push",
    "merge",
    "commit",
    "token",
    "password",
    "key",
    "secret",
)
PATH_MARKERS = ("://", "/Users/", "~/", "\\")
CODE_MARKERS = ("`", "{", "}", ";", "$(")
DIGIT_RUN = re.compile(r"\d{5,}")
NEWLINE = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
MATCH_IGNORED_CATEGORIES = frozenset({"Mn", "Me", "Zs", "Zl", "Zp"})
# Admission-only exemptions for intact emoji sequences. Matching never honors
# these exemptions: ZWJ, VS15, and VS16 are always deleted before comparison.
INVISIBLE_REJECT_EXEMPTIONS = frozenset({"\u200d", "\ufe0e", "\ufe0f"})
INVISIBLE_REJECT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
DECORATIVE_END_CATEGORIES = frozenset({"So", "Sk", "Pe", "Pf"})
DECORATIVE_ENDERS = frozenset("。．.!！?？…、,，〜～ーｰ♪wｗ")

# Unicode 16.0.0 DerivedCoreProperties.txt, Default_Ignorable_Code_Point
# (4,174 codepoints), published by Unicode, Inc.:
# https://www.unicode.org/Public/16.0.0/ucd/DerivedCoreProperties.txt
# Twelve known blank/nonprinting stragglers extend it to the 4,186-codepoint
# review corpus. This remains the enumerated part of matching canonicalization;
# faithful storage never deletes from it. Tests import the resulting corpus
# rather than maintaining a twin.
IGNORABLE_CORPUS_UNICODE_VERSION = "16.0.0"
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_BLANK_OR_NONPRINTING_STRAGGLERS = frozenset(
    {
        0x0000,
        0x0001,
        0x0002,
        0x0008,
        0x000B,
        0x001F,
        0x007F,
        0x0080,
        0x009F,
        0x2800,
        0xE000,
        0x16FE4,
    }
)
IGNORABLE_CORPUS = frozenset(
    chr(codepoint)
    for start, end in _DEFAULT_IGNORABLE_RANGES
    for codepoint in range(start, end + 1)
) | frozenset(chr(codepoint) for codepoint in _BLANK_OR_NONPRINTING_STRAGGLERS)


def unicode_admission_drift() -> tuple[str, str] | None:
    """Return corpus/runtime versions when admission must fail closed."""

    corpus = tuple(int(part) for part in IGNORABLE_CORPUS_UNICODE_VERSION.split("."))
    runtime = tuple(int(part) for part in unicodedata.unidata_version.split("."))
    if runtime != corpus:
        return IGNORABLE_CORPUS_UNICODE_VERSION, unicodedata.unidata_version
    return None


def _is_noncharacter(codepoint: int) -> bool:
    """Recognize Unicode noncharacters independently of the runtime UCD."""

    return 0xFDD0 <= codepoint <= 0xFDEF or (
        codepoint & 0xFFFF
    ) in {0xFFFE, 0xFFFF}


def _deleted_before_matching(char: str) -> bool:
    """Return the enumerated-or-runtime-property matching pre-pass predicate."""

    codepoint = ord(char)
    return (
        char in IGNORABLE_CORPUS
        or unicodedata.category(char) == "Cn"
        or _is_noncharacter(codepoint)
    )


def _ignored_for_matching(char: str) -> bool:
    category = unicodedata.category(char)
    return (
        category.startswith("C")
        or category in MATCH_IGNORED_CATEGORIES
    )


_BMP_MATCH_DELETE = {
    codepoint: None
    for codepoint in range(0x10000)
    if _ignored_for_matching(chr(codepoint))
}

_BMP_MATCH_PREPASS_DELETE = {
    codepoint: None
    for codepoint in range(0x10000)
    if _deleted_before_matching(chr(codepoint))
}


def _regex_class(codepoints: tuple[int, ...]) -> str:
    ranges: list[tuple[int, int]] = []
    start = previous = codepoints[0]
    for codepoint in codepoints[1:]:
        if codepoint == previous + 1:
            previous = codepoint
            continue
        ranges.append((start, previous))
        start = previous = codepoint
    ranges.append((start, previous))

    def escaped(codepoint: int) -> str:
        return (
            f"\\u{codepoint:04X}"
            if codepoint <= 0xFFFF
            else f"\\U{codepoint:08X}"
        )

    return "".join(
        escaped(first) if first == last else f"{escaped(first)}-{escaped(last)}"
        for first, last in ranges
    )


_ASCII_COMPLEX_MATCH = re.compile(
    f"[{_regex_class(tuple(codepoint for codepoint in _BMP_MATCH_DELETE if codepoint < 128 and codepoint != 0x20))}]"
)
_BMP_COMPLEX_OR_ASTRAL_MATCH = re.compile(
    f"[{_regex_class(tuple(codepoint for codepoint in _BMP_MATCH_DELETE if codepoint != 0x20))}"
    "\\U00010000-\\U0010FFFF]"
)


def _strip_for_matching(text: str) -> str:
    """Delete matching-only codepoints with the BMP work kept inside C."""

    if text.isascii():
        if _ASCII_COMPLEX_MATCH.search(text) is None:
            return text.replace(" ", "")
        return text.translate(_BMP_MATCH_DELETE)
    if _BMP_COMPLEX_OR_ASTRAL_MATCH.search(text) is None:
        return text.replace(" ", "")
    stripped = text.translate(_BMP_MATCH_DELETE)
    if stripped.isascii() or all(ord(char) <= 0xFFFF for char in stripped):
        return stripped
    return "".join(
        char
        for char in stripped
        if ord(char) <= 0xFFFF or not _ignored_for_matching(char)
    )


def _matching_prepass(text: str) -> str:
    """Delete ignorables, every runtime-Cn, and every noncharacter."""

    if text.isascii():
        return text.translate(_BMP_MATCH_PREPASS_DELETE)
    has_astral = len(text.encode("utf-16-le", "surrogatepass")) != len(text) * 2
    prepassed = text.translate(_BMP_MATCH_PREPASS_DELETE)
    if not has_astral:
        return prepassed
    return "".join(
        char
        for char in prepassed
        if ord(char) <= 0xFFFF or not _deleted_before_matching(char)
    )


def canonicalize_for_storage(text: str) -> str:
    """Return faithful NFC storage text with whitespace runs collapsed."""

    return " ".join(unicodedata.normalize("NFC", text).split())


def canonicalize_for_matching(text: str) -> str:
    """Return the primary NFKC-then-strip comparison view."""

    return matching_views(text)[0]


def matching_views(text: str) -> tuple[str, ...]:
    """Return matching views after the corpus/property deletion pre-pass.

    Neither NFKC-then-strip nor strip-then-NFKC is sufficient alone: the
    former can compose an inserted mark into a letter, while the latter can
    destroy a legitimately decomposed denied token. Rules and blocklist
    comparisons therefore inspect both views.
    """

    prepassed = _matching_prepass(text)
    normalized = unicodedata.normalize("NFKC", prepassed)
    compose_first = _strip_for_matching(normalized)
    strip_first = unicodedata.normalize("NFKC", _strip_for_matching(prepassed))
    if strip_first == compose_first:
        return (compose_first,)
    return (compose_first, strip_first)


def matching_text(text: str) -> str:
    """Return the NFKC-then-strip view for diagnostics and simple callers.

    Security decisions must use :func:`matching_views` so both orders are
    inspected. Keeping this single-view helper avoids materializing a joined
    representation in hot diagnostic paths.
    """

    return canonicalize_for_matching(text)


def matching_contains(needle: str, haystack: str) -> bool:
    """Return whether any non-empty matching view is a substring of another."""

    return any(
        needle_view and needle_view in haystack_view
        for needle_view in matching_views(needle)
        for haystack_view in matching_views(haystack)
    )


def _grammar_stem(text: str) -> str:
    end = len(text)
    while end and (
        text[end - 1] in DECORATIVE_ENDERS
        or unicodedata.category(text[end - 1]) in DECORATIVE_END_CATEGORIES
    ):
        end -= 1
    return text[:end]


_PATH_TRANSLATION = str.maketrans({"〜": "~", "～": "~"})
_YEN_PATH = re.compile(r"(?:^[A-Za-z]:¥|¥[A-Za-z0-9_]+¥)")


def _katakana_to_hiragana(text: str) -> str:
    """Fold full-width katakana to hiragana for grammar checks only."""

    folded: list[str] = []
    for char in text:
        codepoint = ord(char)
        if 0x30A1 <= codepoint <= 0x30F6:
            folded.append(chr(codepoint - 0x60))
        elif codepoint == 0x30FD:
            folded.append("\u309d")
        elif codepoint == 0x30FE:
            folded.append("\u309e")
        else:
            folded.append(char)
    return "".join(folded)


def _is_japanese_letter(char: str) -> bool:
    if char == "・":
        return False
    codepoint = ord(char)
    return (
        0x3040 <= codepoint <= 0x309F
        or 0x30A0 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _grammar_views(text: str) -> tuple[str, ...]:
    """Return grammar-only views, plus one bounded trailing-char trim."""

    stem = _grammar_stem(text)
    if not stem:
        return ("",)
    variants = [stem]
    if not _is_japanese_letter(stem[-1]):
        variants.append(stem[:-1])
    folded: list[str] = []
    for variant in variants:
        folded_variant = _katakana_to_hiragana(variant)
        if folded_variant not in folded:
            folded.append(folded_variant)
    return tuple(folded)


def _path_views(text: str) -> tuple[str, ...]:
    folded = text.translate(_PATH_TRANSLATION)
    if folded == text:
        return (text,)
    return text, folded


def _contains_path_marker(text: str) -> bool:
    return any(marker in text for marker in PATH_MARKERS) or _YEN_PATH.search(text) is not None


def lint_phrase(text: object) -> list[str]:
    """Return every violated rule id in stable contract order."""

    if not isinstance(text, str):
        return list(RULE_IDS)
    violations: list[str] = []
    storage_text = canonicalize_for_storage(text)
    if len(storage_text) > 24 or NEWLINE.search(text):
        violations.append(RULE_LENGTH)
    if any(
        unicodedata.category(char) in INVISIBLE_REJECT_CATEGORIES
        and char not in INVISIBLE_REJECT_EXEMPTIONS
        for char in text
    ):
        violations.append(RULE_INVISIBLE_FORMAT)
    # Length is measured on faithful NFC storage, which is what renders.
    # Every structural grammar rule inspects comparison-only views; the emoji
    # exemptions above affect admission rejection, never these views.
    views = matching_views(text)
    grammar_views = tuple(
        candidate for view in views for candidate in _grammar_views(view) if candidate
    )
    if any(stem.endswith(IMPERATIVE_ENDINGS) for stem in grammar_views):
        violations.append(RULE_IMPERATIVE)
    if any(SECOND_PERSON.search(view) for view in grammar_views):
        violations.append(RULE_SECOND_PERSON)
    if any(NEGATIVE_IMPERATIVE.search(stem) for stem in grammar_views):
        violations.append(RULE_NEGATIVE_IMPERATIVE)
    lowered = tuple(view.casefold() for view in views)
    if any(word in view for view in views for word in PRIVILEGE_JA) or any(
        word in view for view in lowered for word in PRIVILEGE_ASCII
    ):
        violations.append(RULE_PRIVILEGE)
    if any(
        _contains_path_marker(path_view)
        for view in views
        for path_view in _path_views(view)
    ):
        violations.append(RULE_PATH)
    if any(marker in view for view in views for marker in CODE_MARKERS):
        violations.append(RULE_CODE)
    if any(DIGIT_RUN.search(view) for view in views):
        violations.append(RULE_DIGITS)
    return violations
