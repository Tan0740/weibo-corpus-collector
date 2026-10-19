import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector


def post(username, text, identifier="1"):
    return collector.SearchPost(identifier, username, "offline", text, "offline")


class CollectorTests(unittest.TestCase):
    def test_full_text_and_publication_timestamp(self):
        from lxml import html
        tree = html.fromstring('''<div class="card-wrap">
          <a class="name" nick-name="测试用户">测试用户</a>
          <p node-type="feed_list_content">那很好了...展开全文</p>
          <p node-type="feed_list_content_full">前文。那很好了。后文完整语境。</p>
          <p class="from"><a href="https://weibo.com/123/AbC123" date="1792396800000">今天 16:00</a></p>
        </div>''')
        posts = collector.parse_search_posts(tree, "https://s.weibo.com/realtime?q=test")
        self.assertEqual(len(posts), 1)
        rows = collector.collect_from_post_text(posts[0], re.compile(collector.DEFAULT_PATTERN), set(), 1, 1)
        self.assertEqual(rows[0].username, "测试用户")
        self.assertEqual(rows[0].post_text, "前文。那很好了。后文完整语境。")
        self.assertEqual(rows[0].comment_text, rows[0].post_text)
        self.assertEqual(rows[0].full_text_status, "full")
        self.assertEqual(rows[0].published_at, "2026-10-19T16:00:00+08:00")
        self.assertEqual(rows[0].published_time_raw, "今天 16:00")

    def test_relative_time_not_fabricated(self):
        from lxml import html
        tree = html.fromstring('<div><time>5分钟前</time><p node-type="feed_list_content">那很好了</p></div>')
        metadata = collector.extract_post_metadata(tree)
        self.assertEqual(metadata, {"published_at": "", "published_time_raw": "5分钟前"})
        self.assertEqual(collector.extract_search_text(tree)[1], "visible_only")

    def test_old_csv_migration_preserves_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.csv"
            path.write_text("number,username,corpus,post_text\n1,user,那很好了,完整正文\n", encoding="utf-8-sig")
            collector.append_rows(str(path), [])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = list(csv.DictReader(stream))[0]
            self.assertEqual(row["post_text"], "完整正文")
            self.assertEqual(row["username"], "user")
            self.assertEqual(row["published_at"], "")
            self.assertTrue(path.with_suffix(".csv.bak").is_file())

    def test_search_url_requires_correct_host_and_keyword(self):
        self.assertTrue(collector.is_keyword_search(collector.browser_realtime_search_url("那很了"), "那很了"))
        self.assertFalse(collector.is_keyword_search("https://me.weibo.com/data/overview", "那很了"))
        self.assertFalse(collector.is_keyword_search(collector.browser_realtime_search_url("太了"), "那很了"))

    def test_login_redirect_returns_to_search_without_enter(self):
        args = collector.parse_args([])
        page = MagicMock()
        page.url = "https://me.weibo.com/data/overview"
        page.is_closed.return_value = False
        page.get_by_role.return_value.count.return_value = 0
        page.goto.side_effect = lambda url, **kwargs: setattr(page, "url", url)
        context = MagicMock()
        context.pages = [page]
        context.cookies.return_value = [{"name": "SUB", "value": "fictional"}]
        with patch.object(collector, "require_lxml_html"), \
             patch.object(collector, "parse_search_posts", return_value=[post("a", "那很好吃了")]), \
             patch("builtins.input", side_effect=AssertionError("must not require Enter")):
            selected, _, url = collector.wait_for_realtime_results(context, page, args)
        self.assertIs(selected, page)
        self.assertTrue(collector.is_keyword_search(url, args.keyword))
        page.goto.assert_called_once()

    def test_replaced_login_tab_selects_new_search_tab(self):
        old, new = MagicMock(), MagicMock()
        new.is_closed.return_value = False
        new.url = collector.browser_realtime_search_url("那很了")
        context = MagicMock()
        context.pages = [new]
        self.assertIs(collector.select_search_page(context, old, "那很了"), new)

    def test_closed_browser_stops_with_clear_message(self):
        context = MagicMock()
        context.pages = []
        with self.assertRaisesRegex(SystemExit, "浏览器已关闭"):
            collector.select_search_page(context, MagicMock(), "那很了")

    def test_login_wait_is_bounded(self):
        args = collector.parse_args(["--login-timeout", "1"])
        with patch.object(collector, "require_lxml_html"), \
             patch.object(collector.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(SystemExit, "未识别到"):
                collector.wait_for_realtime_results(MagicMock(), MagicMock(), args)

    def test_login_form_is_not_interrupted_by_navigation(self):
        args = collector.parse_args([])
        page = MagicMock()
        page.url = "https://passport.weibo.com/login"
        page.is_closed.return_value = False
        page.wait_for_timeout.side_effect = KeyboardInterrupt
        context = MagicMock()
        context.pages = [page]
        context.cookies.return_value = [{"name": "SUB", "value": "fictional"}]
        with patch.object(collector, "require_lxml_html"), self.assertRaises(KeyboardInterrupt):
            collector.wait_for_realtime_results(context, page, args)
        page.goto.assert_not_called()

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
