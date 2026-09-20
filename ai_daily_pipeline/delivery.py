"""Phase 6 delivery: publish-gated Feishu notifications for Tech Daily."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .enrich import run_enrichment
from .pipeline import run_collection
from .publish import PublishError, publish_latest_report
from .store import ArticleStore

SHANGHAI = ZoneInfo("Asia/Shanghai")
FORMAL_BASE_URL = "https://news.mariespace.cn/"
DELIVERY_LOG = "data/delivery-runs.jsonl"


class DeliveryError(RuntimeError):
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


def card_for_report(report: dict[str, object], url: str) -> dict[str, object]:
    items = report["items"]
    assert isinstance(items, list)
    highlights = []
    for item in items[:3]:
        assert isinstance(item, dict)
        highlights.extend([{"tag": "plain_text", "content": item["title_en"]}, {"tag": "plain_text", "content": item["title_cn"]}])
    date = str(report["report_date"])
    stories = int(report["article_count"])
    minutes = int(report["estimated_reading_minutes"])
    elements: list[dict[str, object]] = [
        {"tag": "markdown", "content": f"**{date}**\n{stories} stories · {minutes} min read\n{stories} 条资讯 · 预计阅读 {minutes} 分钟"},
        {"tag": "markdown", "content": "**Today's highlights**\n**今日科技重点**"},
    ]
    for title in highlights:
        elements.append({"tag": "div", "text": title})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button", "type": "primary", "url": url,
            "text": {"tag": "plain_text", "content": "Read Today's Tech Daily\n阅读今日科技日报"},
        }],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "MarieSpace Tech Daily\nMarieSpace 科技日报"}},
        "elements": elements,
    }


def verify_public_report(report: dict[str, object], url: str, *, opener=urlopen) -> None:
    if not url.startswith(FORMAL_BASE_URL):
        raise DeliveryError("daily URL is not on the formal domain")
    try:
        with opener(Request(url, headers={"User-Agent": "MarieSpace-Tech-Daily/1.0"}), timeout=20) as response:
            if getattr(response, "status", 200) != 200:
                raise DeliveryError("daily page is not reachable")
        report_date = str(report["report_date"])
        data_url = f"{FORMAL_BASE_URL}data/daily/ai/{report_date}.json"
        with opener(Request(data_url, headers={"User-Agent": "MarieSpace-Tech-Daily/1.0"}), timeout=20) as response:
            public = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise DeliveryError("formal daily URL could not be verified") from exc
    if not isinstance(public, dict) or public.get("report_date") != report_date or not public.get("items"):
        raise DeliveryError("formal daily data is missing or empty")


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
                         opener=urlopen) -> DeliveryResult:
    started = _now(now)
    record: dict[str, object] = {"run_date": started.date().isoformat(), "start_time": started.isoformat(), "candidate_count": 0,
                                 "selected_count": 0, "report_generated": False, "report_url": None, "url_reachable": False,
                                 "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                                 "skipped_reason": None, "retry_count": 0}
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
        verify_public_report(report, url, opener=opener)
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
    except DeliveryError as exc:
        record["skipped_reason"] = str(exc)
        return DeliveryResult(report_date, None, "skipped", str(exc), None, None, 0)
    finally:
        record["end_time"] = _now(now).isoformat()
        append_run_log(pipeline_root, record)


def deploy_site_data(site_root: Path, report_path: Path) -> None:
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
                return
            if result.returncode != 1:
                raise DeliveryError("cannot inspect staged site data")
        elif result.returncode != 0:
            raise DeliveryError("cannot stage site data")
    date = relative_report.stem
    for command in (["git", "commit", "-m", f"Publish Tech Daily {date}"], ["git", "push", "origin", "main"]):
        result = subprocess.run(command, cwd=site_root, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeliveryError("site data deployment failed")


def run_scheduled_delivery(pipeline_root: Path, site_root: Path, *, force: bool = False, now: datetime | None = None) -> DeliveryResult:
    current = _now(now)
    try:
        collection = run_collection(pipeline_root)
        enrichment = run_enrichment(pipeline_root)
    except Exception:
        append_run_log(pipeline_root, {"run_date": current.date().isoformat(), "start_time": current.isoformat(), "end_time": _now(now).isoformat(),
                                      "candidate_count": 0, "selected_count": 0, "report_generated": False, "report_url": None,
                                      "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                                      "skipped_reason": "collection_or_enrichment_failed", "retry_count": 0})
        return DeliveryResult(current.date().isoformat(), None, "skipped", "collection_or_enrichment_failed", None, None, 0)
    if not enrichment.output_path or enrichment.saved <= 0:
        record = {"run_date": current.date().isoformat(), "start_time": current.isoformat(), "end_time": _now(now).isoformat(),
                  "candidate_count": enrichment.candidates, "selected_count": 0, "report_generated": False, "report_url": None,
                  "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                  "skipped_reason": "no_qualified_tech_news", "retry_count": 0, "collection_inserted": collection.inserted}
        append_run_log(pipeline_root, record)
        return DeliveryResult(current.date().isoformat(), None, "skipped", "no_qualified_tech_news", None, None, 0)
    try:
        report_path = publish_latest_report(pipeline_root, site_root)
        deploy_site_data(site_root, report_path)
    except (PublishError, DeliveryError):
        append_run_log(pipeline_root, {"run_date": current.date().isoformat(), "start_time": current.isoformat(), "end_time": _now(now).isoformat(),
                                      "candidate_count": enrichment.candidates, "selected_count": enrichment.saved, "report_generated": False, "report_url": None,
                                      "url_reachable": False, "feishu_send_attempted": False, "feishu_send_result": None, "message_id": None,
                                      "skipped_reason": "publication_or_deployment_failed", "retry_count": 0})
        return DeliveryResult(current.date().isoformat(), None, "skipped", "publication_or_deployment_failed", None, None, 0)
    return send_existing_report(pipeline_root, site_root, report_path.stem, force=force, now=now)
