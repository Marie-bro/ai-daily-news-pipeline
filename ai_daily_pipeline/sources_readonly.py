"""Read-only, date-scoped source and article view over saved production artifacts."""
from __future__ import annotations

from datetime import datetime, date
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .sources import load_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")
UNAVAILABLE = "unavailable"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_STAGES = ("collected", "normalized", "candidate", "enrichment", "validation", "publishable")


def validate_date(value: str) -> str:
    if not _DATE.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be a real calendar date")
    return value


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _local_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI).date().isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _latest_run(root: Path, requested_date: str | None = None) -> dict | None:
    value = _read_json(root / "data" / "latest-run.json")
    if value is None:
        return None
    health = value.get("source_health") or []
    timestamps = [row.get("checked_at") for row in health if isinstance(row, dict)]
    dates = {_local_date(x) for x in timestamps}
    if len(dates) != 1 or None in dates:
        return None
    actual_date = dates.pop()
    return value if requested_date is None or actual_date == requested_date else None


def _audits(root: Path, requested_date: str | None = None) -> list[dict]:
    directory = root / "data" / "run-audits"
    results = []
    if not directory.is_dir():
        return results
    for path in directory.glob("*.json"):
        value = _read_json(path)
        if not value or not isinstance(value.get("run_id"), str):
            continue
        audit_date = value.get("date")
        if not isinstance(audit_date, str) or not _DATE.fullmatch(audit_date):
            continue
        if requested_date is None or audit_date == requested_date:
            results.append(value)
    results.sort(key=lambda row: (row.get("end_time") or "", row["run_id"]), reverse=True)
    return results


def available_dates(root: Path) -> list[str]:
    days = {row["date"] for row in _audits(root)}
    latest = _latest_run(root)
    if latest:
        days.add(_local_date(latest["source_health"][0]["checked_at"]))
    return sorted((day for day in days if day), reverse=True)


def _sqlite_metadata(root: Path, article_ids: list[str]) -> dict[str, dict]:
    path = root / "data" / "ai_daily.sqlite3"
    if not path.exists() or not article_ids:
        return {}
    uri = path.resolve().as_uri() + "?mode=ro"
    found = {}
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for start in range(0, len(article_ids), 800):
            chunk = article_ids[start:start + 800]
            marks = ",".join("?" for _ in chunk)
            cursor = connection.execute(
                f"SELECT id,source,title,published_at,original_url FROM articles WHERE id IN ({marks})", chunk)
            found.update((row["id"], dict(row)) for row in cursor)
    return found


def _event_state(events: list[dict], stage: str) -> str:
    def matches(event: dict) -> bool:
        name = event.get("stage", "")
        if stage == "collected":
            return name == "collection" or name.startswith("adapter_")
        if stage == "normalized":
            return name == "normalization"
        if stage == "candidate":
            return name in {"initial_candidate_pool", "initial_selection", "candidate_pool",
                            "initial_freshness", "initial_quality_filter", "initial_historical_dedup"}
        if stage == "enrichment":
            return name in {"enrichment", "token_budget"}
        return name == stage or name == "final_" + stage

    decisions = [e for e in events if matches(e)]
    if decisions:
        last = decisions[-1]
        if last.get("stage") == "token_budget":
            return "dropped"
        return str(last.get("status") or UNAVAILABLE)
    if stage == "validation" and any(e.get("stage") == "repair" and e.get("reason") == "repair_failed"
                                     for e in events):
        # A saved failed repair proves the item did not pass validation, although
        # older audits did not save the original validation rejection reason.
        return "dropped"
    if stage in {"collected", "normalized"} and any(
            e.get("stage") == "inventory" and e.get("reason") == "historical_inventory" for e in events):
        return "not_in_current_run"
    # Only a drop *before* this boundary proves it was not reached. A later
    # selection drop cannot prove that a historical inventory item was never
    # normalized in the current collection run.
    terminal_before = {
        "normalized": {"source_url_dedup", "feed_freshness", "source_cap"},
        "candidate": {"source_url_dedup", "feed_freshness", "source_cap", "normalization",
                      "extracted_freshness", "release_burst_dedup", "collection_cap", "content_gate"},
        "enrichment": {"source_url_dedup", "feed_freshness", "source_cap", "normalization",
                       "extracted_freshness", "release_burst_dedup", "collection_cap", "content_gate",
                       "initial_freshness", "initial_quality_filter", "initial_historical_dedup", "initial_selection"},
        "validation": {"enrichment", "token_budget"},
        "publishable": {"validation", "repair"},
    }
    for index in range(2, _STAGES.index(stage) + 1):
        terminal_before[_STAGES[index]] |= terminal_before[_STAGES[index - 1]]
    if any(e.get("status") == "dropped" and
           (e.get("stage", "").startswith("adapter_") or e.get("stage") in terminal_before.get(stage, set()))
           for e in events):
        return "not_reached"
    return UNAVAILABLE


def _normalize_status(value: object) -> str:
    if value in (None, "not_observable_from_existing_data"):
        return UNAVAILABLE
    if value in ("selected_initial", "passed", "accepted", "offline_selected", "kept"):
        return "kept"
    if value in ("rejected", "offline_excluded", "validation_failed", "dropped", "batch_not_submitted"):
        return "dropped"
    if value in ("not_requested", "not_reached"):
        return "not_reached"
    return str(value)


def _article(row: dict, sqlite_row: dict | None, origin: str) -> dict:
    row_id = row.get("article_id") or row.get("id") or row.get("trace_id")
    sqlite_row = sqlite_row or {}
    events = row.get("events") if isinstance(row.get("events"), list) else []
    supply = {}
    for event in events:
        detail = event.get("detail") or {}
        for field in ("supply_layer", "is_today", "locked_today", "selected_as_fallback",
                      "fallback_priority", "historical_age_days", "reserve_status", "reserve_type",
                      "reserve_reason", "policy_validity", "deep_read", "cache_reused"):
            if field in detail:
                supply[field] = detail[field]
    if origin == "offline":
        historical_names = {"collected": "collection", "normalized": "normalization"}
        stages = {stage: _normalize_status(row.get(historical_names.get(stage, stage))) for stage in _STAGES}
        if row.get("enrichment") in {"accepted", "rejected"}:
            stages["enrichment"] = "kept"  # The model returned; rejected refers to validation.
        if stages["validation"] not in {"kept", "dropped", "not_reached", UNAVAILABLE}:
            stages["validation"] = "dropped"  # The saved value is a reject stage, not a status.
    elif origin == "latest-run":
        stages = {stage: ("kept" if stage == "collected" else UNAVAILABLE) for stage in _STAGES}
    else:
        stages = {stage: _event_state(events, stage) for stage in _STAGES}
    original_url = row.get("original_url") or sqlite_row.get("original_url")
    if not isinstance(original_url, str) or urlparse(original_url).scheme != "https":
        original_url = None
    drops = [event for event in events if event.get("status") == "dropped"]
    reason = (row.get("screening_reason") or (drops[-1].get("reason") if drops else None)
              or row.get("final_reason"))
    if reason == "not_observable_from_existing_data":
        reason = UNAVAILABLE
    return {
        "article_id": row_id or UNAVAILABLE,
        "trace_id": row.get("trace_id") or UNAVAILABLE,
        "source": row.get("source") or sqlite_row.get("source") or UNAVAILABLE,
        "source_id": row.get("source_id") if row.get("source_id") not in (None, "not_observable_from_existing_data") else UNAVAILABLE,
        "source_id_inferred_from_current_config": row.get("source_id_inferred_from_current_config"),
        "title": row.get("title") or row.get("title_en") or sqlite_row.get("title") or UNAVAILABLE,
        "published_at": row.get("published_at") or sqlite_row.get("published_at") or UNAVAILABLE,
        "original_url": original_url or UNAVAILABLE,
        "current_stage": row.get("current_stage") or (events[-1].get("stage") if events else UNAVAILABLE),
        "stages": stages,
        "final_status": row.get("screening_status") or row.get("final_status") or UNAVAILABLE,
        "reason": reason or UNAVAILABLE,
        "events": events,
        "supply": supply,
        "trace_provenance": "recorded" if origin == "audit" else "historical_offline_reconstruction" if origin == "offline" else "collection_snapshot_only",
    }


def _time_key(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


def sources_for_date(root: Path, requested_date: str, run_id: str | None = None) -> dict:
    requested_date = validate_date(requested_date)
    root = root.resolve()
    current_sources = [source for source in load_sources(root / "config" / "sources.json") if source.enabled]
    source_by_id = {source.source_id: source for source in current_sources}
    audits = _audits(root, requested_date)
    latest = _latest_run(root, requested_date)
    run_options = [{"run_id": value["run_id"], "end_time": value.get("end_time") or UNAVAILABLE,
                    "mode": value.get("mode") or "recorded_audit"} for value in audits]
    if not audits and latest:
        run_options.append({"run_id": "latest-run:" + requested_date, "end_time": UNAVAILABLE,
                            "mode": "collection_snapshot_only"})
    if run_id and run_id not in {option["run_id"] for option in run_options}:
        raise KeyError("run_id is not available for this date")
    chosen = next((value for value in audits if value["run_id"] == run_id), None) if run_id else audits[0] if audits else None
    if chosen:
        offline = chosen.get("mode") == "saved_data_read_only_reconstruction"
        raw_rows = chosen.get("articles_107", []) if offline else chosen.get("articles", [])
        origin = "offline" if offline else "audit"
        source_rows = chosen.get("sources") if isinstance(chosen.get("sources"), list) else None
        if source_rows is None and latest:
            source_rows = latest.get("source_health", [])
            source_provenance = "latest-run_same_date_not_run_identified"
        else:
            source_provenance = "selected_run_audit" if source_rows is not None else UNAVAILABLE
    elif latest:
        raw_rows, origin = latest.get("articles", []), "latest-run"
        source_rows, source_provenance = latest.get("source_health", []), "latest-run"
    else:
        raw_rows, origin, source_rows, source_provenance = [], "none", [], UNAVAILABLE
    if not isinstance(raw_rows, list):
        raw_rows = []
    source_status = {row["source_id"]: row for row in source_rows
                     if isinstance(row, dict) and isinstance(row.get("source_id"), str)}
    ids = [row.get("article_id") or row.get("id") or row.get("trace_id") for row in raw_rows if isinstance(row, dict)]
    sqlite_rows = _sqlite_metadata(root, [value for value in ids if isinstance(value, str)])
    articles = [_article(row, sqlite_rows.get(row.get("article_id") or row.get("id") or row.get("trace_id")), origin)
                for row in raw_rows if isinstance(row, dict)]
    articles.sort(key=lambda row: (_time_key(row["published_at"]), row["article_id"]), reverse=True)
    groups = {source.source_id: [] for source in current_sources}
    unassigned = []
    for article in articles:
        sid = article["source_id"]
        article["source_mapping_basis"] = "recorded" if sid in groups else UNAVAILABLE
        if sid in groups:
            groups[sid].append(article)
        else:
            unassigned.append(article)
    sources = []
    for source in current_sources:
        status = source_status.get(source.source_id)
        source_articles = groups[source.source_id]
        sources.append({"source_id": source.source_id, "name": source.name, "region": source.region,
                        "tier": source.tier, "language": source.language, "source_role": source.source_role,
                        "status": status.get("status", UNAVAILABLE) if status else UNAVAILABLE,
                        "article_count": status.get("items", UNAVAILABLE) if status else UNAVAILABLE,
                        "visible_article_count": len(source_articles), "articles": source_articles})
    if unassigned:
        sources.append({"source_id": "unassigned", "name": "Unassigned / 来源无法确定", "region": UNAVAILABLE,
                        "tier": UNAVAILABLE, "language": UNAVAILABLE, "source_role": UNAVAILABLE,
                        "status": UNAVAILABLE, "article_count": UNAVAILABLE,
                        "visible_article_count": len(unassigned), "articles": unassigned})
    return {"date": requested_date, "run_id": chosen["run_id"] if chosen else run_options[0]["run_id"] if run_options else UNAVAILABLE,
            "runs": run_options, "source_count": len(current_sources), "source_status_provenance": source_provenance,
            "article_provenance": origin, "articles": articles, "visible_article_count": len(articles),
            "sources": sources, "notice": (
                "Historical audit only saved collection-approved articles; earlier parser and filter outcomes are unavailable."
                if origin == "offline" else
                "This date has only a collection snapshot; downstream trace fields are unavailable."
                if origin == "latest-run" else
                "No saved run exists for this date; source status and article trace are unavailable."
                if origin == "none" else
                "Counts represent parsed source items; trace rows can also include index entries rejected before parsing.")}
