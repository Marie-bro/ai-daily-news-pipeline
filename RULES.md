# MarieSpace Radar daily supply rules

- The minimum is 10 verified, publishable stories; the normal target is 14 and the maximum is 18. **14 is a target, not a success condition; 10 is the minimum success condition.**
- A story counts toward the minimum only after the existing fact, bilingual, deduplication and final selection checks. Model-accepted items alone do not count.
- Keep `MAX_DAILY_TOKENS=40000` as the daily budget. If the next batch cannot safely fit, publish the current 10 to 13 qualified stories and record `graceful_stop`. If fewer than 10 qualified stories remain, record `daily_failed` with `minimum_not_met_reason=token_budget_exhausted` and do not publish or notify Feishu.
- Do not invent or lower the quality threshold to satisfy the minimum. No model call is needed to decide whether to continue.
