# Phase 4 验收报告：DeepSeek 双语 AI Daily（2026-09-14）

## 本阶段完成

- 只从近 72 小时、已核验的 AI 文章中选择最多 8 条候选；旧文、未核验文章及禁止内容不会进入模型输入。数量不足时如实保留，不补旧文。
- 修正双语方向：英文原文对应中文翻译，中文原文对应英文翻译。原文要点、逐点翻译、中文摘要、英文摘要和相关性分别保存；英文实用表达必须出现在已采集原文中。
- 模型返回的来源、时间和原文链接不被信任，统一由已验证的文章记录回填；生成内容若出现禁止内容会整体拒绝保存。输出还带有 `original_language` 和 `translation_language`，供后续阅读界面识别翻译方向。
- 当前无合格候选时直接返回 `0`，不调用 API，也不覆盖旧的日报数据。`--dry-run` 的预览文件与正式结果文件分离。
- 请求型号统一通过 `DEEPSEEK_MODEL=deepseek-flash` 配置，启用官方 JSON 输出模式和非思考模式；发送前按字符数做保守 Token 预算检查，返回后记录 API 实际 token/cache 用量。9 月 10 日的[官方公告](https://www.deepseek.com/en/news/deepseek-v4-1-flash/)说明旧 `deepseek-v4-flash` 名称仅临时兼容；JSON 参数见[官方接口说明](https://api-docs.deepseek.com/guides/json_mode/)。

## 验证结果

- 自动化测试：26 项通过，覆盖有效期、无候选不调用、预览不覆盖正式结果、翻译方向、禁止内容、Token 预算和请求参数。
- 今日正式库：文章 0 条；执行 `py -3 run_enrichment.py` 得到 `candidates=0 saved=0`，没有触发 DeepSeek，也没有生成今日日报。
- 隔离历史回放：以 2026-09-10 的一篇 [DeepSeek 官方文章](https://www.deepseek.com/en/news/deepseek-v4-1-flash/) 和当天时间窗口验证真实 API。最终得到 1 条结构化结果、4 组原文要点与中文翻译、2 条原文表达。成功调用用了 2,298 Token；连同一次被校验拒绝的调用和一次诊断调用，回放测试实际共使用 **6,960 Token**，均记录在隔离回放库。回放结果单独保存在 `data/replay-2026-09-10/data/latest-enrichment.json`，标注 `replay=true`，没有进入今日库或发布链路。
- 回放首次发现英文页面夹有少量中文导航，旧语言判断将文章错分为中文并拒绝模型结果；已改为按正文主要语言判断，复测通过。拒绝的调用用量保存在隔离回放库，未保存错误内容。

## 2026-09-15 用量与来源字段复核

- 历史回放的 `latest-enrichment.json` 唯一结果保留 `original_url`、`source`、`published_at`、`original_language=en`，同时标记 `translation_language=zh`。这些字段来自已验证的文章记录；新结果按同一结构写出。
- **6,960 Token 是整个隔离历史回放的三次调用总量，不是一份日报的用量**：校验拒绝 2,404（输入 1,242、输出 1,162、cache hit 0、miss 1,242）；成功日报 2,298（输入 1,242、输出 1,056、hit 256、miss 986）；单次诊断 2,258（当时只记录了总量）。成功调用的原始 `usage` 可由回放 JSON 精确找回，已回填 SQLite `model_usage.raw_usage_json`；另外两次没有留存完整原始 JSON，不能补造。拒绝调用已有标量分项，诊断调用没有。
- 新调用将 API 返回的完整 `usage` JSON 与 input/output/cache 标量一起保存。即使模型响应被校验拒绝、正文为空或截断，只要响应包含 `usage`，也记为 rejected 调用；请求未得到 API 响应时无可记录的原始用量。Phase 8 Dashboard 仍未实现。
- 飞书助手和 AI Daily 流水线在本机共用 Phase 1 `.env` 的 `DEEPSEEK_MODEL`，环境变量可覆盖；两处运行代码均不再硬编码模型名。独立部署环境应在部署前配置同名变量。本次未更新远程服务。
- 流水线 31 项、飞书助手 11 项自动化测试通过；本次没有新增真实 API 请求或发布操作。

## 验收边界

这次验证证明了有真实合格候选时的 DeepSeek 调用与结构化保存；**今日没有可生成的日报**。后续有近 72 小时的合格新文时，还需再核对一次当日批次的事实准确性。Phase 5 页面发布、归档与飞书入口均未执行；域名相关配置继续暂停。
