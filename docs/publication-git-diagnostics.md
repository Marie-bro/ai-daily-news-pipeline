# Publication Git diagnostics

Git push success is only a remote Git acknowledgement, never EdgeOne readiness.
The existing H5/JSON readiness gate and Feishu idempotency remain unchanged.

For MarieSpace public-site publication only, the push command carries
`-c http.sslBackend=openssl -c http.sslVerify=true`. It reuses the existing
Git proxy configuration (or existing HTTPS/ALL_PROXY environment settings)
without copying proxy addresses into code or logs. `GIT_SSL_NO_VERIFY` is
removed from that subprocess environment. No system/global/local Git setting,
Windows proxy or Clash setting is changed. Other Git operations and callers
retain their configured transport. An unavailable OpenSSL backend produces a
failed push classified as `tls_backend_unavailable`; there is no Schannel
fallback and no subsequent readiness check or Feishu send.

Each Git command records command, cwd, sanitized remote, branch, local HEAD,
local remote-tracking upstream HEAD (explicitly not a live remote query), exit code,
stdout/stderr, exception type, UTC/offset start and end timestamps, duration, and
presence-only proxy environment and Git http/https proxy configuration flags. A command is limited to 120 seconds;
noninteractive credential behavior prevents a scheduled process waiting for a prompt.
No automatic push retry is performed.

The production path appends redacted records to ignored `data/publication-git.jsonl`.
The complete receipt is also included in Run Audit metrics; failure details are
included in the existing delivery run log. Logs redact HTTP URL userinfo,
credential parameters, Authorization headers, GitHub tokens and known secret environment
values. Do not enable Git credential/debug tracing in production.

Receipt fields: publish_git_commit_sha, publish_push_status,
publish_push_exit_code, publish_push_error_class, publish_push_stderr_safe,
publish_git_commands, publish_tls_backend, publish_proxy_mode,
publish_local_head_after_push, publish_upstream_head_after_push. Both upstream
values are local remote-tracking refs, not live remote queries. Proxy mode is
`existing_git_config`, `existing_environment`, or `git_default_unverified`;
only the mode is recorded. Error classes: dns_network, tls,
tls_backend_unavailable, authentication,
non_fast_forward, remote_rejected, repository_unavailable, timeout, unknown.
A timeout has no observed exit code (null), and does not prove whether the
remote accepted the push; inspect the live remote before further action.

Push failure still yields publication_or_deployment_failed / application exit 5,
without readiness or Feishu. The report_generated flag reflects a returned local
artifact, independent of push success. Old logs are not rewritten.

If the same report is already committed but HEAD differs from origin/main, push
that existing commit without another commit. If both refs match and no files
changed, no push timestamp is invented. Unrelated staged files and non-main
branches block publication to avoid accidentally committing other work.
The remote-tracking reference may be stale: live remote checks are needed for
manual incident diagnosis. No pull, force push, reset, or merge is automatic.

Offline regression tests use temporary local bare repositories and mocked errors;
no daily collection, model request, deployment service or Feishu is invoked.
