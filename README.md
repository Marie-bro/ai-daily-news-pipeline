# Tech Daily 数据流水线

该项目负责 MarieSpace Tech Daily 的真实科技资讯采集、清洗、精简整理和静态日报数据发布。所有正式资讯保留可追溯来源，不虚构新闻、链接或来源。

## 已完成能力

- Phase 3：配置化 Source Adapter、正文清洗、时间过滤、条件请求和 SQLite 去重。
- Phase 4：DeepSeek 单批结构化整理和原始 usage 记录。
- Phase 5：按日期发布静态日报，供飞书 H5 展示和历史回看。
- Phase 5.5：从 AI 扩展到科技资讯，采用精简中文结构，并保持旧日报兼容。
- Phase 6：仅在正式日报 URL 与静态数据均可访问时，向飞书发送交互卡片通知。

## 使用

```powershell
py -3 -m unittest discover -s tests -p 'test_*.py' -v
py -3 run_collect.py --validate-sources
py -3 run_collect.py --check-sources
py -3 run_collect.py --dry-run
py -3 run_collect.py
py -3 run_enrichment.py --dry-run
py -3 run_enrichment.py
py -3 run_publish.py
py -3 run_daily_delivery.py --report-date 2026-09-20 --dry-run
py -3 run_daily_delivery.py --report-date 2026-09-20 --send-dry-run
py -3 run_daily_delivery.py --report-date 2026-09-20 --verify-url
py -3 run_daily_delivery.py --scheduled
```

`run_publish.py` 不调用 DeepSeek。它读取 `data/latest-enrichment.json`，写入相邻站点的 `data/daily/ai/YYYY-MM-DD.json` 并更新 `data/reports.json`。保留 `/daily/ai/` 路径用于历史链接兼容；新版数据使用 `schema_version: 3`、`category: tech` 和英文在前的双语字段。

## Phase 6 推送

`run_daily_delivery.py --scheduled` 复用采集、整理和发布流程；只有存在合格日报、站点数据已提交并推送、`https://news.mariespace.cn/daily/ai/?date=YYYY-MM-DD` 与对应 JSON 均可访问时，才发送飞书交互卡片。卡片直接读取已发布的双语标题、数量和阅读时间，不增加 DeepSeek 调用。

发送状态保存在现有 SQLite 的 `daily_delivery_sends` 表：同一 `report_date + report_id + target` 只会正常发送一次；任何不确定发送结果也会阻止自动重发。管理员仅可通过显式 `--report-date ... --force` 人工重发。运行记录写入忽略版本控制的 `data/delivery-runs.jsonl`，不含凭据。

Windows 调度复用系统 Task Scheduler。完成一次手动验收后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install-phase6-task.ps1
```

该任务名为 `MarieSpace Tech Daily Daily`，每天 08:00（Asia/Shanghai）运行，且已有任务运行时不会启动第二份。

## 来源配置

`config/sources.json` 继续使用 JSON + Source Adapter。每项必须定义：

- `id`、`name`、`region`、`category`、`tier`、`language`
- `source_type`、`enabled`、`adapter`、`fetch_method`、`health_status`
- HTTPS `url`、`allow_hosts`、`priority`

支持 RSS、Atom 和 HTML Index。可选站点级 `cleaning`、`article_path_pattern` 和 `conditional_requests`。正式采集保存 ETag / Last-Modified，逐源健康结果写入 `data/latest-run.json`。Tier 4 仅作线索，不直接进入正式整理。

当前启用来源覆盖中国大陆、港澳台地区、美国、欧洲、日本和韩国。正式内容继续排除 GitHub、OpenAI 域名和相关内容。

## Tech Daily 结构

每条新版资讯包含：`category`、`title_cn`、`title_original`、`source`、`published_at`、`original_url`、`original_language`、`what_happened`、`why_it_matters` 和 `importance_score`。

模型、密钥和限额只从环境变量或已有 Phase 1 本地 `.env` 读取。模型名不在多个文件中硬编码。实际 input/output/total/cache hit/cache miss 及完整原始 usage JSON 写入 SQLite。

## 阶段边界

本项目尚未实现 Phase 6 机器人推送、IELTS、Token Dashboard、多 Agent、MCP 或模型供应商切换。

