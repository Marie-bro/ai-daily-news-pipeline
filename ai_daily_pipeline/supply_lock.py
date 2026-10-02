"""Append-only orchestration around the existing selector; no model calls."""
from collections import Counter
from datetime import datetime

from .supply import deep_read, level, select, same_event

LAYERS = ("today", "catch_up", "deep_read", "evergreen")
PRIORITY = {name: index for index, name in enumerate(LAYERS)}


def historical_age_days(article, now):
    return (now - datetime.fromisoformat(article.published_at)).total_seconds() / 86400


def classify_supply_layer(article, now, reserve=None):
    """24h semantics, including the existing ten-minute future tolerance, come from level()."""
    stage = level(article, now)
    if stage in (1, 2):
        return "today"
    if stage == 3:
        return "catch_up"
    if stage == 4:
        return "deep_read"
    meta = (reserve or {}).get(article.id, {})
    age = historical_age_days(article, now)
    if (meta.get("reserve_status") == "eligible" and meta.get("cache_available")
            and meta.get("validation_status") == "passed" and 3 < age <= 365):
        layer = meta.get("supply_layer")
        if layer == "deep_read" and age <= 30 or layer == "evergreen":
            return layer
    return None


def assert_today_lock(locked_today_ids, final_ids):
    if not set(locked_today_ids).issubset(set(final_ids)):
        raise RuntimeError("Today lock violated: historical filling replaced verified Today content")
    return True


def select_locked(articles, now, rules, past=(), enrichments=None, limit=None,
                  on_decision=None, reserve=None, bilingual_duplicate=None, cache_ids=None):
    """Select/rank within each layer, lock qualified Today, then append only."""
    enrichments = enrichments or {}
    reserve = reserve or {}
    cache_ids = set(enrichments) if cache_ids is None else set(cache_ids)
    grouped = {key: [] for key in LAYERS}
    layers = {}
    def emit(article, stage, status, reason, detail=None):
        if on_decision:
            on_decision(article, stage, status, reason, detail or {})
    for article in articles:
        layer = classify_supply_layer(article, now, reserve)
        if layer is not None:
            layers[article.id] = layer
            grouped[layer].append(article)
        else:
            emit(article, "supply_classification", "dropped", "outside_eligible_supply_layers")
    target = min(int(rules["target"]), limit or int(rules["maximum"]))
    final, metadata, locked = [], {}, []
    diagnostics = {"today_candidates": len(grouped["today"])}
    past = list(past)
    layer_stats = []
    for layer in LAYERS:
        considered = grouped[layer]
        diagnostics[layer + "_considered"] = len(considered)
        if layer != "today" and len(locked) >= rules["minimum"]:
            for article in considered:
                emit(article, "historical_fill", "dropped", "normal_today_edition_no_supplement")
            diagnostics[layer + "_selected"] = 0
            continue
        remaining = max(0, target - len(final))
        if not remaining:
            for article in considered:
                emit(article, "historical_fill", "dropped", "no_remaining_slots")
            diagnostics[layer + "_selected"] = 0
            continue
        # Same URL/fingerprint/event predicates; prior layers are dedup references, never competitors.
        references = past + [{"article_id": article.id, "original_url": article.original_url,
                              "fingerprint": article.fingerprint, "title_original": article.title}
                             for article in final]
        candidates = []
        for article in considered:
            if bilingual_duplicate and any(bilingual_duplicate(article, other) for other in final):
                emit(article, "historical_fill", "dropped", "duplicate_with_locked_selection")
                continue
            candidates.append(article)
        chosen, meta, stats = select(candidates, now, rules, references, enrichments,
            limit=remaining, on_decision=on_decision, supply_layers=layers)
        layer_stats.append({"supply_layer": layer, **stats})
        accepted = []
        for article in chosen:
            if bilingual_duplicate and any(bilingual_duplicate(article, other) for other in accepted):
                emit(article, "bilingual_event_dedup", "dropped", "duplicate_bilingual_event")
                continue
            accepted.append(article)
            entry = {**meta[article.id], "reserve_status": "not_applicable", "reserve_type": None,
                     "reserve_reason": None, "policy_validity": "unknown", **reserve.get(article.id, {}),
                     "article_id": article.id, "supply_layer": layer, "is_today": layer == "today",
                     "locked_today": layer == "today", "selected_as_fallback": layer != "today",
                     "fallback_priority": PRIORITY[layer],
                     "historical_age_days": round(historical_age_days(article, now), 6),
                     "cache_reused": article.id in cache_ids,
                     "deep_read": layer == "deep_read", "remaining_slots_before": target - len(final)}
            # Reserve metadata must never override original article identity or publication metadata.
            entry.update(published_at=article.published_at, fingerprint=article.fingerprint)
            metadata[article.id] = entry
            final.append(article)
            if layer == "today":
                locked.append(article.id)
            emit(article, "today_lock" if layer == "today" else "historical_fill", "kept",
                 "today_locked" if layer == "today" else "append_only_fallback", entry)
        diagnostics[layer + "_selected"] = len(accepted)
        if layer == "today":
            diagnostics.update(today_locked=len(locked), locked_today_ids=list(locked),
                               remaining_slots=max(0, target - len(locked)))
        assert_today_lock(locked, [article.id for article in final])
    diagnostics.update(final_ids=[article.id for article in final], locked_today_ids=locked,
        today_lock_preserved=assert_today_lock(locked, [article.id for article in final]),
        selected=len(final), shortfall=max(0, rules["minimum"] - len(final)),
        minimum_not_met_reason="qualified supply exhausted" if len(final) < rules["minimum"] else None,
        sources=len({a.source for a in final}), categories=dict(Counter(a.category for a in final)),
        channels=dict(Counter(metadata[a.id]["channel"] for a in final)),
        explore_count=sum(a.category not in rules["personal_categories"] for a in final),
        source_counts=dict(Counter(a.source for a in final)),
        diversity_exception=any(stat["diversity_exception"] for stat in layer_stats),
        levels=layer_stats)
    return final, metadata, diagnostics
