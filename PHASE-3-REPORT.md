# Phase 3 增量验收报告（2026-09-14）

## 本次完成

- 来源采集改为可注册的独立 Source Adapter：RSS、Atom 和 HTML 索引各有解析器，来源由现有 `config/sources.json` 选择适配器。没有引入 RSSHub 等项目的代码或依赖。
- 来源配置新增严格校验：必填字段、唯一 ID、适配器名称、HTTPS 和允许的主机、优先级、布尔开关、路径正则与正文清洗选择器。`enabled: false` 的来源不会发起请求。
- 增加 `--validate-sources` 离线校验和 `--check-sources` 逐源健康检查。健康检查抓取索引并验证一篇样本正文，分别报告 `ok`、`empty`、`disabled`、`degraded` 或 `error`；单源失败不阻断其他来源。
- 对启用的来源索引优先发送 `If-None-Match` / `If-Modified-Since`。ETag、Last-Modified 和响应正文保存在原 SQLite 数据库的 `source_fetch_cache` 表；304 时复用缓存正文，以免误报为“无内容”。`--dry-run` 和健康检查不写此缓存。
- 增加站点级 `cleaning.include` 和 `cleaning.exclude` 规则，支持简单标签、类名和 ID 选择器；规则未命中会报错，不会静默退回到可能混入导航的全文。现有来源暂未指定未经验证的站点选择器。
- Google AI 来源改用其公开 AI RSS，避免原 HTML 索引把导航入口当成新闻。来源请求和文章请求均限制为配置允许的 HTTPS 主机，跳转也会检查。
- `latest-run.json` 预留逐源状态字段 `source_health`；原有文章字段和 SQLite 去重机制保持不变。Phase 5/8 功能没有提前实现。

## 验证

```powershell
py -3 run_collect.py --validate-sources
py -3 run_collect.py --check-sources
py -3 run_collect.py --dry-run
py -3 -m unittest discover -s tests -p 'test_*.py' -v
```

当前 3 个启用来源的索引与样本正文检查均为 `ok`。端到端空跑读取 38 个来源候选，近 72 小时内接受 0 条，错误 0 条；没有为达到数量目标而收录旧文或虚构内容。空跑不写文章库，也不调用 DeepSeek。

正式采集路径连续运行两次，均为 38 个索引候选、0 条合格新文、0 错误；文章库仍为 0 条。第二次运行中 DeepSeek 来源实际返回 304，`source_health` 显示 `not_modified=true`、`used_conditional_request=true`，缓存正文被复用。Anthropic 和 Google 当前未提供 ETag / Last-Modified，故使用普通请求。

## 边界

本阶段没有配置域名、DNS、HTTPS、EdgeOne 或飞书正式入口；没有发布日报、Token Dashboard 或定时重试。正式域名实名审核完成前，相关发布步骤继续暂停。
