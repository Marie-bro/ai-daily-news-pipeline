# AI Daily 新闻采集与标准化

Phase 3 的独立数据工程项目。它只从可追溯的官方来源采集 AI 资讯，完成正文抽取、时间筛选、URL/指纹/历史去重，并保存结构化数据。

本项目不会调用 DeepSeek，不会生成日报，不会向飞书推送，也不会修改现有 AI Daily 网站。双语整理属于 Phase 4。

## 数据流程

```text
官方 RSS / Atom / Newsroom
  -> 抓取候选链接
  -> 正文抽取与清洗
  -> 发布时限筛选（24 小时；不足时最多 72 小时）
  -> URL、正文指纹与 SQLite 历史去重
  -> 8–15 条结构化 AI 候选资讯
```

## 使用

使用本机 Python 启动器：

```powershell
py -3 -m unittest discover -s tests -p 'test_*.py' -v
py -3 run_collect.py --dry-run
py -3 run_collect.py
```

`--dry-run` 不写入数据库，运行结果会写到 `data/latest-run.json`。正式运行写入 `data/ai_daily.sqlite3`；这些运行数据不提交到 Git。

## 来源原则

来源在 `config/sources.json` 中集中声明，当前只启用 OpenAI、Anthropic、Google AI 的官方页面或官方 Feed，以及 OpenAI Codex、OpenAI Python、Anthropic Python 的官方 GitHub Release Feed。每条保留原始 URL、来源、来源类型、发布时间、原文和清洗正文。无法确认发布时间、超出时间窗或正文为空的候选会被丢弃，不会由模型补写。

## 数据结构

`articles` 表至少保存：`id`、`category`、`title`、`original_title`、`source`、`source_type`、`published_at`、`original_url`、`language`、`raw_text`、`clean_text`、`fingerprint`、`created_at`、`verification_status`。

## 与其他阶段的边界

- Phase 2 / 2.5 的 GitHub Pages 页面和飞书网页应用保持不变。
- Phase 4 才对已经标准化的少量候选做一次批量 DeepSeek 双语整理。
- Phase 5 才将日报和归档展示接入飞书资讯中心。
