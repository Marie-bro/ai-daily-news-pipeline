# P0: preserve verified daily content after model fill failure

`run_enrichment.process_batch` snapshots the fully selected, deduplicated,
importance-qualified publishable list before each main model batch. A known
DeepSeek request/incomplete-response error or structurally invalid response batch
stops all further initial and fallback work. With at least one item in that
snapshot, finalization continues with that exact prior selection; failed response
content is never recovered or parsed for publication. Zero publishable still
propagates the existing model/validation failure. Storage and unexpected internal
errors remain fatal; this is not a catch-all suppression of failures.

The original quality, deduplication, bilingual validator, prompts, batch size,
40k budget and network retries are unchanged. Modes still use the final verified
count: >=10 normal, 5-9 graceful_degraded, 1-4 minimal_daily, 0 true_failure.

Fill state, failed batch IDs, actual finish reason, verified count at failure and
unsubmitted initial/fallback candidate count are recorded in run audit and
supply diagnostics. Main batch records contain request bytes, submitted clean
text characters, output cap, actual input/output/total usage, finish reason,
parse status, observed output-limit hit, retry count and final batch status.
Missing observations are `not_observable`. A token-limit hit does not establish
the finish reason. Actual failed-response usage is counted once in SQLite and
included in this run's usage totals, with no synthetic usage for failed transport.

Output allocation is unchanged:

```
min(global_output_cap,
    max(1800, ceil(global_output_cap * batch_size / configured_batch_size)))
```

The formal CMD config uses batch size 6 and global cap 7200. Two articles receive
2400 output tokens. This is allocation arithmetic, not proof of why a historical
response ended; old missing finish_reason remains unobservable.

No delivery failure reason is created from a recovered fill warning. If the
report is deployed, passes H5/JSON readiness and Feishu delivery, the existing
exit mapping returns 0. Deployment/readiness/Feishu failures still return 5/6/7.
Local tests replace all external operations and do not execute a production run.
