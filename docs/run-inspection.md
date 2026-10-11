# 下一次正式运行的筛选验收视图

现有本地 Sources 页面 `/sources?date=YYYY-MM-DD&run_id=...` 和每次运行的
`data/run-audits/<run_id>.html` 提供三种视图：链路总览、实际淘汰原因统计、逐条 Article Trace。
仍使用原有 `/api/sources`，没有新增公开路由。Sources 只绑定 loopback；不进入公开日报站点。

## 数量口径

- 采集链路：Raw Index → SourceItem → Current Run Article → Newly Inserted Article。
  原始解析对象的 Keep/Drop 计数只有与实际返回 SourceItem 数量精确核对后才显示淘汰总数。
  SourceItem 对象数和 distinct article ID 集合分别保存，不将同 URL 的多个对象当成多个唯一 Article。
- 库存链路：Current Run Inventory + Historical Inventory → Eligible Inventory → Candidate Pool → 初批 Selected Candidate。
  只有保存的集合证明前后为子集关系才计算集合差；不把历史库存伪装成采集链路产物。
- 模型阶段包含缓存旁路、补位和 Repair 分支，并非严格递减漏斗。
  Validation 展示每篇最后一次结果；同时保留此前失败和修复事件。
  最终选择会读取已有缓存库存，因此输入不等于本轮模型通过数量。
- Published 表示实际写入日报 artifact 的 article IDs 且正式 readiness 成功。
  写入本地 artifact、部署推送、readiness、飞书发送是不同事实，不能互相替代。
- 淘汰 reason 是现有代码决策事件的统计，重复观察同一文章会重复计数；不同阶段不可累加成唯一淘汰文章总数。

## 单篇与批次

Selected Candidate 包含初批和实际请求的补位文章。每篇可查看库存来源、缓存、是否提交模型、批次、
校验与修复、importance、最终原因和对应完整 trace。候选池另有完整去向列表。
没有保存足够证据的最终原因标记 `not_observable`，不会补造“未入选”的规则。

模型批次记录 article IDs、payload bytes、实际 usage、finish_reason、retry history、校验与修复数量。
收到完整响应后记录 finish_reason，即使响应因截断被拒绝也保留观测数据。
Token 仍由现有 SQLite usage 机制记账；观测记录不会重复记账。
主调用的 Token 属于批次，不存在真实单篇拆分，因此单篇 Token 明确标记 `not_observable`；
单篇 Repair 请求则可直接查看该请求的实际 usage。

## 历史与边界

旧 audit 只投影已保存证据，不回写历史 JSON，不重新抓取或调用模型。
旧记录未保存的字段继续显示 `not_observable`。
本修改不改变来源、筛选、排序、Prompt、Token Budget、retry、发布或飞书条件。
下一次正式任务自然执行后才完成生产验收；不能以本地回归或历史页面预览代替真实运行结果。
