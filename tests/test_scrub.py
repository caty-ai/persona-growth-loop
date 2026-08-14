from __future__ import annotations

import unittest

from collectors.claude_code.scrub import scrub_record


class ScrubTests(unittest.TestCase):
    def test_secret_scan_is_independent_and_drops_record(self) -> None:
        record = {"text": "ghp_DUMMY_VALUE", "len": 15}
        self.assertIsNone(scrub_record(record))

    def test_secret_scan_catches_matching_ignorable_interruptions(self) -> None:
        for separator in ("\u200d", "\u200b", "\u034f"):
            for text in (
                f"s{separator}k-DUMMY",
                f"g{separator}hp_DUMMY",
                f"A{separator}KIA-DUMMY",
                ("A" + separator) * 40,
            ):
                with self.subTest(separator=ascii(separator), text=text[:20]):
                    self.assertIsNone(scrub_record({"text": text, "len": len(text)}))

    def test_secret_scan_folds_dash_confusables_before_matching(self) -> None:
        for text in (
            "sk\u2010abcd",
            "sk\u2011abcd",
            "sk\u2013DUMMY",
            "sk\u2014DUMMY",
            "sk\u2212DUMMY",
            "ghp_\u2014DUMMY",
            "github_pat_\u2212DUMMY",
            "\u2010----BEGIN",
        ):
            with self.subTest(text=text):
                self.assertIsNone(scrub_record({"text": text, "len": len(text)}))

    def test_email_and_japanese_phone_variants_are_masked_and_len_recomputed(self) -> None:
        for email, phone in (
            ("hanako@example.invalid", "090-1234-5678"),
            ("hanako@localhost", "03-1234-5678"),
            ("hanako@barserver", "0312345678"),
            ("hanako@barserver", "+81-90-1234-5678"),
            ("hanako@barserver", "+81 90 1234 5678"),
            ("hanako@barserver", "+819012345678"),
            ("hanako@barserver", "+81(90)12345678"),
            ("hanako@barserver", "+81 (0)90 1234 5678"),
        ):
            with self.subTest(email=email, phone=phone):
                original = {"text": f"{email} {phone}", "len": 999}
                scrubbed = scrub_record(original)
                self.assertIsNotNone(scrubbed)
                assert scrubbed is not None
                self.assertEqual(scrubbed["text"], "***@*** 0**-****-****")
                self.assertEqual(scrubbed["len"], len(scrubbed["text"]))
                self.assertEqual(original["len"], 999)

    def test_matching_view_only_pii_is_dropped_fail_closed(self) -> None:
        for text in (
            "連絡先はhanako\u200b@example.invalidです",
            "電話は０９０-１２３４-５６７８です",
            "電話は090\u200b1234\u200b5678です",
            "連絡先はhanako@example.invalid、別口はtaro\u200b@example.invalidです",
        ):
            with self.subTest(text=text):
                self.assertIsNone(scrub_record({"text": text, "len": len(text)}))

    def test_matching_ignored_email_domain_separator_drops_without_residue(self) -> None:
        for text in (
            "foo@exam\u200bplecorp.com です",
            "foo@exa\u200bmple.com に送って",
            "foo@exa mple.com に送って",
        ):
            with self.subTest(text=text):
                self.assertIsNone(scrub_record({"text": text, "len": len(text)}))

    def test_matching_view_mask_residue_before_or_after_mask_drops(self) -> None:
        for text in (
            "foo\u200bbar@example.com",
            "taro.\u200bsuzuki@example.com",
            "foo@bar.com\u200bjp",
            "連絡は foo@exa\u200bmple まで",
            "user@compa\u200bny",
            "fo\u200bo@example.com",
            "foo@example\u200b.com",
            "foo@sub\u200b.example.com",
            "foo@example.\u200bcom",
            "contact@x.\u200bexamplecorp.co.jp",
            "\u3054\u9023\u7d61\u306f contact@x.\u3000examplecorp.co.jp \u307e\u3067",
            "mail foo@bar. example.co.jp \u3067\u3059",
        ):
            with self.subTest(text=text):
                self.assertIsNone(scrub_record({"text": text, "len": len(text)}))

    def test_adjacent_clean_emails_are_masked_and_kept(self) -> None:
        scrubbed = scrub_record(
            {"text": "a@example.com b@example.com", "len": 999}
        )
        self.assertIsNotNone(scrubbed)
        assert scrubbed is not None
        self.assertEqual(scrubbed["text"], "***@*** ***@***")
        self.assertEqual(scrubbed["len"], len(scrubbed["text"]))


if __name__ == "__main__":
    unittest.main()
