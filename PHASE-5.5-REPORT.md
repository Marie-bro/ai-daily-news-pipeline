# Phase 5.5 验收报告

## 范围

本阶段将 AI Daily 最小增量升级为 Tech Daily，不改变 Phase 3/4/5 的采集、SQLite 去重、DeepSeek 单批整理和静态日报发布架构，也未进入 Phase 6。

## 已完成

- 新增 10 个科技分类：`ai`、`chips`、`consumer_tech`、`software`、`robotics`、`mobility`、`space`、`science`、`internet`、`other_tech`。
- 精简 DeepSeek 输出为：`category`、`title_cn`、`title_original`、`source`、`published_at`、`original_url`、`original_language`、`what_happened`、`why_it_matters`、`importance_score`。
- 停止生成翻译数组、英文摘要、学习表达、relevance 和大段 key points。
- 新结构使用 `schema_version: 2`；旧版 `schema_version: 1` 日报继续保留并由 H5 兼容渲染。
- SQLite 新增独立 `tech_enrichments` 表，并为文章补充 `source_region`、`source_tier`，不破坏旧 enrichment 数据。
- 无合格内容时不覆盖已有正式输出，也不生成空日报；页面文案为“今日暂无符合条件的科技资讯”。
- Tier 1/2 优先，Tier 3 可补充，Tier 4 不进入直接整理候选。
- 正式内容继续排除 GitHub 和 OpenAI 域名及相关内容，遵守已确认的来源限制。

## 来源覆盖与健康检查

启用 9 个配置化来源，覆盖 CN、HK、US、EU、JP、KR：

- DeepSeek Research & News（CN，Tier 1）
- Anthropic Newsroom、Google AI、NVIDIA Blog、Cloudflare Blog（US，Tier 1）
- Hugging Face Blog（EU，Tier 1）
- Toyota Global Newsroom（JP，Tier 1）
- Samsung Global Newsroom（KR，Tier 1）
- HKUST News（HK，Tier 1）

每个来源均配置 `region`、`category`、`tier`、`language`、`source_type`、`enabled`、`fetch_method`、`health_status`，并继续使用独立 Source Adapter、ETag/Last-Modified、允许域名和站点清洗规则。逐源检查结果为 9/9 正常。

采集 Dry Run：索引候选 253 条，接受 10 条，写入 0 条，72 小时窗口，错误 0。

## 历史回放与 Token 对比

使用 2026-09-19 Phase 4 已处理的同一组 4 条已验证文章。历史 Dry Run 先执行，模型调用 0 次；随后仅进行一次隔离的比较调用。回放结果标记 `replay: true`，发布器拒绝正式发布。

| 指标 | 旧版 | 新版 | 变化 |
| --- | ---: | ---: | ---: |
| input tokens | 3,873 | 3,908 | +0.9% |
| output tokens | 2,688 | 821 | -69.5% |
| total tokens | 6,561 | 4,729 | -27.9% |
| cache hit | 0 | 0 | 0 |
| cache miss | 3,873 | 3,908 | +0.9% |
| request count | 1 | 1 | 不变 |

新版有 2 条达到 `importance_score >= 60`，体现“广泛采集、严格筛选、少量高质量输出”。

## 测试

- 数据流水线：36 项通过。
- H5：7 项通过。
- Python 编译检查通过。
- 来源配置校验通过。
- 逐源网络健康检查：9/9 通过。
- 历史回放隔离、空日报保护、旧 schema 兼容、新 schema 发布、原文 URL 校验、usage 原始记录均有测试覆盖。

## 阶段边界

未实现机器人推送、IELTS、Token Dashboard、多 Agent、MCP 或模型供应商切换。Phase 6 尚未开始。


