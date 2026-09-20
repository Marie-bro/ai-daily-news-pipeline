from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .deepseek import DeepSeekClient, DeepSeekError
from .bilingual import validate_literal_consistency
from .models import Article, Enrichment
from .sources import BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS
from .store import ArticleStore

TASK_NAME = "phase5_5_bilingual_tech_daily"
TECH_CATEGORIES = {"ai", "chips", "consumer_tech", "software", "robotics", "mobility", "space", "science", "internet", "other_tech"}

# This prompt is deliberately fixed and always placed first so repeated runs can use DeepSeek context caching.
NEWS_SYSTEM_PROMPT = """You prepare a concise bilingual Tech Daily from verified source candidates.
Use only facts contained in each supplied candidate. Never invent an event, date, source, URL, quote, product capability, metric, or certainty. Do not use outside knowledge.
The supplied id, source, published_at, original_url, and title_original are source metadata. Copy them exactly.
For every candidate, return one item. category must be exactly one of: ai, chips, consumer_tech, software, robotics, mobility, space, science, internet, other_tech. Produce paired fields in this fixed order: title_en and title_cn; what_happened_en and what_happened; why_it_matters_en and why_it_matters. English must be natural and appear first; Chinese must be natural and appear second. Each English/Chinese pair must state exactly the same facts. Keep numbers, dates, versions, product names, technical names, and certainty identical between the pair. title_cn, what_happened, and why_it_matters are the Chinese fields. importance_score is an integer from 0 to 100. Do not generate original-body excerpts, key-point arrays, full translations, study expressions, relevance fields, or additional summaries.
Prefer high-value first-party facts. Tier 4 candidates are discovery clues only and must receive importance_score 0 unless the supplied text itself identifies a traceable primary source. Keep each text field short.
Return only valid JSON, with no Markdown, using exactly this outer shape: {"items":[{"id":"","category":"","title_en":"","title_cn":"","title_original":"","source":"","published_at":"","original_url":"","what_happened_en":"","what_happened":"","why_it_matters_en":"","why_it_matters":"","importance_score":0}]}.
"""


class EnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnrichmentResult:
    candidates: int
    saved: int
    model: str | None
    usage: dict[str, object]
    output_path: Path | None


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise EnrichmentError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise EnrichmentError(f"{name} must be greater than zero")
    return parsed


def _candidate_payload(articles: list[Article], per_article_characters: int) -> str:
    candidates = [
        {
            "id": article.id,
            "title_original": article.original_title,
            "source": article.source,
            "published_at": article.published_at,
            "original_url": article.original_url,
            "language": article.language,
            "source_region": article.source_region,
            "source_tier": article.source_tier,
            "clean_text": article.clean_text[:per_article_characters],
        }
        for article in articles
    ]
    return "Verified candidates (process every one):\n" + json.dumps({"candidates": candidates}, ensure_ascii=False)


def _extract_json(content: str) -> dict[str, object]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise EnrichmentError("DeepSeek did not return valid JSON; no enrichment was saved") from exc
    if not isinstance(parsed, dict):
        raise EnrichmentError("DeepSeek JSON root must be an object")
    return parsed


def _string(value: object, field: str, article_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnrichmentError(f"Item {article_id} has an empty {field}")
    return value.strip()


def _validated_enrichments(payload: dict[str, object], articles: list[Article], model: str, generated_at: str) -> list[Enrichment]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(articles):
        raise EnrichmentError("DeepSeek must return exactly one item for every candidate")
    by_id: dict[str, dict[str, object]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or not isinstance(raw_item.get("id"), str):
            raise EnrichmentError("Every DeepSeek item must have an id")
        item_id = raw_item["id"]
        if item_id in by_id:
            raise EnrichmentError(f"DeepSeek returned duplicate item id {item_id}")
        by_id[item_id] = raw_item
    expected_ids = {article.id for article in articles}
    if set(by_id) != expected_ids:
        raise EnrichmentError("DeepSeek item ids do not match the verified candidates")

    enrichments: list[Enrichment] = []
    for article in articles:
        item = by_id[article.id]
        category = _string(item.get("category"), "category", article.id)
        if category not in TECH_CATEGORIES:
            raise EnrichmentError(f"Item {article.id} has an invalid category")
        score = item.get("importance_score")
        if type(score) is not int or not 0 <= score <= 100:
            raise EnrichmentError(f"Item {article.id} has an invalid importance_score")
        # Source metadata is always taken from the stored, verified article rather than trusting model output.
        enrichment = Enrichment(
            article_id=article.id, task=TASK_NAME, generated_at=generated_at, model=model,
            title_cn=_string(item.get("title_cn"), "title_cn", article.id),
            title_en=_string(item.get("title_en"), "title_en", article.id),
            title_original=article.original_title, source=article.source, published_at=article.published_at,
            original_url=article.original_url, category=category, original_language=article.language,
            what_happened=_string(item.get("what_happened"), "what_happened", article.id),
            what_happened_en=_string(item.get("what_happened_en"), "what_happened_en", article.id),
            why_it_matters=_string(item.get("why_it_matters"), "why_it_matters", article.id),
            why_it_matters_en=_string(item.get("why_it_matters_en"), "why_it_matters_en", article.id),
            importance_score=score,
        )
        try:
            validate_literal_consistency(enrichment.title_en, enrichment.title_cn, "title")
            validate_literal_consistency(enrichment.what_happened_en, enrichment.what_happened, "what_happened")
            validate_literal_consistency(enrichment.why_it_matters_en, enrichment.why_it_matters, "why_it_matters")
        except ValueError as exc:
            raise EnrichmentError(f"Item {article.id} has inconsistent bilingual literals: {exc}") from exc
        if BLOCKED_CONTENT_TERMS.search(json.dumps(enrichment.to_dict(), ensure_ascii=False)):
            raise EnrichmentError(f"Item {article.id} contains blocked content")
        enrichments.append(enrichment)
    return sorted(enrichments, key=lambda value: value.importance_score, reverse=True)


def _eligible_content(article: Article) -> bool:
    parsed = urlparse(article.original_url)
    host = (parsed.hostname or "").lower()
    return (article.verification_status == "source_verified" and article.category in TECH_CATEGORIES
            and article.source_tier <= 3
            and parsed.scheme == "https" and bool(host)
            and not any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_CONTENT_HOSTS)
            and not BLOCKED_CONTENT_TERMS.search(article.title + " " + article.clean_text[:3_000]))


def _dedupe_events(items: list[Enrichment]) -> list[Enrichment]:
    """Keep one highest-value rendering when multiple sources describe the same event."""
    selected: list[Enrichment] = []
    for item in sorted(items, key=lambda value: value.importance_score, reverse=True):
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title_cn.casefold())
        if any(SequenceMatcher(None, normalized, re.sub(r"[^\w\u4e00-\u9fff]", "", other.title_cn.casefold())).ratio() >= .78
               for other in selected):
            continue
        selected.append(item)
    return selected


def run_enrichment(root: Path, *, dry_run: bool = False, now: datetime | None = None) -> EnrichmentResult:
    max_articles = _positive_int("MAX_BATCH_ARTICLES", 10)
    if max_articles > 10:
        raise EnrichmentError("MAX_BATCH_ARTICLES must be at most 10 for one daily digest")
    character_limit = _positive_int("MAX_NEWS_INPUT_CHARS_PER_ARTICLE", 4_000)
    max_output_tokens = _positive_int("MAX_NEWS_OUTPUT_TOKENS", 1_800)
    max_daily_tokens = _positive_int("MAX_DAILY_TOKENS", 40_000)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    data_dir = root / "data"
    output_path = data_dir / "latest-enrichment.json"
    store = ArticleStore(data_dir / "ai_daily.sqlite3")
    try:
        candidates = store.unenriched_articles(
            TASK_NAME, max_articles * 4,
            published_after=(now - timedelta(hours=72)).isoformat(),
            published_before=(now + timedelta(minutes=10)).isoformat(),
        )
        articles = [article for article in candidates if _eligible_content(article)][:max_articles]
        if not articles:
            return EnrichmentResult(0, 0, None, {}, None)
        prompt = _candidate_payload(articles, character_limit)
        if dry_run:
            preview_path = data_dir / "enrichment-preview.json"
            preview_path.write_text(json.dumps({"candidates": len(articles), "prompt_characters": len(prompt), "dry_run": True}, indent=2), encoding="utf-8")
            return EnrichmentResult(len(articles), 0, None, {}, preview_path)
        today_total = store.daily_total_tokens(now.date().isoformat())
        if today_total >= max_daily_tokens:
            raise EnrichmentError(f"MAX_DAILY_TOKENS reached: {today_total}/{max_daily_tokens} actual tokens already recorded today")
        conservative_estimate = len(NEWS_SYSTEM_PROMPT) + len(prompt) + max_output_tokens
        if today_total + conservative_estimate > max_daily_tokens:
            raise EnrichmentError(
                f"Daily token guard: {today_total} recorded + {conservative_estimate} conservative estimate exceeds {max_daily_tokens}"
            )
        try:
            content, usage, model = DeepSeekClient().complete_json(
                system_prompt=NEWS_SYSTEM_PROMPT, user_prompt=prompt, max_tokens=max_output_tokens,
            )
        except DeepSeekError as exc:
            if exc.usage is not None:
                store.record_usage(task=f"{TASK_NAME}_rejected", model=exc.model or "unknown",
                                   created_at=now.isoformat(), usage=exc.usage)
                store.commit()
            raise
        try:
            enrichments = _validated_enrichments(_extract_json(content), articles, model, now.isoformat())
        except EnrichmentError:
            # Even an unusable model response has consumed real API tokens. Preserve that fact without saving its content.
            store.record_usage(task=f"{TASK_NAME}_rejected", model=model, created_at=now.isoformat(), usage=usage)
            store.commit()
            raise
        for enrichment in enrichments:
            store.save_enrichment(enrichment)
        store.record_usage(task=TASK_NAME, model=model, created_at=now.isoformat(), usage=usage)
        store.commit()
        publishable = _dedupe_events([item for item in enrichments if item.importance_score >= 60])[:10]
        if not publishable:
            return EnrichmentResult(len(articles), 0, model, usage, None)
        output_path.write_text(json.dumps({
            "schema_version": 3, "generated_at": now.isoformat(), "task": TASK_NAME, "model": model, "usage": usage,
            "items": [enrichment.to_dict() for enrichment in publishable],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return EnrichmentResult(len(articles), len(publishable), model, usage, output_path)
    finally:
        store.close()
