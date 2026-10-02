from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .extract import extract_article
from .diagnostics import failure_details, mark_failure
from .models import Article, SourceItem
from .sources import RADAR_TERMS, BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS, SourceDefinition, classify_radar, collect_source, fetch, load_sources
from .store import ArticleStore
from .text import article_id, fingerprint


@dataclass(frozen=True)
class RunResult:
    fetched_source_items: int
    accepted: int
    inserted: int
    time_window_hours: int
    errors: tuple[str, ...]
    articles: tuple[Article, ...]
    source_health: tuple[dict[str, object], ...] = ()
    source_errors: tuple[dict[str, object], ...] = ()
    normalization_errors: tuple[dict[str, object], ...] = ()


def _in_window(published_at: datetime, now: datetime, hours: int) -> bool:
    return now - timedelta(hours=hours) <= published_at <= now + timedelta(minutes=10)


def _language(text: str) -> str:
    han = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return "zh" if han >= 20 and han * 5 >= latin else "en"


def _collapse_release_bursts(articles: list[Article]) -> list[Article]:
    """Collapse same-day patch bursts; distinct major/minor versions remain eligible."""
    selected: dict[tuple[str, str, str], Article] = {}
    others: list[Article] = []
    for article in articles:
        if article.source_type != "official_changelog":
            others.append(article)
            continue
        key = (article.source, article.published_at[:10], re.sub(r"(v?\d+\.\d+)\.\d+", r"\1", article.title.casefold()))
        current = selected.get(key)
        if current is None or article.published_at > current.published_at:
            selected[key] = article
    return sorted(others + list(selected.values()), key=lambda article: article.published_at, reverse=True)


def _candidate_to_article(item: SourceItem, now: datetime, source: SourceDefinition, audit=None) -> Article | None:
    if source.source_role == "discovery":
        if audit: audit.event(item, "normalization", "dropped", "discovery_source")
        return None
    page = fetch(item.url, **source.fetch_options())
    extracted = extract_article(page, item.title, source.cleaning)
    published = extracted.published_at or item.published_at
    if not published or not extracted.clean_text or len(extracted.clean_text) < 120:
        if audit:
            reason = "missing_publish_time" if not published else "missing_content" if not extracted.clean_text else "content_shorter_than_120"
            audit.event(item, "normalization", "dropped", reason, {"has_publish_time": bool(published), "text_length": len(extracted.clean_text or "")})
        return None
    title = extracted.title or item.title
    if BLOCKED_CONTENT_TERMS.search(title + " " + extracted.clean_text[:3_000]):
        if audit: audit.event(item, "normalization", "dropped", "blocked_content_terms")
        return None
    # Feed indexes can contain generic company posts; retain only actual technology candidates.
    if not RADAR_TERMS.search(title + " " + extracted.clean_text[:3_000]):
        if audit: audit.event(item, "normalization", "dropped", "radar_terms_absent")
        return None
    published = published.astimezone(UTC)
    body_fingerprint = fingerprint(title, extracted.clean_text)
    category, channel = classify_radar(title + " " + extracted.clean_text[:600], item.categories, item.channels)
    return Article(
        id=article_id(item.url), category=category, title=title, original_title=title,
        source=item.source, source_type=item.source_type, published_at=published.isoformat(),
        original_url=item.url, language=_language(extracted.clean_text), raw_text=extracted.raw_text,
        clean_text=extracted.clean_text, fingerprint=body_fingerprint,
        created_at=now.astimezone(UTC).isoformat(), verification_status="source_verified",
        source_region=item.region, source_tier=item.tier, source_role=item.source_role, channel=channel,
    )


def check_sources(root: Path) -> list[dict[str, object]]:
    """Check every source index independently without writing articles or cache."""
    results: list[dict[str, object]] = []
    for source in load_sources(root / "config" / "sources.json"):
        try:
            collection = collect_source(source)
            status = collection.status
            if collection.items and source.source_role != "discovery":
                sample = collection.items[0]
                extracted = extract_article(fetch(sample.url, **source.fetch_options()), sample.title, source.cleaning)
                if len(extracted.clean_text) < 120:
                    status = "degraded"
            results.append({"source_id": source.source_id, "status": status, "items": len(collection.items),
                            "region": source.region, "tier": source.tier, "source_role": source.source_role,
                            "channel": list(source.channels), "configured_health": source.health_status,
                            "checked_at": datetime.now(UTC).isoformat()})
        except Exception as exc:
            results.append({"source_id": source.source_id, "status": "error", "items": 0,
                            "checked_at": datetime.now(UTC).isoformat(), "error": f"{type(exc).__name__}: {exc}"})
    return results


def run_collection(root: Path, dry_run: bool, now: datetime | None = None, minimum: int = 5, maximum: int = 200, *, audit=None) -> RunResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    sources: list[SourceDefinition] = load_sources(root / "config" / "sources.json")
    sources_by_id = {source.source_id: source for source in sources}
    errors: list[str] = []
    source_items: list[SourceItem] = []
    source_health: list[dict[str, object]] = []
    source_errors: list[dict[str, object]] = []
    normalization_errors: list[dict[str, object]] = []
    cache = ArticleStore(root / "data" / "ai_daily.sqlite3") if not dry_run else None
    try:
        for source in sources:
            source_started = time.monotonic()
            try:
                result = collect_source(source, cache, on_decision=audit.parser_event) if audit else collect_source(source, cache)
                duration_ms = round((time.monotonic() - source_started) * 1000, 2)
                network_detail = {"duration_ms": duration_ms, "proxy_mode": source.proxy_mode,
                                  "request_history": list(result.request_history)}
                source_items.extend(result.items)
                if audit:
                    audit.source(source.source_id, result.status, len(result.items), {"fetch_method": source.fetch_method, **network_detail})
                    for item in result.items: audit.event(item, "collection", "kept", "parsed_source_item")
                latest_success = cache.record_source_health(source.source_id, result.status, now.isoformat(), len(result.items), duration_ms=duration_ms) if cache else (now.isoformat() if result.status == "ok" else None)
                source_health.append({"source_id": source.source_id, "status": result.status,
                                      **network_detail, **(cache.source_health_metrics(source.source_id) if cache else {}),
                                      "fetch_method": source.fetch_method,
                                      "region": source.region, "tier": source.tier, "source_role": source.source_role,
                                      "channel": list(source.channels), "configured_health": source.health_status,
                                      "items": len(result.items), "not_modified": result.not_modified,
                                      "used_conditional_request": result.used_conditional_request,
                                      "checked_at": now.isoformat(), "latest_success_at": latest_success})
            except Exception as exc:  # An unavailable source must not invent replacements.
                duration_ms = round((time.monotonic() - source_started) * 1000, 2)
                network_detail = {"duration_ms": duration_ms, "proxy_mode": source.proxy_mode,
                                  "request_history": list(getattr(exc, "source_request_history", ()))}
                message = f"{source.source_id}: {type(exc).__name__}: {exc}"
                errors.append(message)
                detail = failure_details(exc, "collection")
                if audit: audit.source(source.source_id, "error", 0, {
                    **network_detail, "fetch_method": source.fetch_method, "error_type": detail["error_type"],
                    "error_kind": detail["error_kind"], "http_status": detail["http_status"]})
                source_errors.append({"source_id": source.source_id, "fetch_method": source.fetch_method,
                                      **network_detail,
                                      "http_status": detail["http_status"], "error_type": detail["error_type"],
                                      "error_kind": detail["error_kind"], "error_summary": detail["error_summary"],
                                      "isolated": True})
                latest_success = cache.record_source_health(source.source_id, "error", now.isoformat(), 0, message,
                                                           duration_ms=duration_ms, error_kind=detail["error_kind"]) if cache else None
                source_health.append({"source_id": source.source_id, "status": "error", "items": 0,
                                      **network_detail, **(cache.source_health_metrics(source.source_id) if cache else {}),
                                      "error_kind": detail["error_kind"],
                                      "fetch_method": source.fetch_method,
                                      "region": source.region, "tier": source.tier, "source_role": source.source_role,
                                      "channel": list(source.channels), "configured_health": source.health_status,
                                      "not_modified": False, "used_conditional_request": False,
                                      "checked_at": now.isoformat(), "latest_success_at": latest_success, "error": message})
    finally:
        if cache:
            cache.close()
    unique_items = {item.url: item for item in source_items}
    if audit: audit.metrics["source_unique_urls"] = len(unique_items)
    if audit:
        for item in source_items:
            if unique_items[item.url] is not item:
                audit.event(item, "source_url_dedup", "dropped", "duplicate_url", {"duplicate_of": article_id(item.url)})
            else: audit.event(item, "source_url_dedup", "kept", "unique_url")
    ordered = sorted(
        unique_items.values(),
        key=lambda item: (-(item.published_at.timestamp() if item.published_at else 0), item.priority),
    )
    articles: list[Article] = []
    window_hours = 24
    for candidate_window in (168,):
        # Feed timestamps let us discard stale entries before downloading article pages.
        eligible_by_source: dict[str, list[SourceItem]] = {}
        for item in ordered:
            if item.published_at is not None and not _in_window(item.published_at, now, candidate_window):
                if audit: audit.event(item, "feed_freshness", "dropped", "future_publication_time" if item.published_at > now + timedelta(minutes=10) else "outside_168h_feed_window", {"published_at": item.published_at.isoformat()})
                continue
            bucket = eligible_by_source.setdefault(item.source_id, [])
            # HTML indexes mix article cards with navigation; scan a bounded set of recent links.
            if len(bucket) < 20:
                bucket.append(item)
                if audit: audit.event(item, "source_cap", "kept", "within_20_per_source")
            elif audit: audit.event(item, "source_cap", "dropped", "per_source_scan_cap_20")
        eligible = [item for bucket in eligible_by_source.values() for item in bucket]
        if audit: audit.metrics["feed_freshness_and_source_cap_passed"] = len(eligible)

        def normalize(item: SourceItem) -> tuple[SourceItem, Article | None, str | None, dict[str, object] | None]:
            try:
                return item, _candidate_to_article(item, now, sources_by_id[item.source_id], audit), None, None
            except Exception as exc:
                return item, None, f"{item.url}: {type(exc).__name__}: {exc}", failure_details(exc, "normalization")

        articles = []
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                for item, article, error, detail in executor.map(normalize, eligible):
                    if error:
                        errors.append(error)
                        if audit: audit.event(item, "normalization", "dropped", "normalization_exception", {"error_kind": detail["error_kind"], "http_status": detail["http_status"]})
                        normalization_errors.append({"source_id": item.source_id, "article_id": article_id(item.url),
                                                     "fetch_method": sources_by_id[item.source_id].fetch_method,
                                                     "error_type": detail["error_type"], "error_summary": detail["error_summary"],
                                                     "error_kind": detail["error_kind"], "http_status": detail["http_status"],
                                                     "isolated": True})
                    elif article:
                        if audit: audit.event(article, "normalization", "kept", "extracted_content")
                        if _in_window(datetime.fromisoformat(article.published_at), now, candidate_window):
                            if audit: audit.event(article, "extracted_freshness", "kept", "within_168h_extracted_window")
                            articles.append(article)
                        elif audit: audit.event(article, "extracted_freshness", "dropped", "future_publication_time" if datetime.fromisoformat(article.published_at) > now + timedelta(minutes=10) else "outside_168h_extracted_window", {"published_at": article.published_at})
            before_burst = articles
            if audit: audit.metrics["freshness_passed"] = len(before_burst)
            collapsed = _collapse_release_bursts(articles)
            articles = collapsed[:maximum]
            if audit: audit.metrics["dedup_passed"] = len(collapsed)
            if audit:
                collapsed_ids = {a.id for a in collapsed}
                retained_ids = {a.id for a in articles}
                for a in before_burst:
                    audit.event(a, "release_burst_dedup", "kept" if a.id in collapsed_ids else "dropped",
                                "distinct_release" if a.id in collapsed_ids else "same_day_patch_burst")
                for a in collapsed:
                    audit.event(a, "collection_cap", "kept" if a.id in retained_ids else "dropped",
                                "within_maximum" if a.id in retained_ids else "maximum_200")
        except Exception as exc:
            raise mark_failure(exc, "normalization")
        window_hours = candidate_window
        if len(articles) >= minimum or candidate_window == 168:
            break
    inserted = 0
    inserted_ids: list[str] = []
    if not dry_run:
        store = ArticleStore(root / "data" / "ai_daily.sqlite3")
        try:
            store.purge_articles_for_hosts(BLOCKED_CONTENT_HOSTS)
            for article in articles:
                if audit:
                    existing = store.connection.execute(
                        "SELECT id,original_url FROM articles WHERE original_url = ? OR fingerprint = ? LIMIT 1",
                        (article.original_url, article.fingerprint)).fetchone()
                is_new = store.add(article)
                inserted += int(is_new)
                if is_new:
                    inserted_ids.append(article.id)
                if audit:
                    if is_new:
                        audit.event(article, "db_insert", "kept", "inserted")
                    else:
                        audit.event(article, "db_insert", "dropped",
                                    "duplicate_url" if existing and existing[1] == article.original_url else "duplicate_fingerprint",
                                    {"duplicate_of": existing[0] if existing else None,
                                     "existing_article_remains_eligible": True})
        finally:
            store.close()
    if audit:
        audit.collection_sets(source_items, articles, inserted_ids)
        audit.metrics.update({"raw_articles": audit.metrics.get("raw_index_entries_seen", len(source_items)), "parsed_source_items": len(source_items), "normalized_articles": sum(1 for t in audit.traces.values() if any(e["stage"] == "normalization" and e["status"] == "kept" for e in t["events"])), "collection_accepted": len(articles), "collection_inserted": inserted})
    result = RunResult(len(source_items), len(articles), inserted, window_hours, tuple(errors), tuple(articles),
                       tuple(source_health), tuple(source_errors), tuple(normalization_errors))
    output = {
        "fetched_source_items": result.fetched_source_items, "accepted": result.accepted,
        "inserted": result.inserted, "time_window_hours": result.time_window_hours,
        "errors": list(result.errors), "articles": [article.to_dict() for article in result.articles],
        "source_health": list(result.source_health),
        "source_errors": list(result.source_errors), "normalization_errors": list(result.normalization_errors),
    }
    data_dir = root / "data"
    try:
        data_dir.mkdir(exist_ok=True)
        (data_dir / "latest-run.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise mark_failure(exc, "persistence")
    return result
