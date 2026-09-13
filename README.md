# AI Daily 新闻采集与双语整理

该独立项目完成了 Phase 3 的真实新闻采集与标准化，并在 Phase 4 增加一次批量 DeepSeek 双语整理。它只处理可追溯的官方来源，不会虚构新闻、链接或来源。

## 当前边界

- 从官方 RSS、Atom、Newsroom 和 GitHub Release 获取候选，抽取正文并按发布时间、URL、正文指纹和本地历史去重。
- 对已验证且尚未整理的少量候选，发出一次 DeepSeek 批量请求，得到严格校验的结构化双语内容。
- 每个条目保存：中文标题、原文标题、来源、时间、原文链接、原文要点、中文翻译、中英文摘要、相关性与实用表达。
- 来源、发布时间和原文 URL 始终来自本地已验证文章；模型返回的这些元数据不会被信任或覆盖。
- 保存 DeepSeek 返回的实际 `prompt_tokens`、`completion_tokens`、`total_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens` 和可用的 reasoning tokens；不估算用量。
- 不修改 GitHub Pages 或飞书网页应用，不发送飞书消息，也不开始 Phase 5 的展示与历史归档。

## 运行与验收

在项目根目录运行：

```powershell
py -3 -m unittest discover -s tests -p 'test_*.py' -v
py -3 run_collect.py
py -3 run_enrichment.py --dry-run
py -3 run_enrichment.py
```

`run_enrichment.py` 会复用相邻 `feishu-deepseek-assistant/.env` 中已有的 `DEEPSEEK_API_KEY`，或优先使用当前环境变量；密钥不会复制到本仓库，也不会输出。第一次真实运行只处理尚未整理的文章，避免重复调用。

运行结果保存在被 Git 忽略的 `data/ai_daily.sqlite3` 与 `data/latest-enrichment.json`。`latest-enrichment.json` 可直接检查条目结构和 API 返回的实际用量。

## 输入与成本边界

这些环境变量都可选：

- `MAX_BATCH_ARTICLES`：单批文章上限，默认 `8`。
- `MAX_NEWS_INPUT_CHARS_PER_ARTICLE`：每篇提交给模型的清洗正文上限，默认 `4000`。
- `MAX_NEWS_OUTPUT_TOKENS`：单次模型最大输出，默认 `3500`。
- `MAX_DAILY_TOKENS`：当日已记录实际总 token 达到此值后停止新的调用，默认 `40000`。

当可验证候选少于目标数量时，程序会处理实际可用的条目，不会补造内容。
