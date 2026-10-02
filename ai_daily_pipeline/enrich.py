from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

from .deepseek import DeepSeekClient, DeepSeekError
from .diagnostics import failure_details, failure_stage, mark_failure
from .bilingual import BilingualValidationError, normalize_fact_schema, validate_semantic_consistency
from .models import Article, Enrichment
from .sources import BLOCKED_CONTENT_HOSTS, BLOCKED_CONTENT_TERMS, TECH_CATEGORIES
from .store import ArticleStore
from .supply import policy, select, history, same_event, deep_read

TASK_NAME = "phase5_5_bilingual_tech_daily"

# This prompt is deliberately fixed and always placed first so repeated runs can use DeepSeek context caching.
NEWS_SYSTEM_PROMPT = """You prepare a concise bilingual MarieSpace Radar from verified source candidates covering technology, industry, policy, economy, social trends, future opportunities, and deep reads.
Use only facts contained in each supplied candidate. Never invent an event, date, source, URL, quote, product capability, metric, certainty, or background fact. Do not use outside knowledge. The supplied id, source, published_at, original_url, and title_original are source metadata; copy them exactly when requested.

For every candidate, create one compact shared fact_schema first, then render English and Chinese from that exact schema. The two rendered languages may use natural phrasing, but neither may add a fact the fact_schema does not contain. fact_schema.article_id must equal id and fact_schema.category must equal category. fact_schema.core_facts must contain 1–4 material facts only. Each fact must contain: id, type, value, rendered_in (one or more of title, what_happened, why_it_matters), english_forms, and chinese_forms. Use one short form per language unless a second form is required for a natural translation. Include only hard facts actually rendered: companies, institutions, products, technologies, dates, numbers, versions, support/availability, scope, limitations, and release intent. MIT / Massachusetts Institute of Technology and 麻省理工学院 may be forms of one fact; 1.2 billion and 12 亿 may be forms of one number fact. Do not force word-for-word translation.

Also provide the compact schema fields key_entities, dates, numbers, versions, products, companies, technologies, scope, limitations, and importance_reasons. Keep each list to at most three short entries; scope must be one short clause; limitations and importance_reasons must have at most one entry each. These fields describe only verified source material. category must be exactly one of: ai, chips, consumer_tech, software, robotics, mobility, space, science, internet, other_tech, policy, economy, industry, education, employment, society, infrastructure, opportunities. Produce title_en then title_cn; what_happened_en then what_happened; why_it_matters_en then why_it_matters. English is first and Chinese is second. title_cn, what_happened, and why_it_matters are Chinese. For policy items, state the measure, affected groups, implementation timing and scope when present, then explain concrete effects on industry, education, employment, skills or future opportunities using only supplied facts. importance_score is an integer from 0 to 100. Do not generate original-body excerpts, key-point arrays, full translations, study expressions, relevance fields, or additional summaries.

Prefer high-value first-party facts. Tier 4 candidates are discovery clues only and must receive importance_score 0 unless the supplied text itself identifies a traceable primary source. Importance must reflect global significance, evidence, novelty, economic or social impact, and consequences for learning, careers and future choices, never personal AI interests. Ordinary entertainment or low-value trending topics are ineligible. Evaluate tutorials/research/analysis by lasting learning and practical value. Keep each text field short.
Return only valid JSON, with no Markdown, using exactly this outer shape: {"items":[{"id":"","category":"","fact_schema":{"article_id":"","category":"","core_facts":[{"id":"f1","type":"","value":"","rendered_in":["title"],"english_forms":[""],"chinese_forms":[""]}],"key_entities":[],"dates":[],"numbers":[],"versions":[],"products":[],"companies":[],"technologies":[],"scope":"","limitations":[],"importance_reasons":[]},"title_en":"","title_cn":"","title_original":"","source":"","published_at":"","original_url":"","what_happened_en":"","what_happened":"","why_it_matters_en":"","why_it_matters":"","importance_score":0}]}.
"""

REPAIR_SYSTEM_PROMPT = """You repair only explicitly named bilingual Tech Daily fields. Use only the supplied verified candidate and its shared fact_schema. Do not change id, category, fact_schema, source metadata, score, or fields that are not listed in repair_fields. Do not add facts. English and Chinese must preserve the same fact schema, including numbers, dates, versions, products, organizations, support/release intent, scope, and limitations. Return only JSON: {"id":"","fields":{"one_allowed_field":""}}."""


class EnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnrichmentResult:
    candidates: int
    saved: int
    model: str | None
    usage: dict[str, object]
    output_path: Path | None
    token_budget_status: str = "normal"
    minimum_not_met_reason: str | None = None


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
            "content_type_hint": "deep_read" if deep_read(article) else "news",
            "source_region": article.source_region,
            "source_tier": article.source_tier,
            "source_role": article.source_role,
            "channel_hint": article.channel,
            "category_hint": article.category,
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


def _model_items_by_id(payload: dict[str, object], articles: list[Article]) -> dict[str, dict[str, object]]:
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
    return by_id


def _build_enrichment(item: dict[str, object], article: Article, model: str, generated_at: str) -> Enrichment:
    category = _string(item.get("category"), "category", article.id)
    if category not in TECH_CATEGORIES:
        raise EnrichmentError(f"Item {article.id} has an invalid category")
    fact_schema = normalize_fact_schema(item.get("fact_schema"), article.id)
    if fact_schema["category"] != category:
        raise EnrichmentError(f"Item {article.id} fact_schema.category does not match category")
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
        importance_score=score, fact_schema_json=json.dumps(fact_schema, ensure_ascii=False, sort_keys=True),
    )
    if BLOCKED_CONTENT_TERMS.search(json.dumps(enrichment.to_dict(), ensure_ascii=False)):
        raise EnrichmentError(f"Item {article.id} contains blocked content")
    for field, english, chinese in (
        ("title", enrichment.title_en, enrichment.title_cn),
        ("what_happened", enrichment.what_happened_en, enrichment.what_happened),
        ("why_it_matters", enrichment.why_it_matters_en, enrichment.why_it_matters),
    ):
        validate_semantic_consistency(english, chinese, field, fact_schema)
    return enrichment


@dataclass(frozen=True)
class ItemValidation:
    article: Article
    raw_item: dict[str, object]
    enrichment: Enrichment | None
    error: Exception | None


def _validated_batch(payload: dict[str, object], articles: list[Article], model: str, generated_at: str) -> list[ItemValidation]:
    by_id = _model_items_by_id(payload, articles)
    results: list[ItemValidation] = []
    for article in articles:
        item = by_id[article.id]
        try:
            results.append(ItemValidation(article, item, _build_enrichment(item, article, model, generated_at), None))
        except (EnrichmentError, BilingualValidationError) as exc:
            results.append(ItemValidation(article, item, None, exc))
    return results


def _validated_enrichments(payload: dict[str, object], articles: list[Article], model: str, generated_at: str) -> list[Enrichment]:
    """Strict helper kept for unit callers; production handles item failures independently."""
    results = _validated_batch(payload, articles, model, generated_at)
    failures = [result for result in results if result.error]
    if failures:
        failure = failures[0]
        raise EnrichmentError(f"Item {failure.article.id} failed bilingual validation: {failure.error}") from failure.error
    enrichments = [result.enrichment for result in results if result.enrichment]
    return sorted(enrichments, key=lambda value: value.importance_score, reverse=True)


def _audit_entry(result: ItemValidation, *, repaired: bool = False) -> dict[str, object]:
    item = result.raw_item
    error = result.error
    bilingual = error if isinstance(error, BilingualValidationError) else None
    return {
        "article_id": result.article.id,
        "source": result.article.source,
        "category": item.get("category", result.article.category),
        "status": "accepted" if result.enrichment else "rejected",
        "reject_stage": bilingual.stage if bilingual else ("validation" if error else None),
        "reject_reason": str(error) if error else None,
        "hard_fact_conflict": bilingual.hard_fact_conflict if bilingual else False,
        "natural_translation_difference": bilingual.natural_translation_difference if bilingual else False,
        "repaired": repaired,
        "english": {
            "title": item.get("title_en"), "what_happened": item.get("what_happened_en"),
            "why_it_matters": item.get("why_it_matters_en"),
        },
        "chinese": {
            "title": item.get("title_cn"), "what_happened": item.get("what_happened"),
            "why_it_matters": item.get("why_it_matters"),
        },
        "fact_schema": item.get("fact_schema"),
    }


def _repair_prompt(article: Article, item: dict[str, object], issue: BilingualValidationError, per_article_characters: int) -> str:
    return "Repair request:\n" + json.dumps({
        "candidate": {
            "id": article.id, "title_original": article.original_title, "source": article.source,
            "published_at": article.published_at, "original_url": article.original_url,
            "clean_text": article.clean_text[:per_article_characters],
        },
        "fact_schema": item.get("fact_schema"),
        "repair_fields": list(issue.repair_fields),
        "validation_reason": issue.reason,
        "current_fields": {
            "title_en": item.get("title_en"), "title_cn": item.get("title_cn"),
            "what_happened_en": item.get("what_happened_en"), "what_happened": item.get("what_happened"),
            "why_it_matters_en": item.get("why_it_matters_en"), "why_it_matters": item.get("why_it_matters"),
        },
    }, ensure_ascii=False)


def _apply_repair_response(content: str, item: dict[str, object], article_id: str, allowed_fields: tuple[str, ...]) -> dict[str, object]:
    payload = _extract_json(content)
    if payload.get("id") != article_id or not isinstance(payload.get("fields"), dict):
        raise EnrichmentError(f"Item {article_id} returned an invalid field repair")
    fields = payload["fields"]
    if not fields or set(fields) - set(allowed_fields):
        raise EnrichmentError(f"Item {article_id} repair changed an unapproved field")
    repaired = dict(item)
    for field, value in fields.items():
        repaired[field] = _string(value, field, article_id)
    return repaired


def _eligible_content(article: Article) -> bool:
    parsed = urlparse(article.original_url)
    host = (parsed.hostname or "").lower()
    return (article.verification_status == "source_verified" and article.category in TECH_CATEGORIES
            and article.source_tier <= 3
            and article.source_role != "discovery"
            and parsed.scheme == "https" and bool(host)
            and parsed.path.rstrip("/").lower() not in {"", "/research", "/news", "/about", "/newsroom", "/en/news"}
            and not any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_CONTENT_HOSTS)
            and not BLOCKED_CONTENT_TERMS.search(article.title + " " + article.clean_text[:3_000]))


def _dedupe_events(items: list[Enrichment]) -> list[Enrichment]:
    """Keep one highest-value rendering when multiple sources describe the same event."""
    selected: list[Enrichment] = []
    for item in sorted(items, key=lambda value: value.importance_score, reverse=True):
        if any(same_event(item.title_cn, other.title_cn) and same_event(item.title_en, other.title_en)
               for other in selected):
            continue
        selected.append(item)
    return selected


def _usage_total(rows: list[dict[str, object]]) -> dict[str, object]:
    """Preserve raw per-call usage in SQLite while exposing a compact run total."""
    numeric_keys = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    total: dict[str, object] = {key: sum(int(row.get(key) or 0) for row in rows) for key in numeric_keys}
    total["calls"] = len(rows)
    return total


@dataclass(frozen=True)
class TokenEstimate:
    input_tokens: int
    output_tokens: int
    safety_tokens: int

    @property
    def required_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.safety_tokens


def _estimate_request_tokens(system: str, prompt: str, output_limit: int, observed_input_ratio: float = 0) -> TokenEstimate:
    characters = len(system) + len(prompt)
    # The existing character estimator is the floor. A conservative baseline and actual
    # input usage from earlier batches protect the remaining daily budget.
    estimated_input = max((characters + 2) // 3, ceil(characters / 2), ceil(characters * observed_input_ratio))
    return TokenEstimate(estimated_input, output_limit, max(256, ceil(estimated_input * .1)))


def _budget_status(accepted_count: int, token_used: int, token_budget: int, next_required: int, minimum: int) -> str:
    if token_used + next_required <= token_budget:
        return "normal"
    # The configured minimum still defines a normal-sized edition; it is no longer a publication gate.
    return "graceful_stop" if accepted_count >= 1 else "exhausted_before_minimum"


def _daily_mode(publishable_count: int, normal_minimum: int) -> str:
    if publishable_count >= normal_minimum:
        return "normal"
    if publishable_count >= 5:
        return "graceful_degraded"
    return "minimal_daily" if publishable_count >= 1 else "true_failure"


def _ready_selection(inventory: list[Article], all_enriched: dict[str, Enrichment], now: datetime, rules: dict[str, object], past: list[dict[str, object]], limit: int) -> tuple[list[Enrichment], dict[str, dict[str, object]], dict[str, object]]:
    ready = [replace(article, category=all_enriched[article.id].category) for article in inventory if article.id in all_enriched]
    final, metadata, diagnostics = select(ready, now, rules, past, all_enriched, limit=limit)
    return _dedupe_events([all_enriched[article.id] for article in final]), metadata, diagnostics


def _record_attempted_as_history(past: list[dict[str, object]], articles: list[Article]) -> list[dict[str, object]]:
    """Ask the existing deterministic selector for previously untried fallback candidates."""
    return [*past, *[
        {"original_url": article.original_url, "title_original": article.title, "fingerprint": article.fingerprint}
        for article in articles
    ]]


def run_enrichment(root: Path, *, dry_run: bool = False, now: datetime | None = None, audit=None,
                   token_budget_override: int | None = None) -> EnrichmentResult:
    model_batch_size = _positive_int("MAX_BATCH_ARTICLES", 12)
    if model_batch_size < 5 or model_batch_size > 12:
        raise EnrichmentError("MAX_BATCH_ARTICLES must be between 5 and 12 per model call")
    character_limit = _positive_int("MAX_NEWS_INPUT_CHARS_PER_ARTICLE", 4_000)
    max_output_tokens = _positive_int("MAX_NEWS_OUTPUT_TOKENS", 7_200)
    repair_output_tokens = _positive_int("MAX_BILINGUAL_REPAIR_OUTPUT_TOKENS", 900)
    max_daily_tokens = min(_positive_int("MAX_DAILY_TOKENS", 40_000), 40_000)
    if token_budget_override is not None:
        if token_budget_override <= 0:
            raise EnrichmentError("token_budget_override must be positive")
        max_daily_tokens = token_budget_override
    now = (now or datetime.now(UTC)).astimezone(UTC)
    data_dir = root / "data"
    output_path = data_dir / "latest-enrichment.json"
    audit_path = data_dir / "bilingual-validation-audit.json"
    store = ArticleStore(data_dir / "ai_daily.sqlite3")
    try:
        rules = policy(root)
        daily_limit = int(rules["maximum"])
        inventory, cached = store.supply_inventory((now - timedelta(hours=168)).isoformat(), (now + timedelta(minutes=10)).isoformat())
        inventory_before_content_gate = inventory
        if audit:
            audit.inventory_origin_events(inventory)
            for article in inventory:
                if _eligible_content(article):
                    audit.event(article, "content_gate", "kept", "eligible_content")
                else:
                    parsed = urlparse(article.original_url)
                    host = (parsed.hostname or "").lower()
                    reason = ("source_not_verified" if article.verification_status != "source_verified" else
                              "category_not_supported" if article.category not in TECH_CATEGORIES else
                              "tier_above_3" if article.source_tier > 3 else
                              "discovery_source" if article.source_role == "discovery" else
                              "non_https_or_host_missing" if parsed.scheme != "https" or not host else
                              "index_page" if parsed.path.rstrip("/").lower() in {"", "/research", "/news", "/about", "/newsroom", "/en/news"} else
                              "blocked_host" if any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_CONTENT_HOSTS) else
                              "blocked_content_terms")
                    audit.event(article, "content_gate", "dropped", reason)
        inventory = [article for article in inventory if _eligible_content(article)]
        past = history(root.parent / "ai-daily-public-site", now)
        chosen, _, selection_diagnostics = select(inventory, now, rules, past, cached, limit=daily_limit,
            on_decision=(lambda a, st, status, reason, detail: audit.event(a, "initial_" + st, status, reason, detail)) if audit else None)
        if audit:
            audit.inventory_sets(inventory_before_content_gate, inventory, chosen)
        if audit: audit.metrics.update({
            "inventory_eligible_content": len(inventory),
            "candidate_pool": sum(any(e["stage"] == "initial_candidate_pool" and e["status"] == "kept"
                                      for e in trace["events"]) for trace in audit.traces.values()),
            "initial_selected": len(chosen)})
        initial_articles = [article for article in chosen if article.id not in cached]
        if not chosen:
            selection_diagnostics["minimum_not_met_reason"] = "no source-verified candidates passed the existing supply policy"
            selection_diagnostics.update({"daily_mode": "true_failure", "daily_status": "true_failure",
                                          "publishable_count": 0, "target_count": rules["target"],
                                          "degraded_reason": "no_eligible_candidates", "token_guard_triggered": False,
                                          "source_shortage": True, "validation_shortage": False})
            if audit:
                audit.metrics.update({key: selection_diagnostics[key] for key in
                                      ("daily_mode", "publishable_count", "target_count", "degraded_reason",
                                       "token_guard_triggered", "source_shortage", "validation_shortage")})
            (data_dir / "supply-status.json").write_text(json.dumps(selection_diagnostics, indent=2), encoding="utf-8")
            return EnrichmentResult(0, 0, None, {}, None)

        prompt = _candidate_payload(initial_articles, character_limit)
        if dry_run:
            preview_path = data_dir / "enrichment-preview.json"
            preview_path.write_text(json.dumps({"candidates": len(initial_articles), "prompt_characters": len(prompt), "dry_run": True}, indent=2), encoding="utf-8")
            return EnrichmentResult(len(initial_articles), 0, None, {}, preview_path)

        all_enriched = dict(cached)
        generated: list[Enrichment] = []
        audit_entries: list[dict[str, object]] = []
        usages: list[dict[str, object]] = []
        request_attempts: list[dict[str, object]] = []
        attempted = list(chosen)
        model: str | None = None
        stats = {
            "initial_candidates": len(initial_articles), "fact_schema_generated": 0, "model_accepted": 0,
            "hard_fact_rejected": 0, "semantic_rejected": 0, "repaired": 0, "fallback_added": 0,
        }
        client = None
        batch_counter = 0
        batches_completed = 0
        observed_input_ratio = 0.0
        token_budget_status = "normal"
        stopped_reason: str | None = None
        publishable, metadata, diagnostics = _ready_selection(inventory, all_enriched, now, rules, past, daily_limit)
        budget_metadata: dict[str, object] = {}
        active_batch_context: dict[str, object] = {}
        last_failure: dict[str, object] | None = None

        def update_budget_metadata(estimate: TokenEstimate | None = None) -> None:
            used = store.daily_total_tokens(now.date().isoformat())
            budget_metadata.update({
                "token_budget_status": token_budget_status,
                "token_budget": max_daily_tokens,
                "token_used": used,
                "token_remaining": max(0, max_daily_tokens - used),
                "batches_completed": batches_completed,
                "accepted_count": len(publishable),
                "target_count": rules["target"],
                "stopped_reason": stopped_reason,
            })
            if estimate is not None or token_budget_status == "normal":
                budget_metadata.update({
                    "next_batch_estimated_input_tokens": estimate.input_tokens if estimate else None,
                    "next_batch_estimated_output_tokens": estimate.output_tokens if estimate else None,
                    "next_batch_required_tokens": estimate.required_tokens if estimate else None,
                })

        update_budget_metadata()

        def persist_audit() -> None:
            audit_path.write_text(json.dumps({
                "generated_at": now.isoformat(), "task": TASK_NAME, "model": model, "summary": stats,
                "entries": audit_entries, "usage": _usage_total(usages) if usages else {},
                "failure": last_failure, "token_budget": budget_metadata,
                "model_request_attempts": request_attempts,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        def record_final_audit() -> None:
            if not audit:
                return
            audit.enrichment_sets(attempted)
            # One final deterministic selector pass provides reasons without another model request.
            ready = [replace(article, category=all_enriched[article.id].category) for article in inventory if article.id in all_enriched]
            final_drops = {}
            def on_final_decision(article, stage, status, reason, detail):
                audit.event(article, "final_" + stage, status, reason, detail)
                if status == "dropped": final_drops[article.id] = (reason, detail)
            selected, _, _ = select(ready, now, rules, past, all_enriched, limit=daily_limit,
                on_decision=on_final_decision)
            pre_dedup = [all_enriched[a.id] for a in selected]
            published_ids = {e.article_id for e in publishable}
            for enrichment in pre_dedup:
                article = next(a for a in ready if a.id == enrichment.article_id)
                if enrichment.article_id in published_ids:
                    audit.event(article, "publishable", "kept", "selected_after_event_dedup")
                else:
                    twin = next((other for other in publishable if same_event(enrichment.title_cn, other.title_cn) and same_event(enrichment.title_en, other.title_en)), None)
                    audit.event(article, "publishable", "dropped", "duplicate_bilingual_event", {"duplicate_of": twin.article_id if twin else None})
            for article in ready:
                if article.id not in {e.article_id for e in pre_dedup}:
                    reason, detail = final_drops.get(article.id, ("final_selector_excluded", {}))
                    audit.event(article, "publishable", "dropped", reason, detail)
            audit.metrics.update({"enrichment_requested": len(attempted), "enrichment_succeeded": stats["model_accepted"],
                "enrichment_failed": len(audit_entries) - stats["model_accepted"], "validation_passed": stats["model_accepted"],
                "validation_failed": stats["hard_fact_rejected"] + stats["semantic_rejected"],
                "repair_succeeded": stats["repaired"], "repair_attempted": sum(bool(x.get("repair_attempted") or x.get("repaired")) for x in audit_entries),
                "final_publishable": len(publishable), "target_count": rules["target"], "min_count": rules["minimum"],
                "max_count": rules["maximum"], "token_used": budget_metadata["token_used"],
                "token_remaining": budget_metadata["token_remaining"], "token_budget_status": token_budget_status})

        def process_batch_body(articles: list[Article], *, fallback: bool) -> bool:
            nonlocal model, character_limit, client, observed_input_ratio, token_budget_status, stopped_reason
            if not articles:
                return True
            while True:
                batch_prompt = _candidate_payload(articles, character_limit)
                batch_output_tokens = min(max_output_tokens, max(1_800, (max_output_tokens * len(articles) + model_batch_size - 1) // model_batch_size))
                estimate = _estimate_request_tokens(NEWS_SYSTEM_PROMPT, batch_prompt, batch_output_tokens, observed_input_ratio)
                current_total = store.daily_total_tokens(now.date().isoformat())
                status = _budget_status(len(publishable), current_total, max_daily_tokens, estimate.required_tokens, rules["minimum"])
                if status == "normal" or character_limit <= 1200:
                    break
                character_limit = max(1200, character_limit - 200)
            update_budget_metadata(estimate)
            if status != "normal":
                token_budget_status = status
                stopped_reason = "token_budget_insufficient_for_next_batch"
                update_budget_metadata(estimate)
                return False
            if client is None:
                client = DeepSeekClient()
            client.request_context = {"batch_id": f"main-{batch_counter}", "article_ids": [article.id for article in articles]}
            client.on_attempt = request_attempts.append
            if audit:
                audit.model_submitted(articles, batch_counter)
            try:
                content, batch_usage, model_name = client.complete_json(system_prompt=NEWS_SYSTEM_PROMPT, user_prompt=batch_prompt, max_tokens=batch_output_tokens)
            except DeepSeekError as exc:
                active_batch_context["model"] = exc.model or getattr(client, "model", None)
                active_batch_context["token_usage"] = _usage_total([exc.usage]) if exc.usage else None
                if exc.usage is not None:
                    store.record_usage(task=f"{TASK_NAME}_rejected", model=exc.model or "unknown", created_at=now.isoformat(), usage=exc.usage)
                    client.mark_usage_recorded()
                    store.commit()
                raise
            model = model_name
            actual_prompt_tokens = batch_usage.get("prompt_tokens")
            if isinstance(actual_prompt_tokens, int) and actual_prompt_tokens > 0:
                observed_input_ratio = max(observed_input_ratio, actual_prompt_tokens / (len(NEWS_SYSTEM_PROMPT) + len(batch_prompt)))
            active_batch_context["model"] = model_name
            active_batch_context["token_usage"] = _usage_total([batch_usage])
            usages.append(batch_usage)
            try:
                results = _validated_batch(_extract_json(content), articles, model_name, now.isoformat())
            except EnrichmentError as exc:
                # A structurally invalid batch cannot be repaired safely, but its token use remains logged above.
                store.record_usage(task=f"{TASK_NAME}_rejected", model=model_name, created_at=now.isoformat(), usage=batch_usage)
                client.mark_usage_recorded()
                store.commit()
                raise mark_failure(exc, "validation")
            store.record_usage(task=TASK_NAME, model=model_name, created_at=now.isoformat(), usage=batch_usage)
            client.mark_usage_recorded()
            for result in results:
                if isinstance(result.raw_item.get("fact_schema"), dict):
                    stats["fact_schema_generated"] += 1
                if result.enrichment:
                    generated.append(result.enrichment)
                    all_enriched[result.article.id] = result.enrichment
                    store.save_enrichment(result.enrichment)
                    stats["model_accepted"] += 1
                    if fallback:
                        stats["fallback_added"] += 1
                    audit_entries.append(_audit_entry(result))
                    if audit: audit.event(result.article, "validation", "kept", "model_accepted")
                    continue
                issue = result.error if isinstance(result.error, BilingualValidationError) else None
                if audit:
                    audit.event(result.article, "validation", "dropped", issue.stage if issue else "schema_invalid",
                                {"error_type": type(result.error).__name__ if result.error else None})
                if issue and issue.stage == "hard_facts":
                    stats["hard_fact_rejected"] += 1
                elif issue and issue.stage == "semantic":
                    stats["semantic_rejected"] += 1
                if not issue or not issue.repair_fields:
                    audit_entries.append(_audit_entry(result))
                    continue
                # One item receives at most one repair call, restricted to the failed field(s).
                active_batch_context["local_repair_triggered"] = True
                repair_prompt = _repair_prompt(result.article, result.raw_item, issue, character_limit)
                repair_estimate = _estimate_request_tokens(REPAIR_SYSTEM_PROMPT, repair_prompt, repair_output_tokens, observed_input_ratio)
                if _budget_status(len(publishable), store.daily_total_tokens(now.date().isoformat()), max_daily_tokens,
                                  repair_estimate.required_tokens, rules["minimum"]) != "normal":
                    audit_entries.append({**_audit_entry(result), "repair_skipped": "daily token guard"})
                    if audit: audit.event(result.article, "repair", "dropped", "token_guard")
                    continue
                try:
                    client.request_context = {"batch_id": f"repair-{batch_counter}-{result.article.id}",
                                              "article_ids": [result.article.id]}
                    repaired_content, repair_usage, repair_model = client.complete_json(
                        system_prompt=REPAIR_SYSTEM_PROMPT, user_prompt=repair_prompt, max_tokens=repair_output_tokens,
                    )
                    usages.append(repair_usage)
                    store.record_usage(task=f"{TASK_NAME}_field_repair", model=repair_model, created_at=now.isoformat(), usage=repair_usage)
                    client.mark_usage_recorded()
                    repaired_item = _apply_repair_response(repaired_content, result.raw_item, result.article.id, issue.repair_fields)
                    repaired = ItemValidation(result.article, repaired_item, _build_enrichment(repaired_item, result.article, repair_model, now.isoformat()), None)
                except (DeepSeekError, EnrichmentError, BilingualValidationError) as repair_error:
                    if isinstance(repair_error, DeepSeekError) and repair_error.usage is not None:
                        usages.append(repair_error.usage)
                        store.record_usage(task=f"{TASK_NAME}_field_repair_rejected", model=repair_error.model or model_name,
                                           created_at=now.isoformat(), usage=repair_error.usage)
                        client.mark_usage_recorded()
                    audit_entries.append({**_audit_entry(result), "repair_attempted": True, "repair_result": str(repair_error)})
                    if audit: audit.event(result.article, "repair", "dropped", "repair_failed", {"error_type": type(repair_error).__name__})
                    continue
                generated.append(repaired.enrichment)
                all_enriched[repaired.article.id] = repaired.enrichment
                store.save_enrichment(repaired.enrichment)
                stats["model_accepted"] += 1
                stats["repaired"] += 1
                if fallback:
                    stats["fallback_added"] += 1
                audit_entries.append({**_audit_entry(repaired, repaired=True), "initial_reject_stage": issue.stage, "initial_reject_reason": issue.reason})
                if audit:
                    audit.event(repaired.article, "repair", "kept", "repair_succeeded", {"initial_reject_stage": issue.stage})
                    audit.event(repaired.article, "validation", "kept", "accepted_after_repair")
            store.commit()
            return True

        def process_batch(articles: list[Article], *, fallback: bool) -> bool:
            nonlocal batch_counter, batches_completed, active_batch_context, last_failure, publishable, metadata, diagnostics
            if not articles:
                return True
            batch_counter += 1
            if audit:
                for article in articles: audit.event(article, "enrichment", "kept", "batch_requested", {"batch_index": batch_counter, "fallback": fallback})
            active_batch_context = {"batch_index": batch_counter, "batch_article_ids": [article.id for article in articles],
                                    "model": None, "request_id": None, "token_usage": None,
                                    "local_repair_triggered": False, "fallback": fallback}
            usage_start = len(usages)
            try:
                completed = process_batch_body(articles, fallback=fallback)
                if audit and not completed:
                    for article in articles: audit.event(article, "token_budget", "dropped", token_budget_status, {"batch_index": batch_counter})
                if completed:
                    batches_completed += 1
                    publishable, metadata, diagnostics = _ready_selection(inventory, all_enriched, now, rules, past, daily_limit)
                    update_budget_metadata()
                persist_audit()
                return completed
            except Exception as exc:
                if isinstance(exc, DeepSeekError):
                    active_batch_context["retry_history"] = exc.retry_history
                    active_batch_context["model_request_failed"] = exc.model_request_failed
                if len(usages) > usage_start:
                    active_batch_context["token_usage"] = _usage_total(usages[usage_start:])
                stage = failure_stage(exc, "enrichment")
                mark_failure(exc, stage, **active_batch_context)
                last_failure = failure_details(exc, stage)
                if audit:
                    update_budget_metadata()
                    record_final_audit()
                if audit:
                    reason = "model_request_failed" if isinstance(exc, DeepSeekError) and exc.model_request_failed else "batch_failed"
                    for article in articles: audit.event(article, stage, "dropped", reason, {"batch_index": batch_counter, "error_type": type(exc).__name__})
                try:
                    persist_audit()
                except OSError:
                    pass  # Diagnostic writing must not replace the original failure.
                raise

        for offset in range(0, len(initial_articles), model_batch_size):
            if not process_batch(initial_articles[offset:offset + model_batch_size], fallback=False):
                break
        # Work toward the target using untouched real candidates; quality gates and the hard maximum remain unchanged.
        while token_budget_status == "normal" and len(publishable) < rules["target"]:
            fallback_history = _record_attempted_as_history(past, attempted)
            fallback_chosen, _, _ = select(inventory, now, rules, fallback_history, cached, limit=daily_limit)
            fallback_articles = [article for article in fallback_chosen if article.id not in all_enriched and article.id not in {item.id for item in attempted}]
            if not fallback_articles:
                break
            needed = max(1, rules["target"] - len(publishable))
            batch = fallback_articles[:min(model_batch_size, max(3, needed * 2))]
            attempted.extend(batch)
            if not process_batch(batch, fallback=True):
                break

        stats["final_count"] = len(publishable)
        stats["minimum_met"] = len(publishable) >= rules["minimum"]
        diagnostics.update(stats)
        update_budget_metadata()
        diagnostics.update(budget_metadata)
        diagnostics["selected"] = len(publishable)
        diagnostics["shortfall"] = max(0, rules["minimum"] - len(publishable))
        daily_mode = _daily_mode(len(publishable), rules["minimum"])
        source_shortage = len(chosen) < rules["minimum"]
        validation_shortage = (len(publishable) < rules["minimum"]
                               and stats["hard_fact_rejected"] + stats["semantic_rejected"] > 0)
        degraded_reason = (None if daily_mode == "normal" else
                           "token_budget_insufficient_for_next_batch" if token_budget_status != "normal" else
                           "eligible_candidate_shortage" if source_shortage else
                           "validation_rejections" if validation_shortage else
                           "final_selection_shortfall")
        diagnostics.update({"daily_mode": daily_mode, "daily_status": daily_mode,
                            "publishable_count": len(publishable), "target_count": rules["target"],
                            "degraded_reason": degraded_reason,
                            "token_guard_triggered": token_budget_status != "normal",
                            "source_shortage": source_shortage, "validation_shortage": validation_shortage})
        if token_budget_status != "normal" and not stats["minimum_met"]:
            diagnostics["minimum_not_met_reason"] = "token_budget_exhausted"
        elif not stats["minimum_met"]:
            diagnostics["minimum_not_met_reason"] = "all eligible, source-verified candidates were exhausted after fact-schema validation"
        total_usage = _usage_total(usages) if usages else {}
        if audit:
            audit.metrics.update({key: diagnostics[key] for key in ("daily_mode", "publishable_count", "target_count",
                                  "degraded_reason", "token_guard_triggered", "source_shortage", "validation_shortage")})
        record_final_audit()
        persist_audit()
        (data_dir / "supply-status.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        if not publishable:
            return EnrichmentResult(len(attempted), 0, model, total_usage, None, token_budget_status,
                                    diagnostics.get("minimum_not_met_reason"))
        output_path.write_text(json.dumps({
            "schema_version": 3, "generated_at": now.isoformat(), "task": TASK_NAME, "model": model, "usage": total_usage,
            "supply": diagnostics,
            "items": [{**enrichment.to_dict(), **metadata[enrichment.article_id]} for enrichment in publishable],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return EnrichmentResult(len(attempted), len(publishable), model, total_usage, output_path,
                                token_budget_status, diagnostics.get("minimum_not_met_reason"))
    finally:
        store.close()
