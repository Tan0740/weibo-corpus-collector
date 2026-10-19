# 微博构式语料采集与频率分析

一个面向语言研究和文本分析的开源 Python 命令行工具。从微博搜索结果的正文中提取符合规则的表达，按“用户名 + 匹配文本”去重，保存 CSV，并生成频率表与 SVG 柱状图。默认以“那很X了”为例，也可以调整为其他关键词和构式。

## 安装

需要 Python 3.10 或更新版本。在终端执行：

```bash
git clone https://github.com/Tan0740/weibo-corpus-collector.git
cd weibo-corpus-collector
python3 -m venv .venv
```

macOS / Linux 激活环境：

```bash
source .venv/bin/activate
```

Windows PowerShell 激活环境：

```powershell
.venv\Scripts\Activate.ps1
```

安装依赖及在线采集需要的浏览器：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 开始采集

```bash
python collector.py --interactive-browser --keyword "那很了" --limit 300 --output results/corpus.csv
```

1. 在弹出的 Chromium 浏览器中登录自己的微博账号。
2. 程序自动检测登录状态，并打开指定关键词的实时搜索页，无需回终端按 Enter。
3. 如遇登录或验证码，由使用者在浏览器中完成。登录后误跳个人主页或数据中心时，程序会自动返回搜索页；登录流程新开搜索标签页时也会自动识别。
4. 识别到候选帖子后按页采集。累计达到目标条数时结束，也可按 Ctrl+C 提前停止。采集期间请保持浏览器开启。

默认等待 300 秒让登录和搜索结果就绪，可用 `--login-timeout 600` 延长。等待超时会提示退出；关闭整个浏览器也会停止，已写入的记录保留。首次登录和平台验证码不能自动代替使用者完成；浏览器配置目录会保留本地登录状态，有效期内再次运行通常无需重复登录。

输出目录自动创建。正常结束或在采集循环中按 Ctrl+C 停止后，会根据已保存语料生成频率表和图。在线结果取决于登录状态、页面结构和平台可见内容，不能保证一定获得目标数量；当前结果不足时会循环检查新内容，可手动停止。

## 调整关键词与提取规则

`--keyword` 是提交给微博的搜索词，决定候选帖子范围。`--pattern` 是 Python 正则表达式，决定从候选正文中提取什么。仅改搜索词不会自动更改匹配规则。

默认搜索词为 `那很了`，默认正则：

```text
那很[^，。！？、\s]{1,12}?了
```

它匹配“那很”和“了”之间包含 1 到 12 个非指定标点、非空白字符的表达，如“那很好吃了”。`1,12` 控制中间部分长度，`?` 表示尽量在较近的“了”结束。它筛选候选文本，不自动判定词性或语法合格性。

例如，改为收集“太X了”的表达：

```bash
python collector.py --interactive-browser --keyword "太 了" --pattern "太[^，。！？、\s]{1,12}?了" --limit 100 --output results/tai_x_le.csv
```

搜索结果先由微博决定，正则不会搜遍全站。搜索词过窄可能遗漏表达，过宽可能引入无关帖子，请根据结果调整并人工复核。正则使用引号包裹；研究不同构式时建议使用不同输出文件。

## 调整语料数量

`--limit` 表示输出 CSV 的累计记录目标，单位是“条记录”，不是汉字数、词数或不同关键词数。

| 设置 | 含义 |
| --- | --- |
| `--limit 100` | 累计到 100 条后停止 |
| `--limit 300` | 累计到 300 条后停止（默认） |
| `--limit 1000` | 累计到 1000 条后停止 |
| `--limit 0` | 不设数量上限，按 Ctrl+C 停止 |

从零开始采集 500 条，用一个不存在的新文件名：

```bash
python collector.py --interactive-browser --keyword "那很了" --limit 500 --output results/new_corpus_500.csv
```

同一文件已有 200 条时，`--limit 300` 只会补至 300 条；已有条数达到或超过目标时不会再登录采集，也不会截断旧文件。更换文件名从 0 开始。重复运行同一个文件会续采，不能当作清空操作。

## 去重和统计口径

同一用户名的同一匹配文本只保留一条，不同用户名的相同表达允许保留。单条微博当前最多收录一个符合规则的新匹配；解析不到用户名的微博不收录。因此统计的是此采集与去重口径下的样本频次，不代表全平台总体频率。用户名也不等同于稳定的用户 ID。

| 文件 | 内容 |
| --- | --- |
| `results/corpus.csv` | 编号、用户名、匹配表达、上下文和来源 |
| `results/corpus_frequency.csv` | `rank` 排名、`corpus` 表达、`frequency` 频次、`unique_users` 独立用户名数 |
| `results/corpus_frequency.svg` | 频次前 30 项柱状图，可在浏览器打开 |

对已有 CSV 重新统计，不联网：

```bash
python collector.py --frequency-only --output results/corpus.csv
```

原始 CSV 字段：`number`（编号）、`username`（用户名）、`corpus`（匹配表达）、`comment_text`（上下文）、`source`（搜索页）、`note`（备注）、`weibo_id`（微博标识）、`comment_page_url`（移动版链接）、`post_text`（正文）。部分字段命名来自早期版本；当前采集的是微博正文，不是评论区。

## 离线示例

可以先运行虚构数据示例，检查提取、去重和统计输出，不需要微博登录、浏览器安装或第三方依赖：

```bash
python demo.py
```

5 条输入去重后保留 4 条记录：“那很好吃了”2 次，“那很幸运了”和“那很期待了”各 1 次。文件写入 `results/demo.csv`、`results/demo_frequency.csv` 和 `results/demo_frequency.svg`。重复运行只会重建这三个示例文件。

## 其他参数

运行 `python collector.py --help` 查看全部参数。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--keyword` | 那很了 | 微博搜索关键词 |
| `--pattern` | 上述默认正则 | 从正文提取语料 |
| `--limit` | 300 | CSV 累计目标条数，0 表示不限 |
| `--output` | 脚本目录下的 results/corpus.csv | CSV 路径，已有文件续采 |
| `--min-delay` / `--max-delay` | 3 / 8 秒 | 两页之间的随机等待范围 |
| `--max-search-pages` | 50 | 最多翻至该页，再返回第一页检查新内容 |
| `--timeout` | 15 秒 | Cookie 请求模式超时；不控制浏览器超时 |
| `--login-timeout` | 300 秒 | 等待登录完成与实时结果就绪的最长时间 |
| `--browser-profile-dir` | 脚本目录下的 weibo_browser_profile | 本地浏览器登录资料目录 |
| `--frequency-only` | 关闭 | 只对已有 CSV 生成统计 |

交互浏览器模式为推荐流程。不加 `--interactive-browser` 时使用 Cookie 请求模式，需通过 `WEIBO_COOKIE` 环境变量或 `--cookie-file` 提供自己的 Cookie；该模式使用移动版搜索，可用性与交互实时搜索不同。`--max-comment-pages-per-post` 是兼容旧版本的参数，当前正文采集不使用它。

## 运行检查

```bash
python -m unittest discover -s tests -v
```

测试使用虚构数据和模拟页面请求，不需要微博账号。离线测试验证提取、去重、累计数量限制和频率输出；在线采集仍需要使用者在当前平台环境下完成登录并验证。

## 许可与贡献

代码采用 [MIT License](LICENSE)，允许使用、修改和分发，需保留许可声明。软件许可不授予对第三方平台内容的额外权利。请遵守平台规则及研究伦理，仅采集有权限访问的内容。

欢迎通过 Issues 报告问题，或通过 Pull Requests 改进解析和统计功能。报告解析问题时请移除 Cookie、登录信息及不必要的个人数据。`.gitignore` 默认排除浏览器资料、真实语料和生成结果。

项目起源于围绕 [dataabc/weiboSpider](https://github.com/dataabc/weiboSpider) 的语料研究实践。本仓库发布独立采集脚本及配套工具，不包含该第三方仓库；第三方代码及许可归其原作者所有。
