from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .extract import extract_article
from .models import Article, SourceItem
from .sources import TECH_TERMS, BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS, SourceDefinition, collect_source, fetch, load_sources
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


def _in_window(published_at: datetime, now: datetime, hours: int) -> bool:
    return now - timedelta(hours=hours) <= published_at <= now + timedelta(minutes=10)


def _language(text: str) -> str:
    han = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return "zh" if han >= 20 and han * 5 >= latin else "en"


def _collapse_release_bursts(articles: list[Article]) -> list[Article]:
    """Keep the newest release from a same-day SDK/repository version burst."""
    selected: dict[tuple[str, str, str], Article] = {}
    others: list[Article] = []
    for article in articles:
        if article.source_type != "official_changelog":
            others.append(article)
            continue
        key = (article.source, article.published_at[:10], "release")
        current = selected.get(key)
        if current is None or article.published_at > current.published_at:
            selected[key] = article
    return sorted(others + list(selected.values()), key=lambda article: article.published_at, reverse=True)


def _candidate_to_article(item: SourceItem, now: datetime, source: SourceDefinition) -> Article | None:
    page = fetch(item.url, allow_hosts=source.allow_hosts)
    extracted = extract_article(page, item.title, source.cleaning)
    published = extracted.published_at or item.published_at
    if not published or not extracted.clean_text or len(extracted.clean_text) < 120:
        return None
    title = extracted.title or item.title
    if BLOCKED_CONTENT_TERMS.search(title + " " + extracted.clean_text[:3_000]):
        return None
    # Feed indexes can contain generic company posts; retain only actual technology candidates.
    if not TECH_TERMS.search(title + " " + extracted.clean_text[:3_000]):
        return None
    published = published.astimezone(UTC)
    body_fingerprint = fingerprint(title, extracted.clean_text)
    return Article(
        id=article_id(item.url), category=item.categories[0], title=title, original_title=title,
        source=item.source, source_type=item.source_type, published_at=published.isoformat(),
        original_url=item.url, language=_language(extracted.clean_text), raw_text=extracted.raw_text,
        clean_text=extracted.clean_text, fingerprint=body_fingerprint,
        created_at=now.astimezone(UTC).isoformat(), verification_status="source_verified",
        source_region=item.region, source_tier=item.tier,
    )


def check_sources(root: Path) -> list[dict[str, object]]:
    """Check every source index independently without writing articles or cache."""
    results: list[dict[str, object]] = []
    for source in load_sources(root / "config" / "sources.json"):
        try:
            collection = collect_source(source)
            status = collection.status
            if collection.items:
                sample = collection.items[0]
                extracted = extract_article(fetch(sample.url, allow_hosts=source.allow_hosts), sample.title, source.cleaning)
                if len(extracted.clean_text) < 120:
                    status = "degraded"
            results.append({"source_id": source.source_id, "status": status, "items": len(collection.items),
                            "region": source.region, "tier": source.tier, "configured_health": source.health_status,
                            "checked_at": datetime.now(UTC).isoformat()})
        except Exception as exc:
            results.append({"source_id": source.source_id, "status": "error", "items": 0,
                            "checked_at": datetime.now(UTC).isoformat(), "error": f"{type(exc).__name__}: {exc}"})
    return results


def run_collection(root: Path, dry_run: bool, now: datetime | None = None, minimum: int = 8, maximum: int = 15) -> RunResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    sources: list[SourceDefinition] = load_sources(root / "config" / "sources.json")
    sources_by_id = {source.source_id: source for source in sources}
    errors: list[str] = []
    source_items: list[SourceItem] = []
    source_health: list[dict[str, object]] = []
    cache = ArticleStore(root / "data" / "ai_daily.sqlite3") if not dry_run else None
    try:
        for source in sources:
            try:
                result = collect_source(source, cache)
                source_items.extend(result.items)
                source_health.append({"source_id": source.source_id, "status": result.status,
                                      "region": source.region, "tier": source.tier, "configured_health": source.health_status,
                                      "items": len(result.items), "not_modified": result.not_modified,
                                      "used_conditional_request": result.used_conditional_request,
                                      "checked_at": now.isoformat()})
            except Exception as exc:  # An unavailable source must not invent replacements.
                message = f"{source.source_id}: {type(exc).__name__}: {exc}"
                errors.append(message)
                source_health.append({"source_id": source.source_id, "status": "error", "items": 0,
                                      "not_modified": False, "used_conditional_request": False,
                                      "checked_at": now.isoformat(), "error": message})
    finally:
        if cache:
            cache.close()
    unique_items = {item.url: item for item in source_items}
    ordered = sorted(
        unique_items.values(),
        key=lambda item: (-(item.published_at.timestamp() if item.published_at else 0), item.priority),
    )
    articles: list[Article] = []
    window_hours = 24
    for candidate_window in (24, 72):
        # Feed timestamps let us discard stale entries before downloading article pages.
        eligible_by_source: dict[str, list[SourceItem]] = {}
        for item in ordered:
            if item.published_at is not None and not _in_window(item.published_at, now, candidate_window):
                continue
            bucket = eligible_by_source.setdefault(item.source_id, [])
            # HTML indexes mix article cards with navigation; scan a bounded set of recent links.
            if len(bucket) < 8:
                bucket.append(item)
        eligible = [item for bucket in eligible_by_source.values() for item in bucket]

        def normalize(item: SourceItem) -> tuple[SourceItem, Article | None, str | None]:
            try:
                return item, _candidate_to_article(item, now, sources_by_id[item.source_id]), None
            except Exception as exc:
                return item, None, f"{item.url}: {type(exc).__name__}: {exc}"

        articles = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            for _, article, error in executor.map(normalize, eligible):
                if error:
                    errors.append(error)
                elif article and _in_window(datetime.fromisoformat(article.published_at), now, candidate_window):
                    articles.append(article)
        articles = _collapse_release_bursts(articles)[:maximum]
        window_hours = candidate_window
        if len(articles) >= minimum or candidate_window == 72:
            break
    inserted = 0
    if not dry_run:
        store = ArticleStore(root / "data" / "ai_daily.sqlite3")
        try:
            store.purge_articles_for_hosts(BLOCKED_CONTENT_HOSTS)
            for article in articles:
                inserted += int(store.add(article))
        finally:
            store.close()
    result = RunResult(len(source_items), len(articles), inserted, window_hours, tuple(errors), tuple(articles), tuple(source_health))
    output = {
        "fetched_source_items": result.fetched_source_items, "accepted": result.accepted,
        "inserted": result.inserted, "time_window_hours": result.time_window_hours,
        "errors": list(result.errors), "articles": [article.to_dict() for article in result.articles],
        "source_health": list(result.source_health),
    }
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "latest-run.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
