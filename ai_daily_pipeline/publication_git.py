"""Bounded, credential-safe publication Git operations (push is not readiness)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone


def redact(value: object) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    # Cover URL credentials, query credentials, headers, and common PAT formats.
    text = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\r\n]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:access[_-]?token|token|username|password|passwd|secret|api[_-]?key)\s*[=:]\s*)[^\s&\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", text)
    for key, secret in os.environ.items():
        if re.search(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY", key, re.I) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return text


def error_class(message: str, exception_type: str | None = None) -> str:
    value = message.lower()
    if exception_type == "TimeoutExpired" or "timed out" in value: return "timeout"
    if any(x in value for x in ("authentication failed", "could not read username", "terminal prompts disabled", "invalid username", "permission denied (publickey)")): return "authentication"
    if any(x in value for x in ("non-fast-forward", "fetch first", "tip of your current branch is behind")): return "non_fast_forward"
    if "remote rejected" in value or "protected branch" in value: return "remote_rejected"
    if any(x in value for x in ("repository not found", "repository unavailable", "does not appear to be a git repository")): return "repository_unavailable"
    if any(x in value for x in ("ssl", "tls", "certificate", "schannel")): return "tls"
    if any(x in value for x in ("could not resolve", "failed to connect", "connection reset", "network is unreachable", "remote end hung up", "empty reply", "recv failure")): return "dns_network"
    return "unknown"


class PublicationGitError(RuntimeError):
    def __init__(self, diagnostics: dict):
        super().__init__("site data deployment failed")
        self.diagnostics = diagnostics


class PublicationGit:
    def __init__(self, site: Path, *, diagnostic_path: Path | None = None, now=None):
        self.site = site.resolve()
        self.path = diagnostic_path
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.context = {"remote": "origin", "branch": "main", "local_head": None, "upstream_head": None,
                        "environment_proxy_mode": {k: bool(os.environ.get(k) or os.environ.get(k.lower())) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}}
        self.records = []
        self.receipt = {"publish_push_status": "not_attempted", "publish_push_exit_code": None,
                        "publish_push_error_class": None, "publish_push_stderr_safe": "",
                        "deploy_push_finished_time": None, "deployment_id": None, "deployment_status": "unknown"}

    def diagnostics(self):
        return {**self.receipt, "publish_git_commit_sha": self.context["local_head"],
                "deploy_commit_sha": self.context["local_head"], "publish_git_commands": list(self.records)}

    def run(self, args, allowed=(0,), *, presence_only=False):
        command = ["git", *args]
        start = self.now().isoformat()
        tick = time.monotonic()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
        exception_type = None
        try:
            result = subprocess.run(command, cwd=self.site, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", check=False, timeout=120, env=env)
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, stdout = None, getattr(exc, "stdout", "")
            stderr = getattr(exc, "stderr", "") or str(exc)
            exception_type = type(exc).__name__
        end = self.now().isoformat()
        record = {**self.context, "command": [redact(x) for x in command], "cwd": str(self.site),
                  "exit_code": code, "stdout": ("present" if stdout else "absent") if presence_only else redact(stdout), "stderr": redact(stderr),
                  "exception_type": exception_type, "start_time": start, "end_time": end,
                  "elapsed_ms": round((time.monotonic() - tick) * 1000)}
        self.records.append(record)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                record["diagnostic_log_write_failed"] = True
        if args[0] == "push":
            self.receipt.update(publish_push_status="pushed" if code == 0 else "failed",
                                deploy_push_status="pushed" if code == 0 else "failed",
                                publish_push_exit_code=code,
                                publish_push_error_class=None if code == 0 else error_class(record["stderr"], exception_type),
                                publish_push_stderr_safe=record["stderr"],
                                deploy_push_finished_time=end if code == 0 else None)
        if code not in allowed:
            raise PublicationGitError(self.diagnostics())
        return code, str(stdout or "").strip()

    def inspect(self):
        self.context["git_config_proxy_present"] = {}
        for key in ("http.proxy", "https.proxy"):
            _, value = self.run(["config", "--get", key], allowed=(0, 1), presence_only=True)
            self.context["git_config_proxy_present"][key] = bool(value)
        self.context["remote"] = redact(self.run(["remote", "get-url", "origin"])[1])
        self.context["branch"] = self.run(["branch", "--show-current"])[1]
        self.context["local_head"] = self.run(["rev-parse", "HEAD"])[1]
        code, upstream = self.run(["rev-parse", "--verify", "origin/main"], allowed=(0, 128))
        self.context["upstream_head"] = upstream if code == 0 else None
        self.context["upstream_head_kind"] = "local_remote_tracking_ref_not_live"
        if self.context["branch"] != "main":
            raise PublicationGitError(self.diagnostics())

    def push_existing(self, expected_head: str):
        self.inspect()
        if self.context["local_head"] != expected_head:
            raise PublicationGitError(self.diagnostics())
        self.run(["push", "origin", "main"])
        return self.diagnostics()


def deploy(site: Path, report: Path, *, diagnostic_path=None, now=None):
    relative = report.resolve().relative_to(site.resolve())
    git = PublicationGit(site, diagnostic_path=diagnostic_path, now=now)
    git.inspect()
    # Never accidentally include unrelated staged work in a daily commit.
    staged = git.run(["diff", "--cached", "--name-only"])[1].splitlines()
    if set(staged) - {relative.as_posix(), "data/reports.json"}:
        raise PublicationGitError(git.diagnostics())
    git.run(["add", "--", relative.as_posix(), "data/reports.json"])
    changed, _ = git.run(["diff", "--cached", "--quiet"], allowed=(0, 1))
    if changed:
        git.run(["commit", "-m", f"Publish Tech Daily {relative.stem}"])
        git.context["local_head"] = git.run(["rev-parse", "HEAD"])[1]
    if not changed and git.context["local_head"] == git.context["upstream_head"]:
        git.receipt.update(deploy_push_status="not_attempted_no_changes", publish_push_status="not_attempted_no_changes")
    else:
        # Includes a previous commit whose push failed: do not create another commit.
        git.run(["push", "origin", "main"])
    return git.diagnostics()
