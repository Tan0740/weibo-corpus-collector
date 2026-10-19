#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collect matching corpus records from Weibo search result post text.

Interactive mode reads realtime search pages after manual login.
Records are deduplicated by username and matched expression.
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit


BASE_URL = "https://weibo.cn"
DEFAULT_PATTERN = r"那很[^，。！？、\s]{1,12}?了"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class SearchPost:
    weibo_id: str
    username: str
    comment_url: str
    post_text: str
    source_url: str
    published_at: str = ""
    published_time_raw: str = ""
    full_text_status: str = "visible_only"


@dataclass(frozen=True)
class CorpusRow:
    number: int
    username: str
    corpus: str
    comment_text: str
    source: str
    note: str
    weibo_id: str
    comment_page_url: str
    post_text: str
    published_at: str = ""
    published_time_raw: str = ""
    full_text_status: str = "visible_only"


@dataclass(frozen=True)
class CommentItem:
    username: str
    text: str
    source: str


def clean_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    value = value.replace("\u200b", "")
    return value.strip()


def read_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    cookie = os.environ.get(args.cookie_env, "").strip()
    if cookie:
        return cookie
    raise SystemExit(
        "没有找到微博 cookie。请使用 --cookie、--cookie-file，"
        f"或设置环境变量 {args.cookie_env}。"
    )


def normalize_cookie(cookie: str) -> str:
    cookie = cookie.strip()
    cookie = re.sub(r"^cookie\s*:\s*", "", cookie, flags=re.IGNORECASE)
    cookie = re.sub(r"[\r\n]+", "; ", cookie)

    pairs: list[str] = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue

        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        try:
            name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SystemExit(
                f"cookie 名称里有非 ASCII 字符：{name!r}。"
                "请重新从浏览器开发者工具复制 Request Headers 里的 Cookie。"
            ) from exc

        # HTTP headers can only be sent as latin-1 in Python's http.client.
        # Browser cookies are normally ASCII; raw Chinese values need percent-encoding.
        value = quote(value, safe="!#$%&'()*+-./:<=>?@[]^_`{|}~")
        pairs.append(f"{name}={value}")

    if not pairs:
        raise SystemExit(
            "cookie 内容看起来不对：没有找到 name=value 形式的字段。"
            "请复制浏览器请求头里的完整 Cookie，而不是占位文字或页面内容。"
        )

    normalized = "; ".join(pairs)
    try:
        normalized.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise SystemExit(
            "cookie 仍然包含无法发送的字符。"
            "建议重新复制浏览器 Request Headers 中的 Cookie 字段。"
        ) from exc
    return normalized


def require_requests() -> Any:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少依赖 requests，请先运行：python3 -m pip install requests lxml") from exc
    return requests


def build_session(cookie: str) -> Any:
    requests = require_requests()
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.headers["Cookie"] = normalize_cookie(cookie)
    return session


def build_session_from_cookie_pairs(
    cookies: list[dict[str, Any]],
    user_agent: str = "",
) -> Any:
    cookie = "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if item.get("name") and item.get("value") is not None
    )
    if not cookie:
        raise SystemExit("没有从浏览器里读到 cookie。请确认你已经在打开的浏览器中登录微博。")
    session = build_session(cookie)
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session


def require_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "缺少依赖 playwright。请先运行：\n"
            "python3 -m pip install playwright\n"
            "python3 -m playwright install chromium"
        ) from exc
    return sync_playwright


def realtime_tab_is_active(page: Any) -> bool:
    if urlsplit(page.url).path.rstrip("/") == "/realtime":
        return True
    links = page.get_by_role("link", name="实时", exact=True)
    if not links.count():
        return False
    return bool(
        links.first.evaluate(
            """element => {
                const nodes = [element, element.parentElement, element.parentElement?.parentElement];
                return nodes.some(node => {
                    if (!node) return false;
                    const classes = String(node.className || '').split(/\\s+/);
                    return classes.some(name => ['cur', 'current', 'active', 'selected'].includes(name))
                        || node.getAttribute('aria-current') === 'page';
                });
            }"""
        )
    )


def is_keyword_search(url: str, keyword: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.hostname == "s.weibo.com"
        and parsed.path.rstrip("/") in {"/realtime", "/weibo"}
        and dict(parse_qsl(parsed.query)).get("q") == keyword
    )


def expand_search_posts(page: Any) -> None:
    links = page.get_by_role("link", name=re.compile(r"^(展开全文|展开)$"))
    for index in reversed(range(min(links.count(), 50))):
        link = links.nth(index)
        if link.is_visible():
            try:
                link.click(timeout=3000)
                page.wait_for_timeout(300)
            except Exception:
                if page.is_closed():
                    raise
                print("[正文] 部分长微博未能展开，将标注为 visible_only。")


def select_search_page(context: Any, page: Any, keyword: str) -> Any:
    pages = [item for item in context.pages if not item.is_closed()]
    if not pages:
        raise SystemExit("[停止] 浏览器已关闭；重新运行即可，已保存的语料会继续保留。")
    for item in reversed(pages):
        if is_keyword_search(item.url, keyword):
            return item
    return page if page in pages else context.new_page()


def wait_for_realtime_results(context: Any, page: Any, args: argparse.Namespace) -> tuple[Any, Any, str]:
    html = require_lxml_html()
    deadline = time.monotonic() + args.login_timeout
    next_navigation = 0.0
    target_url = browser_realtime_search_url(args.keyword)
    print(f"[浏览器] 自动打开“{args.keyword}”实时搜索；如需登录或验证码，请在浏览器完成，无需回终端按回车。")
    while time.monotonic() < deadline:
        page = select_search_page(context, page, args.keyword)
        current_url = page.url
        if is_keyword_search(current_url, args.keyword):
            tree = html.fromstring(page.content())
            posts = parse_search_posts(tree, current_url)
            if realtime_tab_is_active(page) and posts:
                expand_search_posts(page)
                tree = html.fromstring(page.content())
                print(f"[浏览器] 实时搜索已就绪，识别到 {len(posts)} 个候选帖子。")
                return page, tree, current_url
            if not realtime_tab_is_active(page) and time.monotonic() >= next_navigation:
                page.goto(target_url, wait_until="domcontentloaded")
                next_navigation = time.monotonic() + 15
        else:
            # Allow login to finish without repeatedly navigating away from its form.
            has_login = any(
                item.get("name") == "SUB" and item.get("value")
                for item in context.cookies(["https://weibo.com", "https://s.weibo.com"])
            )
            parsed = urlsplit(current_url)
            authenticating = (
                parsed.hostname in {"passport.weibo.com", "passport.weibo.cn", "login.sina.com.cn"}
                or "login" in parsed.path.lower()
                or "verify" in parsed.path.lower()
            )
            if has_login and not authenticating and time.monotonic() >= next_navigation:
                print("[浏览器] 检测到登录状态，自动返回指定关键词的实时搜索页。")
                page.goto(target_url, wait_until="domcontentloaded")
                next_navigation = time.monotonic() + 15
        try:
            page.wait_for_timeout(2000)
        except Exception:
            # A login flow may replace its tab. A closed browser is handled above.
            if not page.is_closed():
                raise
    raise SystemExit(
        f"[停止] {args.login_timeout} 秒内未识别到指定关键词的实时结果。"
        "可能尚未登录、需要验证、搜索无结果或页面结构已变化。"
        "可用 --login-timeout 600 延长等待，完成登录后重新运行。"
    )


def open_interactive_browser(args: argparse.Namespace) -> tuple[Any, Any, str, Any, Any, Any]:
    sync_playwright = require_playwright()
    profile_dir = Path(args.browser_profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = sync_playwright().start()
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto(browser_realtime_search_url(args.keyword), wait_until="domcontentloaded")
        page, tree, current_url = wait_for_realtime_results(context, page, args)

        cookies = context.cookies()
        user_agent = page.evaluate("navigator.userAgent")
        session = build_session_from_cookie_pairs(cookies, user_agent=user_agent)
        return session, tree, current_url, playwright, context, page
    except BaseException:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass
        raise


def wait(args: argparse.Namespace) -> None:
    time.sleep(random.uniform(args.min_delay, args.max_delay))


def require_lxml_html() -> Any:
    try:
        from lxml import html
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少依赖 lxml，请先运行：python3 -m pip install requests lxml") from exc
    return html


def fetch_tree(session: Any, url: str, timeout: int) -> Any:
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"请求失败：{url}\n原因：{exc}") from exc
    if "登录" in response.text and "密码" in response.text:
        raise RuntimeError("当前 cookie 可能已失效：页面返回了登录表单。")
    return require_lxml_html().fromstring(response.content)


def absolute_url(url: str) -> str:
    return urljoin(BASE_URL, url)


def extract_post_text(div: Any) -> str:
    nodes = div.xpath(".//span[contains(@class, 'ctt')]")
    if nodes:
        return clean_text(" ".join(nodes[0].xpath(".//text()")))
    return clean_text(" ".join(div.xpath(".//text()")))


def extract_weibo_permalink(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parts = urlsplit(href)
    if parts.netloc not in {"weibo.com", "www.weibo.com"}:
        return ""

    path_parts = [part for part in parts.path.split("/") if part]
    if "status" in path_parts:
        status_index = path_parts.index("status")
        if status_index + 1 < len(path_parts):
            candidate = path_parts[status_index + 1]
            return candidate if re.fullmatch(r"[A-Za-z0-9]+", candidate) else ""

    if len(path_parts) == 2 and path_parts[0] not in {"u", "n", "p", "tv"}:
        candidate = path_parts[1]
        return candidate if re.fullmatch(r"[A-Za-z0-9]+", candidate) else ""
    return ""


def extract_post_username(container: Any) -> str:
    selectors = [
        "(.//*[@nick-name])[1]/@nick-name",
        "(.//a[contains(concat(' ', normalize-space(@class), ' '), ' name ')])[1]//text()",
        "(.//a[@usercard])[1]//text()",
        "(.//a[starts-with(@href, '/u/') or contains(@href, 'weibo.com/u/')])[1]//text()",
    ]
    for selector in selectors:
        values = container.xpath(selector)
        username = clean_text(" ".join(str(value) for value in values))
        if username:
            return username
    return ""


def extract_post_metadata(container: Any) -> dict[str, str]:
    nodes = container.xpath(".//a[@date] | .//time | .//*[contains(concat(' ', normalize-space(@class), ' '), ' from ')]//a[1] | .//span[@class='ct']")
    raw = ""
    published = ""
    for node in nodes:
        raw = raw or clean_text(" ".join(node.itertext()))
        stamp = node.get("date") or node.get("datetime") or node.get("title") or ""
        if stamp.isdigit() and len(stamp) in {10, 13}:
            seconds = int(stamp) / (1000 if len(stamp) == 13 else 1)
            published = datetime.fromtimestamp(seconds, timezone(timedelta(hours=8))).isoformat()
        elif re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", stamp):
            published = stamp
        if published:
            break
    if not published:
        match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", raw)
        published = match.group(0) if match else ""
    return {"published_at": published, "published_time_raw": raw}


def extract_search_text(container: Any) -> tuple[str, str]:
    full = container.xpath(".//*[@node-type='feed_list_content_full']")
    nodes = full or container.xpath(".//*[@node-type='feed_list_content']")
    if not nodes:
        nodes = container.xpath(".//p | .//article")
    text = clean_text(" ".join(nodes[0].itertext())) if nodes else ""
    return text, "full" if full else "visible_only"


def parse_search_posts(tree: Any, source_url: str) -> list[SearchPost]:
    posts: list[SearchPost] = []
    seen_ids: set[str] = set()
    for div in tree.xpath("//div[starts-with(@id, 'M_')]"):
        raw_id = div.xpath("string(@id)").strip()
        weibo_id = raw_id[2:] if raw_id.startswith("M_") else raw_id
        if not weibo_id:
            continue

        comment_links = div.xpath(
            ".//a[contains(@href, '/comment/') and "
            "(contains(text(), '评论') or contains(@class, 'cc'))]/@href"
        )
        if not comment_links:
            continue

        seen_ids.add(weibo_id)
        posts.append(
            SearchPost(
                weibo_id=weibo_id,
                username=extract_post_username(div),
                comment_url=absolute_url(comment_links[-1].split("#")[0]),
                post_text=extract_post_text(div),
                source_url=source_url,
                **extract_post_metadata(div),
            )
        )

    desktop_cards = tree.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' card-wrap ') "
        "or @action-type='feed_list_item']"
    )
    for card in desktop_cards:
        permalink = ""
        candidate_links = card.xpath(
            ".//a[contains(@href, 'weibo.com/') and "
            "(@date or contains(concat(' ', normalize-space(@class), ' '), ' from '))]/@href"
        )
        if not candidate_links:
            candidate_links = card.xpath(".//a[contains(@href, 'weibo.com/')]/@href")
        for href in candidate_links:
            permalink = extract_weibo_permalink(href)
            if permalink:
                break
        if not permalink or permalink in seen_ids:
            continue

        post_text, full_text_status = extract_search_text(card)
        seen_ids.add(permalink)
        posts.append(
            SearchPost(
                weibo_id=permalink,
                username=extract_post_username(card),
                comment_url=f"{BASE_URL}/comment/{permalink}",
                post_text=post_text,
                source_url=source_url,
                full_text_status=full_text_status,
                **extract_post_metadata(card),
            )
        )

    # Newer realtime pages do not consistently expose card-wrap or mid.
    # Fall back to permalink anchors and use their nearest content container.
    for link in tree.xpath("//a[contains(@href, 'weibo.com/')]"):
        permalink = extract_weibo_permalink(link.xpath("string(@href)"))
        if not permalink or permalink in seen_ids:
            continue

        containers = link.xpath(
            "ancestor::div[.//p or .//article][1]"
        )
        container = containers[0] if containers else link.getparent()
        if container is None:
            continue

        post_text, full_text_status = extract_search_text(container)

        seen_ids.add(permalink)
        posts.append(
            SearchPost(
                weibo_id=permalink,
                username=extract_post_username(container),
                comment_url=f"{BASE_URL}/comment/{permalink}",
                post_text=post_text,
                source_url=source_url,
                full_text_status=full_text_status,
                **extract_post_metadata(container),
            )
        )
    return posts


def parse_next_page(tree: Any) -> str | None:
    links = tree.xpath("//a[contains(text(), '下页')]/@href")
    if not links:
        return None
    return absolute_url(links[0])


def is_comment_block(div: Any) -> bool:
    div_id = div.xpath("string(@id)")
    text = clean_text(" ".join(div.xpath(".//text()")))
    if not text:
        return False
    if div_id.startswith("C_"):
        return True
    if "回复" in text and ("来自" in text or "赞[" in text):
        return True
    return False


def normalize_comment_text(text: str) -> str:
    text = re.sub(r"^.*?:", "", text, count=1)
    text = re.sub(r"\s*(赞\[\d+\]|回复|举报|来自.+)$", "", text)
    return clean_text(text)


def parse_comment_text(div: Any) -> str:
    content_nodes = div.xpath(".//span[contains(@class, 'ctt')]")
    if content_nodes:
        return clean_text(" ".join(content_nodes[0].xpath(".//text()")))
    return normalize_comment_text(" ".join(div.xpath(".//text()")))


def parse_comment_username(div: Any) -> str:
    content_nodes = div.xpath(".//span[contains(@class, 'ctt')]")
    if content_nodes:
        author_links = content_nodes[0].xpath(
            "preceding::a["
            "starts-with(@href, '/u/') or starts-with(@href, 'https://weibo.cn/u/')"
            "][1]"
        )
        if author_links:
            return clean_text(" ".join(author_links[0].xpath(".//text()")))

    author_links = div.xpath(
        "./a[starts-with(@href, '/u/') or starts-with(@href, 'https://weibo.cn/u/')][1]"
    )
    if author_links:
        return clean_text(" ".join(author_links[0].xpath(".//text()")))
    return ""


def parse_comment_source(div: Any) -> str:
    meta = clean_text(" ".join(div.xpath(".//span[contains(@class, 'ct')]//text()")))
    if not meta:
        text = clean_text(" ".join(div.xpath(".//text()")))
        match = re.search(
            r"(?:\d{4}-)?\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}|"
            r"今天\s+\d{1,2}:\d{2}|刚刚",
            text,
        )
        if match:
            source_match = re.search(r"\s+来自\s+(.+?)(?:\s+(?:赞|回复|举报)\b|$)", text)
            return clean_text(source_match.group(1)) if source_match else ""
        return ""

    parts = re.split(r"\s+来自\s+", meta, maxsplit=1)
    return clean_text(parts[1]) if len(parts) > 1 else ""


def parse_comments(tree: Any) -> list[CommentItem]:
    comments: list[CommentItem] = []
    for div in tree.xpath("//div[contains(@class, 'c')]"):
        if not is_comment_block(div):
            continue
        text = parse_comment_text(div)
        if text and text not in {"还没有人评论哦", "评论"}:
            comments.append(
                CommentItem(
                    username=parse_comment_username(div),
                    text=text,
                    source=parse_comment_source(div),
                )
            )
    return comments


def load_seen(path: str) -> set[tuple[str, str]]:
    if not os.path.exists(path):
        return set()

    seen: set[tuple[str, str]] = set()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            seen.add((row.get("weibo_id", ""), row.get("comment_text", "")))
            if row.get("source") and row.get("corpus"):
                seen.add((row.get("source", ""), row.get("corpus", "")))
            if row.get("comment_page_url") and row.get("corpus"):
                seen.add((row.get("comment_page_url", ""), row.get("corpus", "")))
    return seen


def load_seen_user_corpus(path: str) -> set[tuple[str, str]]:
    if not os.path.exists(path):
        return set()

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return {
            (
                clean_text(row.get("username", "")),
                clean_text(row.get("corpus", "")),
            )
            for row in csv.DictReader(f)
            if clean_text(row.get("username", ""))
            and clean_text(row.get("corpus", ""))
        }


def load_next_number(path: str) -> int:
    if not os.path.exists(path):
        return 1

    max_number = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for index, row in enumerate(csv.DictReader(f), start=1):
            raw_number = row.get("number") or row.get("编号") or ""
            try:
                max_number = max(max_number, int(raw_number))
            except ValueError:
                max_number = max(max_number, index)
    return max_number + 1


def load_row_count(path: str) -> int:
    if not os.path.exists(path):
        return 0

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def ensure_table_schema(path: str, fieldnames: list[str]) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        old_fieldnames = reader.fieldnames or []
        if old_fieldnames == fieldnames:
            return
        rows = list(reader)

    backup_path = str(Path(path).with_suffix(Path(path).suffix + ".bak"))
    os.replace(path, backup_path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            migrated = {
                "number": row.get("number") or row.get("编号") or index,
                "username": row.get("username") or row.get("用户名") or "",
                "corpus": row.get("corpus") or row.get("matched_text") or row.get("语料") or "",
                "comment_text": row.get("comment_text") or row.get("完整评论") or "",
                "source": row.get("source") or row.get("来源") or row.get("comment_page_url") or "",
                "note": row.get("note") or row.get("备注") or "",
                "weibo_id": row.get("weibo_id") or "",
                "comment_page_url": row.get("comment_page_url") or "",
                "post_text": row.get("post_text") or "",
                "published_at": row.get("published_at") or "",
                "published_time_raw": row.get("published_time_raw") or "",
                "full_text_status": row.get("full_text_status") or "visible_only",
            }
            writer.writerow(migrated)
    print(f"[表格] 已升级旧表头，原文件备份为：{backup_path}")


def append_rows(path: str, rows: Iterable[CorpusRow]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "number",
        "username",
        "corpus",
        "comment_text",
        "source",
        "note",
        "weibo_id",
        "comment_page_url",
        "post_text",
        "published_at",
        "published_time_raw",
        "full_text_status",
    ]
    ensure_table_schema(path, fieldnames)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def frequency_output_paths(path: str) -> tuple[Path, Path]:
    source = Path(path)
    stem = source.with_suffix("")
    return (
        Path(f"{stem}_frequency.csv"),
        Path(f"{stem}_frequency.svg"),
    )


def write_frequency_outputs(path: str, chart_limit: int = 30) -> None:
    if not os.path.exists(path):
        return

    corpus_users: dict[str, set[str]] = {}
    corpus_counts: dict[str, int] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            corpus = clean_text(row.get("corpus", ""))
            username = clean_text(row.get("username", ""))
            if not corpus:
                continue
            corpus_counts[corpus] = corpus_counts.get(corpus, 0) + 1
            corpus_users.setdefault(corpus, set())
            if username:
                corpus_users[corpus].add(username)

    ranked = sorted(
        corpus_counts,
        key=lambda corpus: (-corpus_counts[corpus], corpus),
    )
    frequency_csv, frequency_svg = frequency_output_paths(path)
    with open(frequency_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["rank", "corpus", "frequency", "unique_users"])
        for rank, corpus in enumerate(ranked, start=1):
            writer.writerow(
                [
                    rank,
                    corpus,
                    corpus_counts[corpus],
                    len(corpus_users[corpus]),
                ]
            )

    chart_items = ranked[:chart_limit]
    row_height = 28
    label_width = 230
    chart_width = 620
    right_padding = 70
    width = label_width + chart_width + right_padding
    height = 76 + max(1, len(chart_items)) * row_height
    max_frequency = max((corpus_counts[item] for item in chart_items), default=1)
    svg: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        (
            '<text x="20" y="32" font-family="Arial, PingFang SC, sans-serif" '
            'font-size="20" font-weight="700" fill="#17202a">'
            "Corpus 频率图（前 30 项）</text>"
        ),
        (
            '<text x="20" y="55" font-family="Arial, PingFang SC, sans-serif" '
            'font-size="12" fill="#5d6d7e">按出现次数降序排列</text>'
        ),
    ]
    for index, corpus in enumerate(chart_items):
        y = 72 + index * row_height
        frequency = corpus_counts[corpus]
        bar_width = max(2, int(chart_width * frequency / max_frequency))
        label = html_module.escape(corpus)
        svg.extend(
            [
                (
                    f'<text x="{label_width - 12}" y="{y + 17}" '
                    'text-anchor="end" font-family="Arial, PingFang SC, sans-serif" '
                    f'font-size="13" fill="#273746">{label}</text>'
                ),
                (
                    f'<rect x="{label_width}" y="{y + 3}" width="{bar_width}" '
                    'height="18" rx="2" fill="#2874a6"/>'
                ),
                (
                    f'<text x="{label_width + bar_width + 8}" y="{y + 17}" '
                    'font-family="Arial, PingFang SC, sans-serif" '
                    f'font-size="13" fill="#17202a">{frequency}</text>'
                ),
            ]
        )
    svg.append("</svg>")
    frequency_svg.write_text("\n".join(svg), encoding="utf-8")
    try:
        from excel_export import export_excel
        excel_path = export_excel(path)
        print(f"[表格] 已生成排版后的 Excel：{excel_path}")
    except (ImportError, OSError) as exc:
        print(f"[表格] CSV 已保留，但 Excel 导出失败：{exc}。请安装 requirements.txt 中的依赖，或关闭正在打开的同名 Excel 后重试。")


def search_url(keyword: str, page: int) -> str:
    return f"{BASE_URL}/search/mblog/?keyword={quote(keyword)}&page={page}&rl=0"


def browser_search_url(keyword: str, page: int = 1) -> str:
    return f"https://s.weibo.com/weibo?q={quote(keyword)}&page={page}"


def browser_realtime_search_url(keyword: str, page: int = 1) -> str:
    return (
        f"https://s.weibo.com/realtime?q={quote(keyword)}"
        f"&rd=realtime&tw=realtime&Refer=weibo_realtime&page={page}"
    )


def paginated_search_url(first_url: str, page: int) -> str:
    parts = urlsplit(first_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def describe_empty_search_page(tree: Any, url: str) -> str:
    title = clean_text(tree.xpath("string(//title)"))
    body_text = clean_text(" ".join(tree.xpath("//body//text()")))[:300]
    return (
        f"[提示] 本页没有解析到候选帖子：{url}\n"
        f"[提示] 页面标题：{title or '无'}\n"
        f"[提示] 页面片段：{body_text or '无'}"
    )


def is_placeholder_corpus(corpus: str) -> bool:
    return bool(re.search(r"x{2,}|X{2,}|Ｘ{2,}", corpus))


def sentence_context(text: str, start: int, end: int) -> str:
    left = max(
        text.rfind(mark, 0, start)
        for mark in ["。", "！", "？", "\n", "；", ";"]
    )
    right_candidates = [
        index for index in [text.find(mark, end) for mark in ["。", "！", "？", "\n", "；", ";"]]
        if index != -1
    ]
    right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 40)
    left = left + 1 if left != -1 else max(0, start - 40)
    return clean_text(text[left:right])


def collect_from_visible_text(
    tree: Any,
    page_url: str,
    pattern: re.Pattern[str],
    seen: set[tuple[str, str]],
    args: argparse.Namespace,
    remaining: int,
    next_number: int,
) -> list[CorpusRow]:
    rows: list[CorpusRow] = []
    texts = [clean_text(text) for text in tree.xpath("//body//text()")]
    for text in texts:
        if not text:
            continue
        for match in pattern.finditer(text):
            corpus = match.group(0)
            if is_placeholder_corpus(corpus):
                continue
            key = (page_url, corpus)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                CorpusRow(
                    number=next_number + len(rows),
                    username="",
                    corpus=corpus,
                    comment_text=sentence_context(text, match.start(), match.end()),
                    source=page_url,
                    note="当前页面文本直接提取",
                    weibo_id="",
                    comment_page_url=page_url,
                    post_text="",
                )
            )
            if len(rows) >= remaining:
                break
        if len(rows) >= remaining:
            break

    if rows:
        append_rows(args.output, rows)
        print(f"[采集] 当前页面直接提取 {len(rows)} 条句式语料，当前文件：{args.output}")
    return rows


def collect_from_post(
    session: Any,
    post: SearchPost,
    pattern: re.Pattern[str],
    seen: set[tuple[str, str]],
    args: argparse.Namespace,
    remaining: int,
    next_number: int,
) -> list[CorpusRow]:
    rows: list[CorpusRow] = []
    collected: list[CorpusRow] = []
    url: str | None = post.comment_url
    page_count = 0

    while url and page_count < args.max_comment_pages_per_post:
        page_count += 1
        tree = fetch_tree(session, url, args.timeout)
        for comment in parse_comments(tree):
            if not comment.username:
                continue
            for match in pattern.finditer(comment.text):
                key = (post.weibo_id, comment.text)
                if key in seen:
                    continue
                seen.add(key)
                source = comment.source or url
                rows.append(
                    CorpusRow(
                        number=next_number + len(collected) + len(rows),
                        username=comment.username,
                        corpus=match.group(0),
                        comment_text=sentence_context(comment.text, match.start(), match.end()),
                        source=source,
                        note="",
                        weibo_id=post.weibo_id,
                        comment_page_url=url,
                        post_text=post.post_text,
                    )
                )
                break
            if len(collected) + len(rows) >= remaining:
                break
        if len(collected) + len(rows) >= remaining:
            break

        if rows:
            append_rows(args.output, rows)
            collected.extend(rows)
            print(
                f"[采集] 微博 {post.weibo_id} 新增 {len(rows)} 条，"
                f"当前文件：{args.output}",
                flush=True,
            )
            rows = []

        url = parse_next_page(tree)
        wait(args)

    if rows:
        append_rows(args.output, rows)
        collected.extend(rows)
        print(
            f"[采集] 微博 {post.weibo_id} 新增 {len(rows)} 条，"
            f"当前文件：{args.output}",
            flush=True,
        )

    return collected


def collect_from_post_text(
    post: SearchPost,
    pattern: re.Pattern[str],
    seen_user_corpus: set[tuple[str, str]],
    remaining: int,
    next_number: int,
) -> list[CorpusRow]:
    if remaining <= 0 or not post.username or not post.post_text:
        return []

    rows: list[CorpusRow] = []
    for match in pattern.finditer(post.post_text):
        corpus = match.group(0)
        if is_placeholder_corpus(corpus):
            continue
        key = (post.username, corpus)
        if key in seen_user_corpus:
            continue
        seen_user_corpus.add(key)
        rows.append(
            CorpusRow(
                number=next_number,
                username=post.username,
                corpus=corpus,
                comment_text=post.post_text,
                source=post.source_url,
                note="实时搜索微博正文",
                weibo_id=post.weibo_id,
                comment_page_url=post.comment_url,
                post_text=post.post_text,
                published_at=post.published_at,
                published_time_raw=post.published_time_raw,
                full_text_status=post.full_text_status,
            )
        )
        break

    return rows


def collect(args: argparse.Namespace) -> None:
    unlimited = args.limit <= 0
    effective_limit = sys.maxsize if unlimited else args.limit
    append_rows(args.output, [])

    if not unlimited and load_row_count(args.output) >= effective_limit:
        write_frequency_outputs(args.output)
        print(f"[完成] 文件已有 {load_row_count(args.output)} 条，已达到目标 {args.limit} 条。")
        return

    first_tree = None
    first_url = None
    playwright_driver = None
    browser_context = None
    browser_page = None
    if args.interactive_browser:
        (
            session,
            first_tree,
            first_url,
            playwright_driver,
            browser_context,
            browser_page,
        ) = open_interactive_browser(args)
    else:
        cookie = read_cookie(args)
        session = build_session(cookie)

    try:
        pattern = re.compile(args.pattern)
        seen_user_corpus = load_seen_user_corpus(args.output)
        total = load_row_count(args.output)
        next_number = load_next_number(args.output)
        visited_posts: set[str] = set()

        print(f"[开始] 搜索关键词：{args.keyword}")
        print(f"[开始] 目标正则：{args.pattern}")
        target_label = "不限数量（按 Ctrl+C 停止）" if unlimited else f"{args.limit} 条"
        print(f"[开始] 已有 {total} 条，目标 {target_label}")

        page_number = 1
        while total < effective_limit:
            if page_number == 1 and first_tree is not None and first_url is not None:
                url = first_url
                tree = first_tree
                first_tree = None
            else:
                base_url = first_url or browser_realtime_search_url(args.keyword)
                url = (
                    paginated_search_url(base_url, page_number)
                    if args.interactive_browser
                    else search_url(args.keyword, page_number)
                )
                if browser_page is not None:
                    browser_page.goto(url, wait_until="domcontentloaded")
                    browser_page.wait_for_timeout(1500)
                    expand_search_posts(browser_page)
                    url = browser_page.url
                    tree = require_lxml_html().fromstring(browser_page.content())
                else:
                    tree = fetch_tree(session, url, args.timeout)

            posts = parse_search_posts(tree, url)
            print(f"[搜索] 实时第 {page_number} 页找到 {len(posts)} 个候选帖子")

            if not posts:
                print(describe_empty_search_page(tree, url))
                print("[翻页] 已到达当前结果末页，稍后返回第 1 页检查新内容。")
                page_number = 1
                wait(args)
                continue

            page_rows: list[CorpusRow] = []
            for post in posts:
                if total + len(page_rows) >= effective_limit:
                    break
                if post.weibo_id in visited_posts:
                    continue
                visited_posts.add(post.weibo_id)

                post_rows = collect_from_post_text(
                    post,
                    pattern,
                    seen_user_corpus,
                    remaining=effective_limit - total - len(page_rows),
                    next_number=next_number + len(page_rows),
                )
                page_rows.extend(post_rows)

            if page_rows:
                append_rows(args.output, page_rows)
                total += len(page_rows)
                next_number += len(page_rows)
                print(
                    f"[正文] 第 {page_number} 页新增 {len(page_rows)} 条，"
                    f"进度 {total}" + ("" if unlimited else f"/{args.limit}"),
                    flush=True,
                )
            else:
                progress = str(total) if unlimited else f"{total}/{args.limit}"
                print(f"[正文] 第 {page_number} 页没有新的匹配语料，进度 {progress}")

            if total >= effective_limit:
                break
            wait(args)
            page_number += 1
            if page_number > args.max_search_pages:
                print("[翻页] 已达到最大页数，返回实时第 1 页检查新内容。")
                page_number = 1

        print(f"[完成] 当前共 {total} 条。输出文件：{args.output}")
    finally:
        if browser_context is not None:
            try:
                browser_context.close()
            except Exception:
                pass
        if playwright_driver is not None:
            try:
                playwright_driver.stop()
            except Exception:
                pass
        write_frequency_outputs(args.output)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从微博实时搜索正文提取语料，按用户名与匹配文本去重并生成频率统计。"
    )
    parser.add_argument("--keyword", default="那很了", help="微博搜索关键词，例如：那很了")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help="提取语料的正则；修改搜索词后请同步检查此参数")
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="CSV 累计目标条数（默认：300）；0 表示不限条数，Ctrl+C 停止",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent / "results" / "corpus.csv"),
        help="CSV 输出路径，已有文件自动续采；新文件从 0 开始",
    )
    parser.add_argument("--cookie", help="微博 cookie 字符串")
    parser.add_argument("--cookie-file", help="保存微博 cookie 的文本文件")
    parser.add_argument("--cookie-env", default="WEIBO_COOKIE", help="读取 cookie 的环境变量名")
    parser.add_argument(
        "--interactive-browser",
        action="store_true",
        help="打开浏览器让你手动登录和搜索，然后从当前页面开始抓取",
    )
    parser.add_argument(
        "--browser-profile-dir",
        default=str(Path(__file__).resolve().parent / "weibo_browser_profile"),
        help="交互浏览器的登录资料目录",
    )
    parser.add_argument("--max-search-pages", type=int, default=50, help="最多翻多少页搜索结果")
    parser.add_argument(
        "--max-comment-pages-per-post",
        type=int,
        default=5,
        help="每条微博最多翻多少页评论",
    )
    parser.add_argument("--min-delay", type=float, default=3.0, help="请求间最小等待秒数")
    parser.add_argument("--max-delay", type=float, default=8.0, help="请求间最大等待秒数")
    parser.add_argument("--timeout", type=int, default=15, help="请求超时秒数")
    parser.add_argument("--login-timeout", type=int, default=300, help="等待登录及实时搜索就绪的最长秒数（默认：300）")
    parser.add_argument("--frequency-only", action="store_true", help="仅统计 --output 指定的已有 CSV，不联网或登录")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    if args.frequency_only:
        if not Path(args.output).is_file():
            raise SystemExit(f"找不到语料文件：{args.output}")
        write_frequency_outputs(args.output)
        print(f"[完成] 频率表与频率图：{frequency_output_paths(args.output)}")
        return
    if not (0 <= args.min_delay <= args.max_delay < float('inf')):
        raise SystemExit("等待时间必须满足 0 <= --min-delay <= --max-delay，且为有限值")
    if args.limit < 0 or args.max_search_pages < 1 or args.timeout <= 0 or args.login_timeout <= 0:
        raise SystemExit("--limit 必须 >= 0，页数和超时必须 > 0")
    if not args.keyword.strip():
        raise SystemExit("--keyword 不能为空")
    try:
        re.compile(args.pattern)
    except re.error as exc:
        raise SystemExit(f"--pattern 正则无效：{exc}") from exc
    try:
        collect(args)
    except KeyboardInterrupt:
        print(f"\n[停止] 已由用户停止采集，现有数据已保存在：{args.output}")
    except Exception as exc:
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            raise exc
        if isinstance(exc, PlaywrightError):
            raise SystemExit(
                f"[浏览器错误] {exc}\n"
                f"已保存的语料保留在：{args.output}。"
                "请保持浏览器开启；若刚才关闭了浏览器，重新运行即可续采。"
            ) from None
        raise


if __name__ == "__main__":
    main()
