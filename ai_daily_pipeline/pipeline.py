from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .extract import extract_article
from .models import Article, SourceItem
from .sources import AI_TERMS, SourceDefinition, collect_source_items, fetch, load_sources
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


def _in_window(published_at: datetime, now: datetime, hours: int) -> bool:
    return now - timedelta(hours=hours) <= published_at <= now + timedelta(minutes=10)


def _language(text: str) -> str:
    return "zh" if any("\u4e00" <= char <= "\u9fff" for char in text) else "en"


def _collapse_release_bursts(articles: list[Article]) -> list[Article]:
    """Keep the newest release from a same-day SDK/repository version burst."""
    selected: dict[tuple[str, str, str], Article] = {}
    others: list[Article] = []
    for article in articles:
        if article.source_type != "github_release":
            others.append(article)
            continue
        key = (article.source, article.published_at[:10], "release")
        current = selected.get(key)
        if current is None or article.published_at > current.published_at:
            selected[key] = article
    return sorted(others + list(selected.values()), key=lambda article: article.published_at, reverse=True)


def _candidate_to_article(item: SourceItem, now: datetime) -> Article | None:
    page = fetch(item.url)
    extracted = extract_article(page, item.title)
    published = extracted.published_at or item.published_at
    if not published or not extracted.clean_text or len(extracted.clean_text) < 120:
        return None
    title = extracted.title or item.title
    # Feed indexes can contain generic company posts; retain only actual AI-relevant candidates.
    if not AI_TERMS.search(title + " " + extracted.clean_text[:3_000]):
        return None
    published = published.astimezone(UTC)
    body_fingerprint = fingerprint(title, extracted.clean_text)
    return Article(
        id=article_id(item.url), category="ai", title=title, original_title=title,
        source=item.source, source_type=item.source_type, published_at=published.isoformat(),
        original_url=item.url, language=_language(extracted.clean_text), raw_text=extracted.raw_text,
        clean_text=extracted.clean_text, fingerprint=body_fingerprint,
        created_at=now.astimezone(UTC).isoformat(), verification_status="source_verified",
    )


def run_collection(root: Path, dry_run: bool, now: datetime | None = None, minimum: int = 8, maximum: int = 15) -> RunResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    sources: list[SourceDefinition] = load_sources(root / "config" / "sources.json")
    errors: list[str] = []
    source_items: list[SourceItem] = []
    for source in sources:
        try:
            source_items.extend(collect_source_items(source))
        except Exception as exc:  # Individual sources must not make us invent replacements.
            errors.append(f"{source.source_id}: {type(exc).__name__}: {exc}")
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
            # Two newest links per source preserve source diversity and cap network work.
            if len(bucket) < 2:
                bucket.append(item)
        eligible = [item for bucket in eligible_by_source.values() for item in bucket]

        def normalize(item: SourceItem) -> tuple[SourceItem, Article | None, str | None]:
            try:
                return item, _candidate_to_article(item, now), None
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
            for article in articles:
                inserted += int(store.add(article))
        finally:
            store.close()
    result = RunResult(len(source_items), len(articles), inserted, window_hours, tuple(errors), tuple(articles))
    output = {
        "fetched_source_items": result.fetched_source_items, "accepted": result.accepted,
        "inserted": result.inserted, "time_window_hours": result.time_window_hours,
        "errors": list(result.errors), "articles": [article.to_dict() for article in result.articles],
    }
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "latest-run.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
