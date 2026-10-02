# MarieSpace Radar 数据流水线

该项目负责 MarieSpace Radar 的真实高价值信息采集、清洗、精简整理和静态日报数据发布，覆盖科技与产业、政策与经济、社会趋势、未来机会和深度阅读。所有正式资讯保留可追溯来源，不虚构新闻、链接或来源。

## 已完成能力

- Phase 3：配置化 Source Adapter、正文清洗、时间过滤、条件请求和 SQLite 去重。
- Phase 4：DeepSeek 单批结构化整理和原始 usage 记录。
- Phase 5：按日期发布静态日报，供飞书 H5 展示和历史回看。
- Phase 5.5：从 AI 扩展到科技资讯，采用精简中文结构，并保持旧日报兼容。
- Phase 6：仅在正式日报 URL 与静态数据均可访问时，向飞书发送交互卡片通知；卡片展示 3 条核心双语资讯和完整日报入口。
- Phase 6.5：飞书内收藏与本地 Obsidian 收藏 Worker。普通访客保存在浏览器本地；Owner 经飞书身份校验后进入受保护队列，由 Windows Worker 主动拉取并写入 Obsidian。

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

`run_publish.py` 不调用 DeepSeek。它读取 `data/latest-enrichment.json`，写入相邻站点的 `data/daily/ai/YYYY-MM-DD.json` 并更新 `data/reports.json`。保留 `/daily/ai/` 路径用于历史链接兼容；新版数据使用 `schema_version: 3`、`category: radar` 和英文在前的双语字段。

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
- `source_type`、`source_role`、`channel`、`enabled`、`adapter`、`fetch_method`、`health_status`
- HTTPS `url`、`allow_hosts`、`priority`

支持 RSS、Atom、HTML Index 和官方 JSON Index。可选站点级 `cleaning`、`article_path_pattern` 和 `conditional_requests`。正式采集保存 ETag / Last-Modified，逐源健康结果写入 SQLite 和 `data/latest-run.json`。`discovery` / Tier 4 仅作线索，不直接进入正式整理。

当前启用来源覆盖中国大陆、港澳台地区、美国、欧洲、日本和韩国。正式内容继续排除 GitHub、OpenAI 域名和相关内容。

## MarieSpace Radar 结构

每条新版资讯在原有 `category` 之上保留 `channel`，并包含 `source_role`、双语标题、双语 `what_happened` / `why_it_matters`、`source`、`published_at`、`original_url`、`original_language` 和 `importance_score`。

模型、密钥和限额只从环境变量或已有 Phase 1 本地 `.env` 读取。模型名不在多个文件中硬编码。实际 input/output/total/cache hit/cache miss 及完整原始 usage JSON 写入 SQLite。

## Phase 6.5 收藏到 Obsidian

Worker 仅主动请求 `https://news.mariespace.cn/api/favorites` 的 `claim` 与 `complete` 接口，不会在 Windows 上监听端口。私有配置保存在已忽略版本控制的 `deploy/phase6.5-worker.env`；可从 `deploy/phase6.5-worker.env.example` 创建配置后验证：

```powershell
py -3 run_obsidian_worker.py --check-config
py -3 run_obsidian_worker.py --once
```

完成队列后端配置及一次手动 `--once` 验收后，安装登录时启动的 Worker：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install-phase6.5-worker-task.ps1
```

任务每次拉取后等待 45 秒再继续；Windows 关机期间队列保留在 EdgeOne Blob，重新登录后任务恢复。使用 `-Remove` 可移除该任务。Worker 只会在 Obsidian Vault 的 `07资源/待读清单` 中按安全的文章 ID 写入 Markdown，重复任务不会覆盖用户笔记。

## 阶段边界

### 正式发布 readiness（P0-2）

本地发布先写入 `data/daily/ai/YYYY-MM-DD.json` 和 `data/reports.json`，再提交、推送站点仓库。`git push` 成功只表示远端 Git 已接受提交，不表示 EdgeOne 已部署完成；当前不获取 EdgeOne deployment ID / status，审计明确保留 `null` / `unknown`。

正式 H5 与当天 JSON 必须全部验证成功，JSON 还须符合原有日期、非空内容检查，才允许进入现有飞书发送与幂等流程。readiness 使用 monotonic deadline，总等待窗口为 210 秒（不含 Git 执行时间），间隔依次为 5、10、15、30 秒，之后保持 30 秒；等待和单次请求都限制在剩余窗口内，没有固定轮数上限。已通过的 URL 不重复请求；404、429、5xx 和网络错误在窗口内记为 `not_ready_yet`，最终超时记为 `publish_verification_failed`，不发送飞书。

阻塞请求由独立 daemon 探针隔离，调用方等待不超过该次时间额度；超时后的迟到结果丢弃，不能触发发送。网络层未结束的探针可能短暂存活，但不写业务状态。

既有 delivery 日志和 run audit 保存 commit SHA、push 完成时间、readiness 开始时间/deadline，以及逐次 URL、timestamp、attempt、HTTP status、异常类型、耗时、push 后经过时间和安全的缓存响应头。最终记录 `h5_first_200_after_ms`、`json_first_200_after_ms`、`readiness_total_ms`、`readiness_result`；没有可证明的 push 时间时，传播耗时为 `null`，不推算历史数据。首次 200 是 HTTP 观测，JSON 内容验证未通过时仍不会放行。

### DeepSeek 网络路径（P0-1）

DeepSeek API 默认使用独立直连路径，不读取 Windows/Clash 系统代理；不影响资讯抓取、部署、飞书或 Worker 的网络路径。保留 urllib 和 HTTPS 证书/主机名验证，每个 attempt 创建新连接，最多 3 次请求，退避仍为 2 秒、5 秒。

可通过当前进程环境变量指定网络配置，不写入 Prompt 或来源配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_PROXY_MODE` | `direct` | `direct` 不使用代理；`system` 使用 urllib 自动代理；`explicit` 使用指定代理 |
| `DEEPSEEK_PROXY_URL` | 空 | 仅 `explicit` 使用，HTTP(S) 代理 URL；不要把含凭据的 URL 写入日志 |
| `DEEPSEEK_CONNECT_TIMEOUT_SECONDS` | `15` | TCP、代理 CONNECT、TLS 建连阶段超时 |
| `DEEPSEEK_READ_TIMEOUT_SECONDS` | `90` | TLS 建立后 socket 读写等待超时，不是完整响应总时长 |

既有 request attempt 日志新增 request_id、start_time、payload_bytes、article_count、clean_text_chars、proxy_mode、failure_phase、response_headers_received、response_body_started。DNS、TCP、代理 CONNECT、TLS、请求头/请求体、响应头/响应体均有明确阶段；旧运行缺失的阶段和字节数不能倒推。只有获得完整响应和实际 usage 后，现有调用方才记录 Token；网络中断时服务端是否已经处理请求仍可能无法判断。

本项目尚未实现 Phase 7 IELTS、Token Dashboard、多 Agent、MCP 或模型供应商切换。

