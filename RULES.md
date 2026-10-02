# MarieSpace Radar daily supply rules

- The normal-sized edition starts at 10 verified, publishable stories; the target is 14 and the maximum is 18. A smaller edition may publish when at least one qualified story remains. Zero stories is a true failure and produces no report or Feishu notification.
- `daily_mode` is `normal` for 10 or more stories, `graceful_degraded` for 5–9, `minimal_daily` for 1–4, and `true_failure` for zero. Only stories passing the existing fact, bilingual, importance, deduplication and final selection checks count.
- Keep `MAX_DAILY_TOKENS=40000` as the daily budget. If the next batch cannot safely fit, stop enrichment and publish any qualified stories already available; do not pursue the target at the expense of an existing valid edition. Never exceed the budget.
- Preserve the existing 24-hour, 72-hour and 3–7-day supply priority, source verification and historical deduplication. Do not invent or lower the quality threshold to increase the count. No model call is needed to decide whether to continue.
