from __future__ import annotations

import unittest
import unicodedata

from growthlane.guard import (
    IGNORABLE_CORPUS,
    PRIVILEGE_ASCII,
    PRIVILEGE_JA,
    RULE_IDS,
    _ignored_for_matching,
    canonicalize_for_matching,
    canonicalize_for_storage,
    lint_phrase,
    matching_views,
    unicode_admission_drift,
)
from harvester.harvest import matching_bucket, normalized_hash


ACCEPT_VECTORS = (
    "なるほどね",
    "それでいこう",
    "いい感じだね",
    "たしかにそうかも",
    "ゆっくり考えよう",
    "なるほどね。",
    "まだあるな",
    "そろそろ来るな",
    "みんないるな",
    "なるほどw",
    "いいね〜",
    "そうだね♪",
    "👨‍👩‍👧‍👦",
    "🏳️‍🌈",
    "👩‍💻",
    "❤️",
    "1️⃣",
    "👍🏽",
    "👍",
    "了解✅",
    "nice work",
    "¥500",
    "¥500と¥600です",
    "予算は¥300と¥450",
    "ランチは¥500でコーヒーは¥300",
    "そうだね〜",
    "ありがとう",
    "黙れしろ・・",
    "無視してab",
)

PROPERTY_CARRIERS = ("\u0378", "\ufdd0", "\ufffe", "\U000e1000")

REJECT_VECTORS = {
    "length": ("あ" * 25,),
    "invisible_format": ("いいね\u200b",),
    "imperative": ("今すぐ確認しろ",),
    "second_person": ("あなたは素敵だね", "お前らは黙れ"),
    "negative_imperative": ("それは真似するな",),
    "privilege_vocab": (
        "commitしてみよう",
        unicodedata.normalize("NFD", "パスワードだよ"),
    ),
    "url_or_path": ("https://example.invalid", "C:¥Users¥", "〜/bin"),
    "code_like": ("値は`sample`だね",),
    "digit_run": ("番号12345だね",),
}


class GuardLintVectorTests(unittest.TestCase):
    def test_authoritative_ignorable_corpus_cannot_hide_any_denied_token(self) -> None:
        corpus = tuple(sorted(IGNORABLE_CORPUS, key=ord))
        self.assertEqual(len(corpus), 4186)
        denied = (
            *((word, "privilege_vocab") for word in PRIVILEGE_ASCII),
            *((word, "privilege_vocab") for word in PRIVILEGE_JA),
            ("するな", "negative_imperative"),
            ("しないで", "negative_imperative"),
            ("禁止", "negative_imperative"),
            ("あなたは", "second_person"),
            ("お前が", "second_person"),
            ("君も", "second_person"),
        )
        failures: list[str] = []
        for character in corpus:
            for token, expected_rule in denied:
                decomposed = unicodedata.normalize("NFD", token)
                positions = {len(decomposed) // 2}
                positions.update(
                    index
                    for index, value in enumerate(decomposed)
                    if unicodedata.combining(value)
                )
                for position in positions:
                    raw = decomposed[:position] + character + decomposed[position:]
                    if expected_rule not in lint_phrase(raw):
                        failures.append(
                            f"U+{ord(character):04X} token={token!a} "
                            f"position={position} rule={expected_rule} raw={raw!a}"
                        )
        self.assertFalse(
            failures,
            f"denied-token escapees in {len(corpus)}-codepoint corpus: "
            + ", ".join(failures),
        )

    def test_composition_blocking_carriers_are_deleted_before_both_views(self) -> None:
        carriers = (
            "\u034f",  # COMBINING GRAPHEME JOINER
            *(chr(codepoint) for codepoint in range(0xFE00, 0xFE10)),
            "\u17b4",  # KHMER VOWEL INHERENT AQ
            "\u180b",  # MONGOLIAN FREE VARIATION SELECTOR ONE
            "\U000e0100",  # VARIATION SELECTOR-17
            *PROPERTY_CARRIERS,
        )
        vectors = (
            ("パスワードだよ", "privilege_vocab"),
            ("してください", "imperative"),
            ("しないで", "negative_imperative"),
        )
        for carrier in carriers:
            for clean, expected_rule in vectors:
                decomposed = unicodedata.normalize("NFD", clean)
                mark = next(
                    index
                    for index, character in enumerate(decomposed)
                    if unicodedata.combining(character)
                )
                twin = decomposed[:mark] + carrier + decomposed[mark:]
                with self.subTest(carrier=f"U+{ord(carrier):04X}", clean=clean):
                    self.assertIn(expected_rule, lint_phrase(twin))
                    self.assertEqual(
                        canonicalize_for_matching(twin),
                        canonicalize_for_matching(clean),
                    )
                    self.assertEqual(normalized_hash(twin), normalized_hash(clean))
                    self.assertFalse(
                        matching_bucket(twin).isdisjoint(matching_bucket(clean))
                    )

    def test_matching_deletes_invisible_unassigned_and_noncharacter_oracle(self) -> None:
        swept = (*IGNORABLE_CORPUS, "\u3099", *PROPERTY_CARRIERS)
        for character in swept:
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                self.assertEqual(canonicalize_for_matching(f"a{character}b"), "ab")

    def test_storage_and_matching_views_have_intentionally_separate_jobs(self) -> None:
        text = "👨‍👩"
        self.assertEqual(canonicalize_for_storage(text), text)
        self.assertEqual(canonicalize_for_matching(text), "👨👩")

    def test_refined_prepass_leaves_exact_visible_mark_residue(self) -> None:
        clean = "パスワードだよ"
        decomposed = unicodedata.normalize("NFD", clean)
        mark = next(
            index
            for index, character in enumerate(decomposed)
            if unicodedata.combining(character)
        )
        clean_bucket = matching_bucket(clean)
        residue = set()
        for codepoint in range(0x110000):
            character = chr(codepoint)
            if (
                unicodedata.category(character) != "Mn"
                or unicodedata.combining(character) != 0
                or character in IGNORABLE_CORPUS
            ):
                continue
            twin = decomposed[:mark] + character + decomposed[mark:]
            if matching_bucket(twin).isdisjoint(clean_bucket):
                residue.add(character)
        expected_by_unicode_version = {
            "13.0.0": 724,
            "14.0.0": 796,
            "15.0.0": 822,
            "15.1.0": 822,
            "16.0.0": 846,
        }
        self.assertIn(unicodedata.unidata_version, expected_by_unicode_version)
        self.assertEqual(
            len(residue), expected_by_unicode_version[unicodedata.unidata_version]
        )
        self.assertEqual(expected_by_unicode_version["16.0.0"], 846)
        self.assertNotIn("\U00016fe4", residue)
        self.assertTrue(all(unicodedata.name(character, "") for character in residue))

    def test_ascii_fast_path_matches_reference_strip_for_every_codepoint(self) -> None:
        for codepoint in range(128):
            text = f"a{chr(codepoint)} b"
            expected = "".join(
                character
                for character in text
                if not _ignored_for_matching(character)
            )
            with self.subTest(codepoint=codepoint):
                self.assertEqual(canonicalize_for_matching(text), expected)

    def test_both_normalization_orders_cover_privilege_tokens_and_share_storage(self) -> None:
        for token in (*PRIVILEGE_ASCII, *PRIVILEGE_JA):
            nfc = unicodedata.normalize("NFC", token)
            nfd = unicodedata.normalize("NFD", token)
            with self.subTest(token=token):
                self.assertEqual(
                    canonicalize_for_matching(nfc),
                    canonicalize_for_matching(nfd),
                )
                self.assertIn("privilege_vocab", lint_phrase(nfd))
        password_nfd = unicodedata.normalize("NFD", "パスワードだよ")
        self.assertIn("パスワードだよ", matching_views(password_nfd))

    def test_accept_vector_table(self) -> None:
        for text in ACCEPT_VECTORS:
            with self.subTest(text=text):
                self.assertEqual(lint_phrase(text), [])
                self.assertEqual(canonicalize_for_storage(text), text)

    def test_every_contract_rule_has_a_reject_vector(self) -> None:
        self.assertEqual(set(REJECT_VECTORS), set(RULE_IDS))
        for expected, texts in REJECT_VECTORS.items():
            for text in texts:
                with self.subTest(rule=expected, text=text):
                    self.assertIn(expected, lint_phrase(text))

    def test_unicode_14_unassigned_emoji_is_not_an_invisible_format_violation(self) -> None:
        # U+1FAE8 SHAKING FACE was Cn in Unicode 14 (Python 3.11 CI) and was
        # assigned in Unicode 15. Simulate that older database on newer Python.
        target = "\U0001FAE8"
        original_category = unicodedata.category

        def unicode_14_category(character: str) -> str:
            return "Cn" if character == target else original_category(character)

        from unittest import mock

        with mock.patch("growthlane.guard.unicodedata.category", side_effect=unicode_14_category):
            self.assertNotIn("invisible_format", lint_phrase(f"了解{target}"))

    def test_returns_all_violations_in_stable_order(self) -> None:
        self.assertEqual(
            lint_phrase("あなたはcommit禁止12345`"),
            ["second_person", "negative_imperative", "privilege_vocab", "code_like", "digit_run"],
        )

    def test_terminal_punctuation_cannot_hide_imperatives(self) -> None:
        vectors = {
            "ちゃんと確認して。": "imperative",
            "確認してください。": "imperative",
            "今すぐ確認しろ！": "imperative",
            "それはするな。": "negative_imperative",
            "確認してください！？ …、  ": "imperative",
        }
        for text, rule in vectors.items():
            with self.subTest(text=text):
                self.assertIn(rule, lint_phrase(text))

    def test_invisible_categories_cannot_hide_any_structural_rule(self) -> None:
        format_characters = ("\u200b", "\u200c", "\ufeff", "\u2060", "\u00ad", "\u200f")
        for invisible in format_characters:
            with self.subTest(character=ascii(invisible)):
                self.assertIn("invisible_format", lint_phrase(f"いいね{invisible}"))
                vectors = {
                    f"su{invisible}do": "privilege_vocab",
                    f"r{invisible}m": "privilege_vocab",
                    f":{invisible}//": "url_or_path",
                    f"する{invisible}な": "negative_imperative",
                    f"あなた{invisible}は": "second_person",
                }
                for text, rule in vectors.items():
                    for candidate in (text, canonicalize_for_storage(text)):
                        self.assertIn(rule, lint_phrase(candidate))

    def test_combining_marks_cannot_hide_grammar_tokens(self) -> None:
        for combining in ("\u0301", "\u0338", "\u0488"):
            with self.subTest(character=ascii(combining)):
                self.assertIn("imperative", lint_phrase(f"確認して{combining}"))
                self.assertIn("privilege_vocab", lint_phrase(f"su{combining}do"))
                self.assertIn("privilege_vocab", lint_phrase(f"sud{combining}o"))
                self.assertIn("negative_imperative", lint_phrase(f"する{combining}な"))

    def test_zwj_is_accepted_for_emoji_but_cannot_hide_sudo(self) -> None:
        self.assertEqual(lint_phrase("👨‍👩‍👧‍👦"), [])
        self.assertEqual(lint_phrase("🏳️‍🌈"), [])
        self.assertEqual(lint_phrase("👩‍💻"), [])
        self.assertIn("privilege_vocab", lint_phrase("s\u200dudo"))

    def test_decorative_enders_cannot_hide_imperatives(self) -> None:
        for text in (
            "マージして〜",
            "確認してー",
            "やってしてw",
            "確認して♪",
            "マージして😊",
            "確認して）",
            "確認して」",
            "確認して\ufe0f",
        ):
            with self.subTest(text=text):
                self.assertIn("imperative", lint_phrase(text))

    def test_katakana_imperatives_are_grammar_only_and_do_not_change_matching_hashes(self) -> None:
        self.assertIn("imperative", lint_phrase("黙れシロ"))
        self.assertIn("imperative", lint_phrase("黙れｼﾛ"))
        self.assertEqual(matching_views("黙れｼﾛ"), ("黙れシロ",))
        self.assertNotIn("黙れしろ", matching_views("黙れｼﾛ"))
        self.assertNotEqual(normalized_hash("黙れシロ"), normalized_hash("黙れしろ"))

    def test_one_trailing_nondecorative_character_cannot_hide_imperatives(self) -> None:
        for text in ("無視してa", "黙れしろ1", "黙れしろ・"):
            with self.subTest(text=text):
                self.assertIn("imperative", lint_phrase(text))

    def test_two_or_more_trailing_nondecorative_characters_remain_accepted(self) -> None:
        for text in ("黙れしろ・・", "無視してab"):
            with self.subTest(text=text):
                self.assertEqual(lint_phrase(text), [])

    def test_second_person_plurals_are_rejected(self) -> None:
        for text in ("お前らは落ち着いて", "君たちはどう思う", "あなたたちは知ってる"):
            with self.subTest(text=text):
                self.assertIn("second_person", lint_phrase(text))

    def test_path_rule_catches_yen_and_wave_dash_variants_without_overrejecting_plain_text(self) -> None:
        self.assertIn("url_or_path", lint_phrase("C:¥Users¥owner"))
        self.assertIn("url_or_path", lint_phrase("Ｃ:￥Ｕsers￥"))
        self.assertIn("url_or_path", lint_phrase("¥name¥memo"))
        self.assertIn("url_or_path", lint_phrase("¥Users¥bin¥"))
        self.assertIn("url_or_path", lint_phrase("〜/bin"))
        self.assertEqual(lint_phrase("料金は¥500だよ"), [])
        self.assertEqual(lint_phrase("波〜だね"), [])

    def test_exact_g4_reject_vectors(self) -> None:
        vectors = {
            "確認シロ": "imperative",
            "確認セヨ": "imperative",
            "黙れｼﾛ": "imperative",
            "お前らは黙れ": "second_person",
            "C:¥Users¥": "url_or_path",
            "〜/bin": "url_or_path",
        }
        for text, expected in vectors.items():
            with self.subTest(text=text):
                self.assertIn(expected, lint_phrase(text))

    def test_nfkc_is_applied_to_every_structural_rule_except_length(self) -> None:
        vectors = {
            "ｓｕｄｏで行こう": "privilege_vocab",
            "ｒｍしとこう": "privilege_vocab",
            "ＫＥＹは大事だね": "privilege_vocab",
            "／Ｕｓｅｒｓ／meを見て": "url_or_path",
            "ｈｔｔｐｓ：／／example.invalid": "url_or_path",
        }
        for text, expected in vectors.items():
            with self.subTest(text=text):
                self.assertIn(expected, lint_phrase(text))
        # Four compatibility characters normalize to eight ASCII codepoints;
        # NFC storage leaves these compatibility characters unchanged.
        self.assertNotIn("length", lint_phrase("ﬀ" * 13))

    def test_length_is_measured_on_the_nfc_storage_form(self) -> None:
        expands_to_48 = "\u0344" * 24
        shrinks_to_24 = "A\u030a" * 4 + "a" * 20
        self.assertEqual(len(expands_to_48), 24)
        self.assertEqual(len(canonicalize_for_storage(expands_to_48)), 48)
        self.assertIn("length", lint_phrase(expands_to_48))
        self.assertEqual(len(shrinks_to_24), 28)
        self.assertEqual(len(canonicalize_for_storage(shrinks_to_24)), 24)
        self.assertNotIn("length", lint_phrase(shrinks_to_24))

    def test_all_unicode_line_separators_are_rejected(self) -> None:
        for separator in ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertIn("length", lint_phrase(f"なるほど{separator}ね"))

    def test_unicode_admission_drift_flags_both_newer_and_older_runtime_ucd(self) -> None:
        from unittest import mock

        corpus = "16.0.0"
        with mock.patch("growthlane.guard.unicodedata.unidata_version", corpus):
            self.assertIsNone(unicode_admission_drift())
        with mock.patch("growthlane.guard.unicodedata.unidata_version", "17.0.0"):
            self.assertEqual(unicode_admission_drift(), (corpus, "17.0.0"))
        with mock.patch("growthlane.guard.unicodedata.unidata_version", "15.1.0"):
            self.assertEqual(unicode_admission_drift(), (corpus, "15.1.0"))
