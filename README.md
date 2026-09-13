# AI Daily 数据流水线

该项目负责 AI Daily 的真实资讯采集、清洗、双语整理和静态日报数据发布。它只使用可追溯来源，并且不会虚构新闻、链接或来源。

## 已完成能力

- Phase 3：采集允许公开域名的官方 RSS、Atom、Newsroom，完成正文清洗、时间过滤和 URL/指纹/历史去重。
- Phase 4：将少量已验证候选一次批量交给 DeepSeek，得到经严格校验的双语结构化内容，并记录 API 实际 token/cache 用量。
- Phase 5：将最新已验证日报发布成现有飞书 H5 项目可读取的静态 JSON；按日期保存详情并更新历史索引。

## 使用

```powershell
py -3 -m unittest discover -s tests -p 'test_*.py' -v
py -3 run_collect.py
py -3 run_enrichment.py
py -3 run_publish.py
```

`run_publish.py` 不调用 DeepSeek。它读取 `data/latest-enrichment.json`，写入相邻 `ai-daily-public-site/data/daily/ai/YYYY-MM-DD.json`，并更新 `ai-daily-public-site/data/reports.json`。历史日期不会被新日报覆盖。

`run_enrichment.py` 优先使用环境变量中的 `DEEPSEEK_API_KEY`，否则复用相邻 Phase 1 项目的本地 `.env`；密钥不会复制到本仓库或输出。

## 阶段边界

此项目不发送飞书机器人通知、不安排定时任务、不实现 IELTS 推送，也不构建 Token Dashboard。飞书页面只读取已发布的静态日报数据。
