from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .deepseek import DeepSeekClient, DeepSeekError
from .bilingual import validate_literal_consistency
from .sources import BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS
from .store import ArticleStore

TASK_NAME = "phase5_5_bilingual_history_backfill"
SYSTEM_PROMPT = """You turn compact, already-published daily-report fields into exact bilingual pairs.
Use only facts supplied for each item. Do not read or infer from the URL, and do not add facts, numbers, dates, versions, product names, technical names, claims, or certainty. Return every id once.
For each item produce title_en then title_cn; what_happened_en then what_happened; why_it_matters_en then why_it_matters. English must be natural and first. Chinese must be natural and second. Every English/Chinese pair must state exactly the same facts. Do not provide original-body excerpts, key points, full translations, extra summaries, Markdown, or commentary.
Return only JSON: {"items":[{"id":"","title_en":"","title_cn":"","what_happened_en":"","what_happened":"","why_it_matters_en":"","why_it_matters":""}]}.
"""


class BilingualBackfillError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BilingualBackfillError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BilingualBackfillError(f"{path} must contain an object")
    return value


def _string(value: object, field: str, item_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BilingualBackfillError(f"Item {item_id} has an empty {field}")
    return value.strip()


def _candidate_payload(items: list[dict[str, object]]) -> str:
    candidates = []
    for index, item in enumerate(items):
        candidates.append({
            "id": str(index), "title_original": item.get("title_original"), "title_cn": item.get("title_cn"),
            "summary_en": item.get("summary_en"), "summary_cn": item.get("summary_cn"),
            "relevance": item.get("relevance"), "source": item.get("source"),
            "published_at": item.get("published_at"), "original_url": item.get("original_url"),
        })
    return "Published compact report items:\n" + json.dumps({"items": candidates}, ensure_ascii=False)


def _validated_pairs(payload: dict[str, object], source_items: list[dict[str, object]]) -> list[dict[str, str]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(source_items):
        raise BilingualBackfillError("DeepSeek must return one bilingual pair for each report item")
    by_id: dict[str, dict[str, object]] = {}
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in by_id:
            raise BilingualBackfillError("Every backfill item needs a unique id")
        by_id[item["id"]] = item
    if set(by_id) != {str(index) for index in range(len(source_items))}:
        raise BilingualBackfillError("Backfill item ids do not match the report")
    pairs: list[dict[str, str]] = []
    for index, source in enumerate(source_items):
        item = by_id[str(index)]
        pair = {field: _string(item.get(field), field, str(index)) for field in (
            "title_en", "title_cn", "what_happened_en", "what_happened", "why_it_matters_en", "why_it_matters"
        )}
        try:
            validate_literal_consistency(pair["title_en"], pair["title_cn"], "title")
            validate_literal_consistency(pair["what_happened_en"], pair["what_happened"], "what_happened")
            validate_literal_consistency(pair["why_it_matters_en"], pair["why_it_matters"], "why_it_matters")
        except ValueError as exc:
            raise BilingualBackfillError(f"Item {index} has inconsistent bilingual literals: {exc}") from exc
        if BLOCKED_CONTENT_TERMS.search(json.dumps(pair, ensure_ascii=False)):
            raise BilingualBackfillError(f"Item {index} contains blocked content")
        url = _string(source.get("original_url"), "original_url", str(index))
        host = (urlparse(url).hostname or "").lower()
        if not host or any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_CONTENT_HOSTS):
            raise BilingualBackfillError(f"Item {index} has a blocked original URL")
        pairs.append(pair)
    return pairs


def backfill_report(pipeline_root: Path, site_root: Path, report_date: str, *, dry_run: bool = False) -> dict[str, object]:
    report_path = site_root / "data" / "daily" / "ai" / f"{report_date}.json"
    report = _read_json(report_path)
    items = report.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        raise BilingualBackfillError("The selected report has no valid items")
    if report.get("schema_version", 1) >= 3:
        return {"report_date": report_date, "updated": 0, "already_bilingual": True}
    prompt = _candidate_payload(items)
    if dry_run:
        return {"report_date": report_date, "candidates": len(items), "prompt_characters": len(prompt), "dry_run": True}
    now = datetime.now(UTC)
    try:
        content, usage, model = DeepSeekClient().complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=prompt, max_tokens=1_800)
    except DeepSeekError as exc:
        if exc.usage is not None:
            store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
            try:
                store.record_usage(task=f"{TASK_NAME}_rejected", model=exc.model or "unknown", created_at=now.isoformat(), usage=exc.usage)
                store.commit()
            finally:
                store.close()
        raise
    try:
        pairs = _validated_pairs(json.loads(content), items)
    except (json.JSONDecodeError, BilingualBackfillError) as exc:
        store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
        try:
            store.record_usage(task=f"{TASK_NAME}_rejected", model=model, created_at=now.isoformat(), usage=usage)
            store.commit()
        finally:
            store.close()
        raise BilingualBackfillError("The bilingual backfill response was invalid") from exc
    store = ArticleStore(pipeline_root / "data" / "ai_daily.sqlite3")
    try:
        store.record_usage(task=TASK_NAME, model=model, created_at=now.isoformat(), usage=usage)
        store.commit()
    finally:
        store.close()
    updated_items = [{**item, **pair} for item, pair in zip(items, pairs, strict=True)]
    report.update({"schema_version": 3, "category": "tech", "items": updated_items})
    report["highlights"] = [f"{item['title_en']} / {item['title_cn']}" for item in updated_items[:3]]
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path = site_root / "data" / "reports.json"
    index = _read_json(index_path)
    reports = index.get("reports")
    if isinstance(reports, list):
        for summary in reports:
            if isinstance(summary, dict) and summary.get("report_date") == report_date:
                summary.update({"category": "tech", "schema_version": 3, "highlights": report["highlights"]})
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"report_date": report_date, "updated": len(updated_items), "model": model, "usage": usage}
