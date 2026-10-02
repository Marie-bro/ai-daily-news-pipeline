"""Phase 6 delivery: publish-gated Feishu notifications for Tech Daily."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import uuid
import re
from queue import Queue, Empty
from threading import Thread
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .enrich import run_enrichment
from .diagnostics import failure_details
from .pipeline import run_collection
from .publish import PublishError, publish_latest_report
from .store import ArticleStore
from .run_audit import RunAudit

SHANGHAI = ZoneInfo("Asia/Shanghai")
FORMAL_BASE_URL = "https://news.mariespace.cn/"
DELIVERY_LOG = "data/delivery-runs.jsonl"
# Backoff saturates at 30 seconds; the deadline, rather than a retry count, ends the wait.
PUBLIC_VERIFY_DELAYS_SECONDS = (5, 10, 15, 30)
PUBLIC_VERIFY_MAX_WAIT_SECONDS = 210


class DeliveryError(RuntimeError):
    pass


class PublishVerificationError(DeliveryError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    report_date: str
    report_id: str | None
    status: str
    reason: str | None
    message_id: str | None
    url: str | None
    retry_count: int


@dataclass(frozen=True)
class ExistingFeishuSettings:
    app_id: str
    app_secret: str

    def missing_credentials(self) -> list[str]:
        return [name for name, value in (("FEISHU_APP_ID", self.app_id), ("FEISHU_APP_SECRET", self.app_secret)) if not value]


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)


def daily_url(report_date: str) -> str:
    if len(report_date) != 10 or report_date[4] != "-" or report_date[7] != "-":
        raise DeliveryError("invalid report date")
    return f"{FORMAL_BASE_URL}daily/ai/?date={report_date}"


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"cannot read report data: {path.name}") from exc
    if not isinstance(value, dict):
        raise DeliveryError("report data must be an object")
    return value


def load_report(site_root: Path, report_date: str) -> dict[str, object]:
    report = _read_json(site_root / "data" / "daily" / "ai" / f"{report_date}.json")
    items = report.get("items")
    if report.get("report_date") != report_date or not isinstance(items, list) or not items:
        raise DeliveryError("report is missing or empty")
    if type(report.get("article_count")) is not int or report["article_count"] != len(items):
        raise DeliveryError("report article count is invalid")
    for item in items:
        if not isinstance(item, dict):
            raise DeliveryError("report contains an invalid item")
        for key in ("title_en", "title_cn", "what_happened_en", "what_happened", "why_it_matters_en", "why_it_matters", "source", "published_at", "original_url"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise DeliveryError(f"report item is missing {key}")
        if not item["original_url"].startswith("https://"):
            raise DeliveryError("report item has an invalid original URL")
    return report


def report_id(report: dict[str, object]) -> str:
    stable = {key: report.get(key) for key in ("schema_version", "report_date", "published_at", "article_count", "items")}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def _favorite_url(report_date: str, item: dict[str, object]) -> str:
    from urllib.parse import urlsplit, urlunsplit

    original = str(item["original_url"])
    parts = urlsplit(original)
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    article_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{daily_url(report_date)}&favorite={article_id}&client=phase65-queue1"


def card_for_report(report: dict[str, object], url: str) -> dict[str, object]:
    items = report["items"]
    assert isinstance(items, list)
    date = str(report["report_date"])
    stories = int(report["article_count"])
    minutes = int(report["estimated_reading_minutes"])
    elements: list[dict[str, object]] = [
        {"tag": "markdown", "content": f"**{date}**\n{stories} stories \u00b7 {minutes} min read\n{stories} \u6761\u8d44\u8baf \u00b7 \u9884\u8ba1\u9605\u8bfb {minutes} \u5206\u949f"},
        {"tag": "markdown", "content": "**Today's highlights**\n**\u4eca\u65e5\u79d1\u6280\u91cd\u70b9**"},
    ]
    for item in items[:3]:
        assert isinstance(item, dict)
        elements.extend([
            {"tag": "div", "text": {"tag": "plain_text", "content": str(item["title_en"])}},
            {"tag": "div", "text": {"tag": "plain_text", "content": str(item["title_cn"])}},
            {"tag": "markdown", "content": f"**What happened?**\n{item['what_happened_en']}"},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"\u53d1\u751f\u4e86\u4ec0\u4e48\uff1f\n{item['what_happened']}"}},
            {"tag": "markdown", "content": f"**Why it matters?**\n{item['why_it_matters_en']}"},
            {"tag": "div", "text": {"tag": "plain_text", "content": f"\u4e3a\u4ec0\u4e48\u503c\u5f97\u5173\u6ce8\uff1f\n{item['why_it_matters']}"}},
            {"tag": "action", "actions": [{
                "tag": "button", "type": "default", "url": _favorite_url(date, item),
                "text": {"tag": "plain_text", "content": "Save for Later\n\u6536\u85cf"},
            }]},
        ])
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button", "type": "primary", "url": url,
            "text": {"tag": "plain_text", "content": "View MarieSpace Radar\n\u67e5\u770b MarieSpace \u6bcf\u65e5\u96f7\u8fbe"},
        }],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "MarieSpace Radar\nMarieSpace \u6bcf\u65e5\u96f7\u8fbe"}},
        "elements": elements,
    }


def _verification_error_kind(exc: Exception) -> str:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(exc, HTTPError):
        return "http_non_200"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    message = str(reason).lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "name or service not known" in message or "getaddrinfo" in message or "name resolution" in message:
        return "dns"
    if "certificate" in message or "ssl" in message or "tls" in message:
        return "tls"
    return "network_other"


def _probe_public_url(endpoint: str, kind: str, report_date: str, opener, timeout: float) -> dict[str, object]:
    result = {"http_status": None, "exception_type": None, "error_message": None,
              "failure_type": None, "response_cache_headers": {}}
    def cache_headers(headers):
        return {name: headers.get(name) for name in
                ("Date", "Age", "Cache-Control", "ETag", "Last-Modified", "EO-Cache-Status", "Retry-After")
                if headers is not None and headers.get(name) is not None}
    try:
        with opener(Request(endpoint, headers={"User-Agent": "MarieSpace-Tech-Daily/1.0",
                                               "Cache-Control": "no-cache", "Pragma": "no-cache"}),
                    timeout=timeout) as response:
            result["http_status"] = getattr(response, "status", 200)
            result["response_cache_headers"] = cache_headers(getattr(response, "headers", None))
            if result["http_status"] != 200:
                result.update(failure_type="http_non_200", error_message=f"HTTP {result['http_status']}")
            elif kind == "json":
                public = json.loads(response.read().decode("utf-8"))
                if not isinstance(public, dict) or public.get("report_date") != report_date or not public.get("items"):
                    result.update(failure_type="invalid_report", error_message="daily data is missing or empty")
    except (HTTPError, URLError, OSError, ValueError) as exc:
        result.update(failure_type=_verification_error_kind(exc), exception_type=type(exc).__name__,
                      error_message=str(exc)[:300])
        if isinstance(exc, HTTPError):
            result["http_status"] = exc.code
            result["response_cache_headers"] = cache_headers(exc.headers)
        elif isinstance(exc, json.JSONDecodeError):
            result["failure_type"] = "invalid_json"
    return result


def _bounded_public_probe(endpoint: str, kind: str, report_date: str, opener, timeout: float) -> dict[str, object]:
    # Socket timeouts alone do not bound DNS or a slowly streaming read(). A daemon
    # worker bounds the caller's wait; a late result is discarded and cannot trigger sending.
    result_queue = Queue(maxsize=1)
    def worker():
        try:
            result_queue.put(_probe_public_url(endpoint, kind, report_date, opener, timeout))
        except Exception as exc:
            result_queue.put({"http_status": None, "exception_type": type(exc).__name__,
                              "error_message": "readiness probe failed", "failure_type": "network_other",
                              "response_cache_headers": {}})
    Thread(target=worker, name="radar-readiness", daemon=True).start()
    try:
        return result_queue.get(timeout=timeout)
    except Empty:
        return {"http_status": None, "exception_type": "TimeoutError", "error_message": "readiness request exceeded its time allowance",
                "failure_type": "timeout", "response_cache_headers": {}}


def verify_public_report(report: dict[str, object], url: str, *, opener=urlopen,
                         sleeper: Callable[[float], None] = time.sleep,
                         attempt_log: list[dict[str, object]] | None = None,
                         clock: Callable[[], float] = time.monotonic,
                         wall_clock: Callable[[], datetime] = _now,
                         readiness_log: dict[str, object] | None = None,
                         deployment_context: dict[str, object] | None = None) -> None:
    if not url.startswith(FORMAL_BASE_URL):
        raise PublishVerificationError("daily URL is not on the formal domain")
    report_date = str(report["report_date"])
    data_url = f"{FORMAL_BASE_URL}data/daily/ai/{report_date}.json"
    attempts = attempt_log if attempt_log is not None else []
    summary = readiness_log if readiness_log is not None else {}
    deployment = deployment_context or {}
    start = clock()
    started_at = wall_clock()
    deadline = start + PUBLIC_VERIFY_MAX_WAIT_SECONDS
    push_offset_ms = None
    try:
        pushed = datetime.fromisoformat(deployment["deploy_push_finished_time"])
        offset = (started_at - pushed).total_seconds() * 1000
        if offset >= 0: push_offset_ms = offset
    except (KeyError, TypeError, ValueError):
        pass  # A manual verify or an old run has no observable push timestamp.
    summary.update({"deploy_commit_sha": deployment.get("deploy_commit_sha"),
                    "deploy_push_finished_time": deployment.get("deploy_push_finished_time"),
                    "readiness_start_time": started_at.isoformat(),
                    "readiness_deadline": (started_at + timedelta(seconds=PUBLIC_VERIFY_MAX_WAIT_SECONDS)).isoformat(),
                    "h5_first_200_after_ms": None, "json_first_200_after_ms": None,
                    "h5_first_200_at": None, "json_first_200_at": None,
                    "readiness_result": "waiting", "readiness_max_wait_seconds": PUBLIC_VERIFY_MAX_WAIT_SECONDS})
    ready = {"h5": False, "json": False}
    attempt = 0
    while clock() < deadline:
        attempt += 1
        for kind, endpoint in (("h5", url), ("json", data_url)):
            if ready[kind]:
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                break
            request_started = clock()
            entry = {"timestamp": wall_clock().isoformat(), "attempt": attempt, "kind": kind, "url": endpoint,
                     "deploy_commit_sha": summary["deploy_commit_sha"],
                     "deploy_push_finished_time": summary["deploy_push_finished_time"],
                     "readiness_start_time": summary["readiness_start_time"], "deadline": summary["readiness_deadline"]}
            entry.update(_bounded_public_probe(endpoint, kind, report_date, opener, min(20, remaining)))
            elapsed = max(0, (clock() - start) * 1000)
            timestamp = wall_clock().isoformat()
            if entry["http_status"] == 200 and summary[f"{kind}_first_200_at"] is None:
                summary[f"{kind}_first_200_at"] = timestamp
                summary[f"{kind}_first_200_after_ms"] = round(push_offset_ms + elapsed, 2) if push_offset_ms is not None else None
            ready[kind] = entry["http_status"] == 200 and entry["failure_type"] is None and clock() < deadline
            entry.update({"elapsed_ms": round(max(0, (clock() - request_started) * 1000), 2),
                          "elapsed_since_push_ms": round(push_offset_ms + elapsed, 2) if push_offset_ms is not None else None,
                          "elapsed_since_readiness_ms": round(elapsed, 2),
                          "first_200_at": summary[f"{kind}_first_200_at"],
                          "status": "ready" if ready[kind] else "not_ready_yet"})
            attempts.append(entry)
        if all(ready.values()) and clock() < deadline:
            summary.update(readiness_result="ready", readiness_total_ms=round((clock() - start) * 1000, 2))
            return
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(PUBLIC_VERIFY_DELAYS_SECONDS[min(attempt - 1, len(PUBLIC_VERIFY_DELAYS_SECONDS) - 1)], remaining))
    summary.update(readiness_result="publish_verification_failed", readiness_total_ms=round((clock() - start) * 1000, 2))
    raise PublishVerificationError("formal daily URL could not be verified")


def _phase_one_root(pipeline_root: Path) -> Path:
    root = pipeline_root.parent / "feishu-deepseek-assistant"
    if not (root / "app" / "feishu" / "client.py").is_file():
        raise DeliveryError("existing Feishu delivery client is unavailable")
    return root


def _existing_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "TARGET_OPEN_ID", "TARGET_CHAT_ID"}:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def target_from_existing_settings(pipeline_root: Path) -> tuple[object, str, str]:
    phase_one = _phase_one_root(pipeline_root)
    local = _existing_env_values(phase_one / ".env")
    sys.path.insert(0, str(phase_one))
    try:
        from app.feishu.client import FeishuClient
    finally:
        sys.path.pop(0)
    settings = ExistingFeishuSettings(os.getenv("FEISHU_APP_ID", local.get("FEISHU_APP_ID", "")).strip(),
                                      os.getenv("FEISHU_APP_SECRET", local.get("FEISHU_APP_SECRET", "")).strip())
    open_id = os.getenv("TARGET_OPEN_ID", local.get("TARGET_OPEN_ID", "")).strip()
    chat_id = os.getenv("TARGET_CHAT_ID", local.get("TARGET_CHAT_ID", "")).strip()
    if bool(open_id) == bool(chat_id):
        raise DeliveryError("exactly one existing Feishu target must be configured")
    return FeishuClient(settings), ("open_id" if open_id else "chat_id"), (open_id or chat_id)


def append_run_log(pipeline_root: Path, record: dict[str, object]) -> None:
    path = pipeline_root / DELIVERY_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: value for key, value in record.items() if key not in {"access_token", "app_secret", "api_key"}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")


def send_existing_report(pipeline_root: Path, site_root: Path, report_date: str, *, force: bool = False,
                         dry_run: bool = False, send_dry_run: bool = False, verify_only: bool = False, now: datetime | None = None,
                         sender: Callable[[str, str, dict[str, object], str], tuple[str, int]] | None = None,
                         target: tuple[str, str] | None = None,
                         opener=urlopen, verification_sleeper: Callable[[float], None] = time.sleep,
                         verification_clock: Callable[[], float] = time.monotonic,
                         verification_wall_clock: Callable[[], datetime] = _now,
                         deployment_context: dict[str, object] | None = None, audit=None) -> DeliveryResult:
    started = _now(now)
    record: dict[str, object] = {"run_date": started.date().isoformat(), "start_time": started.isoformat(), "candidate_count": 0,
                                 "selected_count": 0, "report_generated": False, "report_url": None, "url_reachable": False,
                                 "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                                 "skipped_reason": None, "retry_count": 0, "verification_retry_count": 0}
    readiness = {}
    if deployment_context:
        record.update({key: deployment_context.get(key) for key in
                       ("deploy_commit_sha", "deploy_push_finished_time", "deploy_push_status", "deployment_id", "deployment_status")})
    try:
        report = load_report(site_root, report_date)
        identifier = report_id(report)
        url = daily_url(report_date)
        record.update({"candidate_count": report["article_count"], "selected_count": report["article_count"], "report_generated": True,
                       "report_url": url})
        card = card_for_report(report, url)
        if dry_run or send_dry_run:
            record["skipped_reason"] = "dry_run" if dry_run else "feishu_send_dry_run"
            return DeliveryResult(report_date, identifier, "dry_run", str(record["skipped_reason"]), None, url, 0)
        record["verification_attempts"] = []
        verify_public_report(report, url, opener=opener, sleeper=verification_sleeper,
                             attempt_log=record["verification_attempts"], clock=verification_clock,
                             wall_clock=verification_wall_clock, readiness_log=readiness,
                             deployment_context=deployment_context)
        record["url_reachable"] = True
        if verify_only:
            record["skipped_reason"] = "verified_without_send"
            return DeliveryResult(report_date, identifier, "verified", "verified_without_send", None, url, 0)
        if sender is None:
            client, target_type, target_id = target_from_existing_settings(pipeline_root)

            def sender(target_type: str, target_id: str, card: dict[str, object], request_id: str) -> tuple[str, int]:
                return client.send_interactive_card(target_id, target_type, card, request_id), client.last_retry_count
        elif target is not None:
            target_type, target_id = target
        else:
            _client, target_type, target_id = target_from_existing_settings(pipeline_root)

        store = ArticleStore(pipeline_root / "data" / "news.sqlite3")
        try:
            if not force and store.delivery_already_recorded(report_date=report_date, report_id=identifier, target_type=target_type, target_id=target_id):
                record["skipped_reason"] = "already_sent_or_pending"
                return DeliveryResult(report_date, identifier, "skipped", "already_sent_or_pending", None, url, 0)
            request_id = str(uuid.uuid4())
            attempt_id = store.create_delivery_attempt(report_date=report_date, report_id=identifier, target_type=target_type,
                                                       target_id=target_id, request_id=request_id, created_at=started.isoformat(), force_resend=force)
            record["feishu_send_attempted"] = True
            try:
                message_id, retries = sender(target_type, target_id, card, request_id)
            except Exception:
                store.finish_delivery_attempt(attempt_id, status="uncertain", updated_at=_now(now).isoformat(), retry_count=0,
                                              error_summary="Feishu send failed or outcome is uncertain")
                record.update({"feishu_send_result": "uncertain", "skipped_reason": "feishu_send_failed_or_uncertain"})
                return DeliveryResult(report_date, identifier, "uncertain", "feishu_send_failed_or_uncertain", None, url, 0)
            store.finish_delivery_attempt(attempt_id, status="sent", updated_at=_now(now).isoformat(), message_id=message_id, retry_count=retries)
            record.update({"feishu_send_result": "sent", "message_id": message_id, "retry_count": retries})
            return DeliveryResult(report_date, identifier, "sent", None, message_id, url, retries)
        finally:
            store.close()
    except PublishVerificationError:
        record["skipped_reason"] = "publish_verification_failed"
        return DeliveryResult(report_date, identifier, "skipped", "publish_verification_failed", None, url, 0)
    except DeliveryError as exc:
        record["skipped_reason"] = str(exc)
        return DeliveryResult(report_date, None, "skipped", str(exc), None, None, 0)
    finally:
        record.update(readiness)
        if audit:
            audit.metrics.update({key: record[key] for key in
                                  ("deploy_commit_sha", "deploy_push_finished_time", "deploy_push_status", "deployment_id", "deployment_status",
                                   "readiness_start_time", "readiness_deadline", "h5_first_200_at", "json_first_200_at",
                                   "h5_first_200_after_ms", "json_first_200_after_ms", "readiness_total_ms", "readiness_result",
                                   "verification_attempts") if key in record})
        if record.get("verification_attempts"):
            record["verification_retry_count"] = max(row["attempt"] for row in record["verification_attempts"]) - 1
        record["end_time"] = _now(now).isoformat()
        append_run_log(pipeline_root, record)


def deploy_site_data(site_root: Path, report_path: Path) -> dict[str, object]:
    try:
        relative_report = report_path.relative_to(site_root)
    except ValueError as exc:
        raise DeliveryError("report path is outside the site project") from exc
    for command in (
        ["git", "add", "--", str(relative_report), "data/reports.json"],
        ["git", "diff", "--cached", "--quiet"],
    ):
        result = subprocess.run(command, cwd=site_root, capture_output=True, text=True, check=False)
        if command[1:3] == ["diff", "--cached"]:
            if result.returncode == 0:
                return {"deploy_commit_sha": _deploy_commit_sha(site_root), "deploy_push_finished_time": None,
                        "deploy_push_status": "not_attempted_no_changes", "deployment_id": None, "deployment_status": "unknown"}
            if result.returncode != 1:
                raise DeliveryError("cannot inspect staged site data")
        elif result.returncode != 0:
            raise DeliveryError("cannot stage site data")
    date = relative_report.stem
    for command in (["git", "commit", "-m", f"Publish Tech Daily {date}"], ["git", "push", "origin", "main"]):
        result = subprocess.run(command, cwd=site_root, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeliveryError("site data deployment failed")
    push_finished_time = _now().isoformat()
    return {"deploy_commit_sha": _deploy_commit_sha(site_root), "deploy_push_finished_time": push_finished_time,
            "deploy_push_status": "pushed", "deployment_id": None, "deployment_status": "unknown"}


def _deploy_commit_sha(site_root: Path) -> str | None:
    # A diagnostic read never replaces a successful push with an observability failure.
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=site_root, capture_output=True, text=True, check=False)
        sha = result.stdout.strip()
        return sha if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None
    except OSError:
        return None


def _run_scheduled_delivery(pipeline_root: Path, site_root: Path, *, force: bool = False, now: datetime | None = None,
                            audit=None, token_budget_override: int | None = None) -> DeliveryResult:
    current = _now(now)
    def record_failure(exc: Exception, default_stage: str, candidate_count: int = 0) -> DeliveryResult:
        details = failure_details(exc, default_stage)
        append_run_log(pipeline_root, {"run_date": current.date().isoformat(), "start_time": current.isoformat(),
                                      "end_time": _now(now).isoformat(), "candidate_count": candidate_count,
                                      "selected_count": 0, "report_generated": False, "report_url": None,
                                      "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None,
                                      "message_id": None, "retry_count": 0, **details})
        return DeliveryResult(current.date().isoformat(), None, "skipped", str(details["skipped_reason"]), None, None, 0)

    try:
        collection = run_collection(pipeline_root, dry_run=False, audit=audit)
    except Exception as exc:
        return record_failure(exc, "collection")
    try:
        enrichment_kwargs = {"token_budget_override": token_budget_override} if token_budget_override is not None else {}
        enrichment = run_enrichment(pipeline_root, audit=audit, **enrichment_kwargs)
    except Exception as exc:
        return record_failure(exc, "enrichment", collection.accepted)
    if not enrichment.output_path or enrichment.saved <= 0:
        budget_failed = enrichment.token_budget_status == "exhausted_before_minimum"
        reason = "token_budget_exhausted" if budget_failed else "no_qualified_tech_news"
        record = {"run_date": current.date().isoformat(), "start_time": current.isoformat(), "end_time": _now(now).isoformat(),
                  "candidate_count": enrichment.candidates, "selected_count": 0, "report_generated": False, "report_url": None,
                  "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                  "skipped_reason": reason, "retry_count": 0, "collection_inserted": collection.inserted,
                  "token_budget_status": enrichment.token_budget_status}
        append_run_log(pipeline_root, record)
        return DeliveryResult(current.date().isoformat(), None, "daily_failed" if budget_failed else "skipped", reason, None, None, 0)
    try:
        report_path = publish_latest_report(pipeline_root, site_root)
        deployment = deploy_site_data(site_root, report_path)
        if audit: audit.metrics.update(deployment)
    except (PublishError, DeliveryError):
        append_run_log(pipeline_root, {"run_date": current.date().isoformat(), "start_time": current.isoformat(), "end_time": _now(now).isoformat(),
                                      "candidate_count": enrichment.candidates, "selected_count": enrichment.saved, "report_generated": False, "report_url": None,
                                      "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                                      "skipped_reason": "publication_or_deployment_failed", "retry_count": 0})
        return DeliveryResult(current.date().isoformat(), None, "skipped", "publication_or_deployment_failed", None, None, 0)
    return send_existing_report(pipeline_root, site_root, report_path.stem, force=force, now=now,
                                deployment_context=deployment, audit=audit)


def run_scheduled_delivery(pipeline_root: Path, site_root: Path, *, force: bool = False, now: datetime | None = None,
                           token_budget_override: int | None = None) -> DeliveryResult:
    audit = RunAudit(pipeline_root, _now(now))
    result = None
    try:
        result = _run_scheduled_delivery(pipeline_root, site_root, force=force, now=now, audit=audit,
                                         token_budget_override=token_budget_override)
        return result
    finally:
        try:
            audit.save(status=result.status if result else "unhandled_failure",
                       report_url=result.url if result else None,
                       report_id=result.report_id if result else None,
                       feishu_send_result=result.status if result and result.status in {"sent", "uncertain"} else "not_sent",
                       message_id=result.message_id if result else None,
                       skipped_reason=result.reason if result else "unhandled_failure")
        except Exception:
            pass  # Audit remains best-effort and cannot change delivery outcome.
