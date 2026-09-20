# Phase 5 数据发布器验收说明（2026-09-15）

发布器继续读取 Phase 4 的 `data/latest-enrichment.json`，按 Asia/Shanghai 日期写出 H5 可读的 `data/daily/ai/YYYY-MM-DD.json` 和 `data/reports.json`。每条资讯保留原始来源、`original_url`、发布时间、原文语言和已验证的双语结构。新日期不会覆盖旧日期。

发布前校验非空日报、来源字段、HTTPS 原文链接、发布时间和语言方向；拒绝缺失字段、受限制内容及标记为 `replay=true` 的隔离历史回放。无合格资讯时不会创建空日报。当前正式输出没有新的合格日报，所以正式 H5 索引仍为空。

流水线 33 项测试通过。本阶段未调用 DeepSeek、未发布机器人消息，也未执行正式域名或飞书入口配置。H5 与设备尺寸的验证见相邻站点项目的 `PHASE-5-REPORT.md`。
