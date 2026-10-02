# Per-source network policy and health measurements

P1-2 changes source transport and source observability only. It does not change
candidate eligibility, ranking, the model, publication or delivery.

## Source configuration

Optional JSON fields:

- `proxy_mode`: `system` (default) or `direct`. Direct uses an explicit empty
  urllib ProxyHandler, retaining certificate verification and an HTTPS/host
  allowlist for redirects. This bypasses environment/Windows explicit proxy
  discovery, not an operating-system TUN or transparent network route.
- `request_timeout_seconds`: socket timeout, integer 1–30, default 12.
- `request_attempts`: total attempts, integer 1–3, default 2.

The same source policy applies to index and article requests. Seven affected
factual sources retain their existing HTML endpoints, parser rules and 12-second
timeout. They use direct transport and at most two attempts. Baidu discovery uses
its same-origin JSON board, direct transport, six seconds and one attempt.

The system route retains its existing open-time URLError retry policy. Direct
sources retry transient URL/timeout/connection/TLS failures and HTTP 429/5xx,
with a 0.5-second wait before the second attempt. A new opener is created on each
attempt. HTTP 403, certificate verification failure, invalid redirect, parser
failure and size-limit failure are not retried. The existing 2 MB body limit is
unchanged. Socket timeout is not an end-to-end deadline: DNS, redirects and
several reads can extend total elapsed time.

`discovery_json` is restricted to Tier 4 discovery sources. It converts Baidu
JSON title/link records to the existing HTML index admission rules. An unexpected
API envelope is an explicit failure; an empty board is empty. Discovery items
remain excluded from Article normalization and therefore cannot become final
facts. No official versioned contract was found for this API; its health must be
observed instead of assuming long-term stability.

## Health metrics

The existing SQLite gains `source_health_events`; existing `source_health` rows
and their last successful timestamps remain compatible. One observation per
source check records status, checked_at, index duration and error kind. Disabled
sources do not contribute measurements. Measurements include parsing/cache work,
but not subsequent article downloads.

`source_health_metrics()` and the existing inventory command expose:

- success_count / timeout_count / empty_count / failure_count (disjoint)
- average_duration_ms
- p95_duration_ms: nearest rank, only after 20 measured durations; otherwise null
- last_success_at / consecutive_failures
- sample_count / duration_sample_count / statistics_since

Counters start when instrumentation is used; historical snapshots are not
invented or backfilled. Successful or empty checks reset the failure streak.
The next normal collection persists these metrics in the existing latest-run
source health and request details in Run Audit. Request histories contain
attempt, proxy mode, HTTP status, exception type, error kind, failure phase and
elapsed_ms. `request_open` does not claim to distinguish DNS/TCP/TLS phases.

The read-only scripts `diagnose_source_network.py` and
`check_repaired_sources.py` write diagnostic artifacts only. Their limited
parallelism is not production collection concurrency. They never write SQLite,
generate a report, call a model, deploy or send a notification.
