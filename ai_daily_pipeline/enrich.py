from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .deepseek import DeepSeekClient
from .models import Article, Enrichment
from .store import ArticleStore

TASK_NAME = "phase4_bilingual_daily"

# This prompt is deliberately fixed and always placed first so repeated runs can use DeepSeek context caching.
NEWS_SYSTEM_PROMPT = """You prepare a bilingual AI Daily from verified source candidates.
Use only facts contained in each supplied candidate. Never invent an event, date, source, URL, quote, product capability, metric, or certainty. Do not use outside knowledge.
The supplied id, source, published_at, original_url, and title_original are source metadata. Copy them exactly.
For every candidate, return one item. key_points_original must be 2 to 4 concise points faithful to the original-language source text. translation must be a Chinese translation of the matching key point in the same array position. Keep Original Key Points, Chinese Translation, AI Summary, and English Summary separate.
For an English candidate, useful_expressions must contain exactly 2 useful English expressions from its supplied text. For a Chinese candidate, include exactly 2 accurate English study expressions only when supported by its translated summary; otherwise return an empty array.
Return only valid JSON, with no Markdown, using exactly this outer shape: {"items":[{"id":"","title_cn":"","title_original":"","source":"","published_at":"","original_url":"","key_points_original":[],"translation":[],"summary_cn":"","summary_en":"","relevance":"","useful_expressions":[]}]}.
"""


class EnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnrichmentResult:
    candidates: int
    saved: int
    model: str | None
    usage: dict[str, object]
    output_path: Path


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


def _strings(value: object, field: str, article_id: str, *, minimum: int, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise EnrichmentError(f"Item {article_id} must contain {minimum} to {maximum} {field}")
    values = tuple(_string(entry, field, article_id) for entry in value)
    return values


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
        points = _strings(item.get("key_points_original"), "key_points_original", article.id, minimum=2, maximum=4)
        translations = _strings(item.get("translation"), "translation", article.id, minimum=2, maximum=4)
        if len(points) != len(translations):
            raise EnrichmentError(f"Item {article.id} has unmatched key points and translations")
        expressions_raw = item.get("useful_expressions")
        if not isinstance(expressions_raw, list) or len(expressions_raw) > 4:
            raise EnrichmentError(f"Item {article.id} has invalid useful_expressions")
        expressions = tuple(_string(value, "useful_expressions", article.id) for value in expressions_raw)
        if article.language == "en" and len(expressions) < 2:
            raise EnrichmentError(f"English item {article.id} needs 2 to 4 useful expressions")
        # Source metadata is always taken from the stored, verified article rather than trusting model output.
        enrichments.append(Enrichment(
            article_id=article.id, task=TASK_NAME, generated_at=generated_at, model=model,
            title_cn=_string(item.get("title_cn"), "title_cn", article.id),
            title_original=article.original_title, source=article.source, published_at=article.published_at,
            original_url=article.original_url, key_points_original=points, translation=translations,
            summary_cn=_string(item.get("summary_cn"), "summary_cn", article.id),
            summary_en=_string(item.get("summary_en"), "summary_en", article.id),
            relevance=_string(item.get("relevance"), "relevance", article.id), useful_expressions=expressions,
        ))
    return enrichments


def run_enrichment(root: Path, *, dry_run: bool = False) -> EnrichmentResult:
    max_articles = _positive_int("MAX_BATCH_ARTICLES", 8)
    character_limit = _positive_int("MAX_NEWS_INPUT_CHARS_PER_ARTICLE", 4_000)
    max_output_tokens = _positive_int("MAX_NEWS_OUTPUT_TOKENS", 3_500)
    max_daily_tokens = _positive_int("MAX_DAILY_TOKENS", 40_000)
    now = datetime.now(UTC)
    data_dir = root / "data"
    output_path = data_dir / "latest-enrichment.json"
    store = ArticleStore(data_dir / "ai_daily.sqlite3")
    try:
        articles = store.unenriched_articles(TASK_NAME, max_articles)
        prompt = _candidate_payload(articles, character_limit)
        if not articles:
            raise EnrichmentError("No unenriched verified candidates are available. Run collection first.")
        if dry_run:
            output_path.write_text(json.dumps({"candidates": len(articles), "prompt_characters": len(prompt), "dry_run": True}, indent=2), encoding="utf-8")
            return EnrichmentResult(len(articles), 0, None, {}, output_path)
        today_total = store.daily_total_tokens(now.date().isoformat())
        if today_total >= max_daily_tokens:
            raise EnrichmentError(f"MAX_DAILY_TOKENS reached: {today_total}/{max_daily_tokens} actual tokens already recorded today")
        content, usage, model = DeepSeekClient().complete_json(
            system_prompt=NEWS_SYSTEM_PROMPT, user_prompt=prompt, max_tokens=max_output_tokens,
        )
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
        output_path.write_text(json.dumps({
            "generated_at": now.isoformat(), "task": TASK_NAME, "model": model, "usage": usage,
            "items": [enrichment.to_dict() for enrichment in enrichments],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return EnrichmentResult(len(articles), len(enrichments), model, usage, output_path)
    finally:
        store.close()
