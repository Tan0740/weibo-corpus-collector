"""Run a reproducible offline demonstration using fictional corpus records."""

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from collector import (
    DEFAULT_PATTERN,
    CorpusRow,
    append_rows,
    clean_text,
    load_row_count,
    write_frequency_outputs,
)


def main():
    examples = [
        ("demo_user_a", "今天吃到了喜欢的甜点，那很好吃了。"),
        ("demo_user_b", "这份点心，那很好吃了！"),
        ("demo_user_a", "再说一遍，那很好吃了。"),
        ("demo_user_c", "终于赶上末班车，那很幸运了。"),
        ("demo_user_d", "看到新安排，那很期待了。"),
    ]
    pattern = re.compile(DEFAULT_PATTERN)
    seen = set()
    rows = []
    for username, text in examples:
        for match in pattern.finditer(clean_text(text)):
            corpus = match.group(0)
            key = (username, corpus)
            if key in seen:
                continue
            seen.add(key)
            rows.append(CorpusRow(
                number=len(rows) + 1,
                username=username,
                corpus=corpus,
                comment_text=text,
                source="offline-demo",
                note="虚构演示数据，不来自真实平台",
                weibo_id="",
                comment_page_url="",
                post_text=text,
            ))

    results = Path(__file__).resolve().parent / "results"
    results.mkdir(exist_ok=True)
    # Build from scratch so rerunning the demo does not append duplicates.
    with TemporaryDirectory(dir=results) as temporary:
        output = Path(temporary) / "demo.csv"
        append_rows(str(output), rows)
        write_frequency_outputs(str(output))
        count = load_row_count(str(output))
        for artifact in Path(temporary).iterdir():
            artifact.replace(results / artifact.name)
    print(f"演示完成：5 条输入，去重后 {count} 条语料。")
    print(f"语料表：{results / 'demo.csv'}")
    if (results / "demo.xlsx").is_file():
        print(f"排版后的 Excel：{results / 'demo.xlsx'}")
    print(f"频率表：{results / 'demo_frequency.csv'}")
    print(f"频率图：{results / 'demo_frequency.svg'}")


if __name__ == "__main__":
    main()
