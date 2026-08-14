from __future__ import annotations

import unittest

from collectors.claude_code.filter import filter_fragment, filter_user_turn


class FilterTests(unittest.TestCase):
    def _entry(self, content: object, **extra: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "type": "user",
            "message": {"content": content},
            "isSidechain": False,
        }
        entry.update(extra)
        return entry

    def test_plain_string_and_text_blocks_are_included(self) -> None:
        stats: dict[str, int] = {}
        self.assertEqual(filter_user_turn(self._entry("うん"), stats), ["うん"])
        self.assertEqual(
            filter_user_turn(
                self._entry([{"type": "text", "text": "そうだね"}]), stats
            ),
            ["そうだね"],
        )
        self.assertEqual(stats["population"], 2)

    def test_tool_results_and_sidechains_are_excluded_before_population(self) -> None:
        stats: dict[str, int] = {}
        self.assertEqual(
            filter_user_turn(
                self._entry([{"type": "tool_result", "content": "hidden"}]), stats
            ),
            [],
        )
        self.assertEqual(
            filter_user_turn(
                self._entry([{"type": "text", "text": "hidden"}], toolUseResult={}),
                stats,
            ),
            [],
        )
        self.assertEqual(
            filter_user_turn(
                self._entry([{"type": "text", "text": "hidden"}], isSidechain=True),
                stats,
            ),
            [],
        )
        self.assertEqual(stats.get("population", 0), 0)

    def test_mixed_text_and_tool_result_poison_the_whole_turn(self) -> None:
        stats: dict[str, int] = {}
        fragments = filter_user_turn(
            self._entry(
                [
                    {"type": "text", "text": "safe-looking"},
                    {"type": "tool_result", "content": "sensitive"},
                    {"type": "text", "text": "also safe-looking"},
                ]
            ),
            stats,
        )
        self.assertEqual(fragments, [])
        self.assertEqual(stats["drop_rule_2_tool_result"], 2)
        self.assertEqual(stats.get("population", 0), 0)

    def test_removal_rules_and_unclosed_fence(self) -> None:
        stats: dict[str, int] = {}
        text = (
            "残す\n<system-reminder>remove\nall</system-reminder>\n"
            "<command-name>remove line</command-name>\n"
            "https://example.invalid/remove\n残る"
        )
        self.assertEqual(filter_fragment(text, stats), "残す\n\n残る")
        self.assertEqual(filter_fragment("before ```python\nprint('x')", stats), "before")
        self.assertEqual(filter_fragment("残す<system-reminder>unclosed", stats), "残す")
        self.assertIsNone(filter_fragment("</command-args>", stats))

    def test_code_point_boundary_and_rule_order(self) -> None:
        stats: dict[str, int] = {}
        self.assertEqual(filter_fragment("あ" * 240, stats), "あ" * 240)
        self.assertIsNone(filter_fragment("AKIA" + "あ" * 237, stats))
        self.assertEqual(stats["drop_rule_7_length"], 1)
        self.assertNotIn("drop_rule_8_secret_like", stats)

    def test_secret_patterns_fail_closed(self) -> None:
        for value in (
            "AKIADUMMY",
            "sk-DUMMY",
            "ghp_DUMMY",
            "github_pat_DUMMY",
            "xoxDUMMY",
            "-----BEGIN DUMMY",
            "prefix eyJDUMMY",
            "A" * 40,
            ):
                with self.subTest(value=value):
                    self.assertIsNone(filter_fragment(value, {}))

    def test_secret_patterns_fold_dash_confusables_before_matching(self) -> None:
        for value in (
            "sk\u2010abcd",
            "sk\u2011abcd",
            "sk\u2013DUMMY",
            "sk\u2014DUMMY",
            "sk\u2212DUMMY",
            "ghp_\u2014DUMMY",
            "github_pat_\u2212DUMMY",
            "\u2010----BEGIN",
        ):
            with self.subTest(value=value):
                stats: dict[str, int] = {}
                self.assertIsNone(filter_fragment(value, stats))
                self.assertEqual(stats.get("drop_rule_8_secret_like"), 1)

    def test_secret_patterns_cannot_be_interrupted_by_matching_ignorables(self) -> None:
        for separator in ("\u200d", "\u200b", "\u034f"):
            for value in (
                f"s{separator}k-DUMMY",
                f"g{separator}hp_DUMMY",
                f"A{separator}KIA-DUMMY",
                ("A" + separator) * 40,
            ):
                with self.subTest(separator=ascii(separator), value=value[:20]):
                    stats: dict[str, int] = {}
                    self.assertIsNone(filter_fragment(value, stats))
                    self.assertEqual(stats.get("drop_rule_8_secret_like"), 1)

    def test_raw_pii_is_masked_but_matching_view_only_pii_drops_fail_closed(self) -> None:
        stats: dict[str, int] = {}
        self.assertEqual(
            filter_fragment("連絡はhanako@example.invalid、電話は090-1234-5678です", stats),
            "連絡は***@***、電話は0**-****-****です",
        )
        dropped = filter_fragment("連絡はhanako\u200b@example.invalidです", stats)
        self.assertIsNone(dropped)
        self.assertEqual(stats.get("drop_rule_8_secret_like"), 1)
        mixed = filter_fragment(
            "連絡はhanako@example.invalid、別口はtaro\u200b@example.invalidです",
            stats,
        )
        self.assertIsNone(mixed)
        self.assertEqual(stats.get("drop_rule_8_secret_like"), 2)

    def test_matching_ignored_email_domain_separator_drops_without_residue(self) -> None:
        for text in (
            "foo@exam\u200bplecorp.com です",
            "foo@exa\u200bmple.com に送って",
            "foo@exa mple.com に送って",
        ):
            with self.subTest(text=text):
                stats: dict[str, int] = {}
                self.assertIsNone(filter_fragment(text, stats))
                self.assertEqual(stats.get("drop_rule_8_secret_like"), 1)

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
                stats: dict[str, int] = {}
                self.assertIsNone(filter_fragment(text, stats))
                self.assertEqual(stats.get("drop_rule_8_secret_like"), 1)

    def test_adjacent_clean_emails_are_masked_and_kept(self) -> None:
        self.assertEqual(
            filter_fragment("a@example.com b@example.com", {}),
            "***@*** ***@***",
        )

    def test_wave_dash_home_and_yen_windows_paths_are_removed_without_overdropping_plain_wave_text(self) -> None:
        stats: dict[str, int] = {}
        self.assertEqual(filter_fragment("いいね〜", stats), "いいね〜")
        self.assertIsNone(filter_fragment("〜/bin", stats))
        self.assertIsNone(filter_fragment("C:¥Users¥owner", stats))
        self.assertIsNone(filter_fragment("Ｃ:￥Ｕsers￥", stats))
        self.assertIsNone(filter_fragment("¥name¥notes", stats))
        self.assertIsNone(filter_fragment("¥Users¥bin¥", stats))
        self.assertIsNone(filter_fragment("path C:¥Users¥owner", stats))
        self.assertIsNone(filter_fragment("path ¥name¥notes", stats))
        self.assertEqual(filter_fragment("料金は¥500だよ", stats), "料金は¥500だよ")
        self.assertEqual(filter_fragment("¥500と¥600です", stats), "¥500と¥600です")
        self.assertEqual(filter_fragment("予算は¥300と¥450", stats), "予算は¥300と¥450")
        self.assertEqual(
            filter_fragment("ランチは¥500でコーヒーは¥300", stats),
            "ランチは¥500でコーヒーは¥300",
        )


if __name__ == "__main__":
    unittest.main()
