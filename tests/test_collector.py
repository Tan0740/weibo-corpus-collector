import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector


def post(username, text, identifier="1"):
    return collector.SearchPost(identifier, username, "offline", text, "offline")


class CollectorTests(unittest.TestCase):
    def test_user_expression_deduplication(self):
        seen = set()
        pattern = re.compile(collector.DEFAULT_PATTERN)
        first = collector.collect_from_post_text(post("a", "那很好吃了"), pattern, seen, 10, 1)
        duplicate = collector.collect_from_post_text(post("a", "那很好吃了"), pattern, seen, 10, 2)
        other = collector.collect_from_post_text(post("b", "那很好吃了"), pattern, seen, 10, 2)
        self.assertEqual([len(first), len(duplicate), len(other)], [1, 0, 1])

    def test_custom_pattern_and_remaining_limit(self):
        pattern = re.compile(r"太[^，。！？、\s]{1,12}?了")
        rows = collector.collect_from_post_text(post("a", "太好吃了"), pattern, set(), 1, 7)
        self.assertEqual((rows[0].corpus, rows[0].number), ("太好吃了", 7))
        self.assertEqual(collector.collect_from_post_text(post("a", "太好了"), pattern, set(), 0, 8), [])

    def test_collection_stops_exactly_at_limit_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "corpus.csv"
            args = collector.parse_args(["--output", str(output), "--limit", "2"])
            with patch.object(collector, "read_cookie", return_value="fictional"), \
                 patch.object(collector, "build_session"), \
                 patch.object(collector, "fetch_tree"), \
                 patch.object(collector, "parse_search_posts", return_value=[
                     post("a", "那很好吃了", "1"),
                     post("b", "那很好吃了", "2"),
                     post("c", "那很期待了", "3"),
                 ]), patch.object(collector, "wait") as wait:
                collector.collect(args)
                wait.assert_not_called()
            self.assertEqual(collector.load_row_count(str(output)), 2)
            with open(output.with_name("corpus_frequency.csv"), encoding="utf-8-sig") as stream:
                frequency = list(csv.DictReader(stream))
            self.assertEqual((frequency[0]["frequency"], frequency[0]["unique_users"]), ("2", "2"))
            self.assertTrue(output.with_name("corpus_frequency.svg").is_file())
            with patch.object(collector, "read_cookie") as cookie:
                collector.collect(args)
                cookie.assert_not_called()
            self.assertEqual(collector.load_row_count(str(output)), 2)

    def test_interrupt_preserves_data_and_generates_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "corpus.csv"
            args = collector.parse_args(["--output", str(output), "--limit", "0"])
            with patch.object(collector, "read_cookie", return_value="fictional"), \
                 patch.object(collector, "build_session"), \
                 patch.object(collector, "fetch_tree"), \
                 patch.object(collector, "parse_search_posts", return_value=[post("a", "那很期待了")]), \
                 patch.object(collector, "wait", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    collector.collect(args)
            self.assertEqual(collector.load_row_count(str(output)), 1)
            self.assertTrue(output.with_name("corpus_frequency.csv").is_file())

    def test_invalid_arguments_fail_before_login(self):
        for arguments in (["--pattern", "["], ["--limit", "-1"], ["--min-delay", "-1"], ["--keyword", ""]):
            with self.subTest(arguments=arguments), patch.object(collector, "collect") as collect:
                with self.assertRaises(SystemExit):
                    collector.main(arguments)
                collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
