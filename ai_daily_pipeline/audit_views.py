"""Pure projections of recorded run evidence. No database, network or model calls."""
from collections import Counter, defaultdict
from html import escape
import json

UNKNOWN = "not_observable"
SUPPLY_FIELDS = ("supply_layer", "is_today", "locked_today", "historical_age_days",
                "selected_as_fallback", "fallback_priority", "reserve_type", "reserve_status",
                "reserve_reason", "policy_validity", "deep_read", "cache_reused")
SUPPLY_COUNTS = ("today_candidates", "today_locked", "remaining_slots", "catchup_considered",
                "catchup_selected", "deep_read_considered", "deep_read_selected",
                "evergreen_considered", "evergreen_selected", "today_lock_preserved")


def _ids(value):
    return set(value.get("article_ids", [])) if isinstance(value, dict) and isinstance(value.get("article_ids"), list) else None


def _boundary(label, before, after):
    left, right = _ids(before), _ids(after)
    removed = len(left - right) if left is not None and right is not None and right <= left else UNKNOWN
    return {"stage": label, "input": before.get("count", UNKNOWN), "kept": after.get("count", UNKNOWN),
            "dropped": removed, "basis": after.get("basis", UNKNOWN),
            "note": "Set differences only where saved sets prove a subset; parser/object counts are not silently subtracted."}


def build_inspection(data):
    metrics = data.get("metrics", {})
    flows = data.get("flows", {})
    collection = flows.get("collection", {})
    inventory = flows.get("inventory_candidate", {})
    traces = data.get("articles", [])
    calls = metrics.get("model_request_attempts", [])
    publication = metrics.get("published_article_ids")
    published = set(publication) if isinstance(publication, list) else None
    requested = _ids(inventory.get("enrichment_attempted"))
    submitted = _ids(inventory.get("model_submitted"))
    pool = _ids(inventory.get("candidate_pool"))
    selected = _ids(inventory.get("selected_candidate"))
    rows = []
    stages = defaultdict(list)
    for trace in traces:
        events = trace.get("events", [])
        for event in events:
            stages[event.get("stage", UNKNOWN)].append((trace.get("article_id"), event))
        ident = trace.get("article_id", trace.get("trace_id", UNKNOWN))
        def last(stage):
            return next((e for e in reversed(events) if e.get("stage") == stage), {})
        origin = last("inventory").get("reason", UNKNOWN)
        cache = last("enrichment_cache").get("reason", UNKNOWN)
        validation, repair = last("validation"), last("repair")
        final = last("publishable")
        is_published = ident in published if published is not None else UNKNOWN
        if is_published is True:
            reason = final.get("reason", UNKNOWN)
        elif final.get("status") == "kept":
            reason = metrics.get("skipped_reason") or UNKNOWN
        elif final.get("status") == "dropped":
            reason = final.get("reason", UNKNOWN)
        else:
            # A repaired validation rejection is not the final reason. Initial/final
            # decisions take priority over exploratory fallback-selector decisions.
            drops = [e for e in events if e.get("status") == "dropped" and not e.get("stage", "").startswith("fallback_")]
            if validation.get("status") == "kept":
                drops = [e for e in drops if e.get("stage") not in {"validation", "repair"}]
            reason = drops[-1].get("reason", UNKNOWN) if drops else UNKNOWN
        score = UNKNOWN
        for event in reversed(events):
            if event.get("stage") in {"final_selection", "final_candidate_pool", "final_quality_filter", "validation", "enrichment_cache"}:
                value = event.get("detail", {}).get("importance_score")
                if type(value) is int:
                    score = value
                    break
        batches = [b.get("batch_index") for b in inventory.get("model_submitted", {}).get("batches", [])
                   if ident in b.get("article_ids", [])]
        related = [c for c in calls if ident in c.get("article_ids", [])]
        supply = {field: UNKNOWN for field in SUPPLY_FIELDS}
        for observed in events:
            detail = observed.get("detail") or {}
            supply.update({field: detail[field] for field in SUPPLY_FIELDS if field in detail})
        cache_reused = (cache == "cache_hit" if (selected is not None and ident in selected) or
                        (requested is not None and ident in requested) or is_published is True else False)
        model_result = ("cache_reused" if cache_reused and (submitted is not None and ident not in submitted) else
                        "accepted" if validation.get("status") == "kept" else
                        "validation_rejected" if validation.get("status") == "dropped" else
                        last("enrichment").get("reason", UNKNOWN))
        rows.append({"article_id": ident, "trace_id": trace.get("trace_id", UNKNOWN),
                     "source_id": trace.get("source_id") or UNKNOWN,
                     "current_run": True if origin == "current_run_inventory" else False if origin == "historical_inventory" else UNKNOWN,
                     "collection_origin": [e for e in events if e.get("stage") in {"collection", "inventory"}],
                     "supply": supply,
                     "title": trace.get("title") or UNKNOWN, "source": trace.get("source") or UNKNOWN,
                     "original_url": trace.get("original_url") or UNKNOWN,
                     "published_at": trace.get("published_at") or UNKNOWN, "origin": origin,
                     "cache": cache, "cache_reused": cache_reused if cache != UNKNOWN else UNKNOWN,
                     "selected_initial": ident in selected if selected is not None else UNKNOWN,
                     "fallback_requested": any(e.get("stage") == "enrichment" and e.get("detail", {}).get("fallback") for e in events),
                     "model_submitted": ident in submitted if submitted is not None else UNKNOWN,
                     "batch_numbers": batches, "model_result": model_result,
                     "validation": validation or UNKNOWN, "repair": repair or UNKNOWN,
                     "importance_score": score, "publishable": final.get("status", UNKNOWN),
                     "selection_evidence": [e for e in events if e.get("stage", "").startswith("final_")],
                     "published": is_published, "final_reason": reason,
                     "token_usage_per_article": UNKNOWN,
                     "token_basis": "Usage belongs to model requests/batches; article costs are not allocated.",
                     "model_calls": related})
    def pick(name):
        return inventory.get(name, {})
    collection_rows = [_boundary(label, collection.get(a, {}), collection.get(b, {}))
                       for label, a, b in (("Raw Index → SourceItem", "raw_index", "source_item"),
                           ("SourceItem → Current Run Article", "source_item", "current_run_article"),
                           ("Current Run Article → Newly Inserted Article", "current_run_article", "newly_inserted_article"))]
    raw = collection.get("raw_index", {})
    if type(raw.get("observed_dropped_to_source_items")) is int:
        collection_rows[0]["dropped"] = raw["observed_dropped_to_source_items"]
        collection_rows[0]["basis"] = raw.get("accounting_basis", UNKNOWN)
    # SourceItem is an object count; expose its real distinct set separately.
    source = collection.get("source_item", {})
    source_ids = source.get("distinct_article_ids")
    article_ids = _ids(collection.get("current_run_article"))
    if isinstance(source_ids, list) and article_ids is not None and article_ids <= set(source_ids):
        collection_rows[1]["distinct_input"] = len(set(source_ids))
        collection_rows[1]["distinct_dropped"] = len(set(source_ids) - article_ids)
        if len(source_ids) == source.get("count"):
            collection_rows[1]["dropped"] = collection_rows[1]["distinct_dropped"]
    inventory_rows = [_boundary(label, pick(a), pick(b)) for label, a, b in (
        ("Inventory → Eligible Inventory", "inventory_total", "eligible_inventory"),
        ("Eligible Inventory → Candidate Pool", "eligible_inventory", "candidate_pool"),
        ("Candidate Pool → Selected Candidate (initial)", "candidate_pool", "selected_candidate"))]
    stage_rows = []
    for name, observations in stages.items():
        reasons = Counter(e.get("reason", UNKNOWN) for _, e in observations if e.get("status") == "dropped")
        stage_rows.append({"stage": name, "input": len(observations),
                           "kept": sum(e.get("status") == "kept" for _, e in observations),
                           "dropped": sum(e.get("status") == "dropped" for _, e in observations),
                           "other": sum(e.get("status") not in {"kept", "dropped"} for _, e in observations),
                           "reasons": dict(reasons), "basis": "recorded decision occurrences; revisits counted, not a strict funnel"})
    batches = []
    failure = metrics.get("enrichment_failure") or {}
    batch_records = {b.get("batch_id"): b for b in metrics.get("model_batches", [])}
    for batch in inventory.get("model_submitted", {}).get("batches", []):
        ids = batch.get("article_ids", [])
        index = batch.get("batch_index")
        batch_id = f"main-{index}"
        attempts = [c for c in calls if c.get("batch_id") == batch_id]
        successful = [c for c in attempts if isinstance(c.get("usage"), dict)]
        final_call = attempts[-1] if attempts else {}
        recorded = batch_records.get(batch_id, {})
        members = [r for r in rows if r["article_id"] in ids]
        failed = failure.get("batch_index") == index
        batches.append({"batch_id": batch_id, "article_ids": ids, "batch_size": len(ids),
                        "payload_bytes": recorded.get("payload_bytes", final_call.get("payload_bytes", UNKNOWN)),
                        "input_tokens": sum(c["usage"].get("prompt_tokens", 0) for c in successful) if successful else UNKNOWN,
                        "output_tokens": sum(c["usage"].get("completion_tokens", 0) for c in successful) if successful else UNKNOWN,
                        "total_tokens": sum(c["usage"].get("total_tokens", 0) for c in successful) if successful else UNKNOWN,
                        "finish_reason": recorded.get("finish_reason", final_call.get("finish_reason", UNKNOWN)),
                        "clean_text_chars": recorded.get("clean_text_chars", final_call.get("clean_text_chars", UNKNOWN)),
                        "max_output_tokens": recorded.get("max_output_tokens", final_call.get("max_output_tokens", UNKNOWN)),
                        "prompt_tokens": recorded.get("prompt_tokens", final_call.get("usage", {}).get("prompt_tokens", UNKNOWN)),
                        "completion_tokens": recorded.get("completion_tokens", final_call.get("usage", {}).get("completion_tokens", UNKNOWN)),
                        "retry_count": recorded.get("retry_count", max(0, len(attempts)-1) if attempts else UNKNOWN),
                        "response_parse_status": recorded.get("response_parse_status", final_call.get("response_parse_status", UNKNOWN)),
                        "final_batch_status": recorded.get("final_batch_status", UNKNOWN),
                        "validation_passed": sum(isinstance(r["validation"], dict) and r["validation"].get("status") == "kept" for r in members),
                        "repair_attempts": sum(any(e.get("stage") == "repair" and e.get("reason") in {"field_repair_requested", "repair_failed", "repair_succeeded"}
                                                   for e in t.get("events", [])) for t in traces if t.get("article_id") in ids),
                        "repair_successes": sum(isinstance(r["repair"], dict) and r["repair"].get("reason") == "repair_succeeded" for r in members),
                        "failure_type": failure.get("error_summary", UNKNOWN) if failed else None,
                        "publishable_at_failure": failure.get("publishable_at_failure", UNKNOWN) if failed else None,
                        "degraded_daily_entered": failure.get("degraded_daily_entered", UNKNOWN) if failed else None,
                        "attempts": attempts})
    def last_ids(stage, status):
        found = set()
        for trace in traces:
            last = next((e for e in reversed(trace.get("events", [])) if e.get("stage") == stage), None)
            if last and last.get("status") == status:
                found.add(trace.get("article_id"))
        return found
    valid = last_ids("validation", "kept")
    rejected = last_ids("validation", "dropped")
    repair_ok, repair_fail = last_ids("repair", "kept"), last_ids("repair", "dropped")
    repair_requested = {t.get("article_id") for t in traces if any(
        e.get("stage") == "repair" and e.get("reason") == "field_repair_requested" for e in t.get("events", []))}
    repair_proven = repair_requested | {t.get("article_id") for t in traces if any(
        e.get("stage") == "repair" and e.get("reason") in {"repair_failed", "repair_succeeded"} for e in t.get("events", []))}
    ready_input = {t.get("article_id") for t in traces if any(e.get("stage", "").startswith("final_") for e in t.get("events", []))}
    publishable_ids = last_ids("publishable", "kept")
    model_pipeline = [
        {"stage": "DeepSeek (new articles; cache bypass)", "input": len(requested) if requested is not None else UNKNOWN,
         "kept": len(submitted) if submitted is not None else UNKNOWN, "dropped": UNKNOWN,
         "basis": "requested includes cached and guarded candidates; submission is not validation success"},
        {"stage": "Validation (final result after repair)", "input": len(submitted) if submitted is not None else UNKNOWN,
         "kept": len(valid), "dropped": len(rejected),
         "not_reached": len(submitted - valid - rejected) if submitted is not None else UNKNOWN,
         "basis": "last recorded validation per article; batch failures shown separately"},
        {"stage": "Repair (branch)", "input": len(repair_proven), "kept": len(repair_ok),
         "dropped": len(repair_fail - {t.get('article_id') for t in traces if any(e.get('reason') == 'token_guard' and e.get('stage') == 'repair' for e in t.get('events', []))}),
         "basis": "attempts evidenced by request or repair outcome; budget-skipped repair is not an attempt"},
        {"stage": "importance / final selection (including cached inventory)", "input": len(ready_input),
         "kept": len(publishable_ids), "dropped": len(ready_input - publishable_ids), "basis": "all observed final selector inputs; includes cache, not only new model results"},
        {"stage": "Publishable → Published (formal readiness verified)", "input": metrics.get("final_publishable", UNKNOWN),
         "kept": len(published) if published is not None else UNKNOWN,
         "dropped": len(publishable_ids - published) if published is not None else UNKNOWN,
         "basis": "not published is a run/delivery outcome, not another quality filter"},
    ]
    return {"run_id": data.get("run_id", UNKNOWN), "collection": collection_rows,
            "inventory_origins": {"current_run": pick("current_run_inventory").get("count", UNKNOWN),
                                  "historical": pick("historical_inventory").get("count", UNKNOWN)},
            "inventory": inventory_rows, "stages": stage_rows,
            "supply_summary": {key: metrics.get(key, UNKNOWN) for key in SUPPLY_COUNTS},
            "selected_candidates": [r for r in rows if r["selected_initial"] is True or r["fallback_requested"]],
            "candidate_outcomes": [r for r in rows if pool is not None and r["article_id"] in pool],
            "published": [r for r in rows if r["published"] is True],
            "article_outcomes": {r["article_id"]: r for r in rows}, "batches": batches,
            "model_pipeline": model_pipeline,
            "model_calls": calls, "published_count": len(published) if published is not None else UNKNOWN,
            "note": "Repair is a branch, cache bypasses DeepSeek, fallback extends initial selection; these are not strictly decreasing steps."}


def inspection_html(view):
    """Portable, escaped local audit view using native details, no external assets."""
    def table(rows, fields):
        header = ''.join('<th>'+escape(f)+'</th>' for f in fields)
        body = ''.join('<tr>'+''.join('<td>'+escape(str(r.get(f, UNKNOWN)))+'</td>' for f in fields)+'</tr>' for r in rows)
        return '<div class="audit-scroll"><table><thead><tr>'+header+'</tr></thead><tbody>'+body+'</tbody></table></div>'
    sections = ['<h2>漏斗总览</h2><p>'+escape(view['note'])+'</p><h3>采集链路</h3>',
                '<h3>Today / Reserve</h3><pre>'+escape(json.dumps(view['supply_summary'],ensure_ascii=False,indent=2))+'</pre>',
                table(view['collection'], ('stage','input','kept','dropped','basis')),
                '<h3>库存 / 筛选链路</h3><p>'+escape(str(view['inventory_origins']))+'</p>',
                table(view['inventory'], ('stage','input','kept','dropped','basis')),
                '<h3>模型 / 发布</h3>', table(view['model_pipeline'], ('stage','input','kept','dropped','not_reached','basis')),
                table(view['batches'], ('batch_id','batch_size','payload_bytes','input_tokens','output_tokens','total_tokens','finish_reason','validation_passed','repair_attempts','repair_successes','failure_type','publishable_at_failure','degraded_daily_entered')),
                '<h2>淘汰原因统计</h2><p>以下为实际决策次数；同一文章可能在不同阶段出现，不能相加当作独立淘汰文章数。</p>',
                table(view['stages'], ('stage','input','kept','dropped','other','reasons'))]
    for batch in view['batches']:
        sections.append('<details><summary>'+escape(batch['batch_id'])+' · 模型请求与文章 IDs</summary><pre>'+escape(json.dumps(batch,ensure_ascii=False,indent=2))+'</pre></details>')
    for label,key in (('Selected Candidate（含补位）','selected_candidates'), ('为什么候选没有进入日报','candidate_outcomes'), ('最终日报来源解释','published')):
        sections.append('<details><summary>'+label+' · '+str(len(view[key]))+'</summary>')
        for row in view[key]:
            sections.append('<details><summary>'+escape(row['title'])+' · '+escape(row['final_reason'])+'</summary><a href="#'+escape(row['trace_id'],quote=True)+'">完整 Article Trace</a><pre>'+escape(json.dumps(row,ensure_ascii=False,indent=2))+'</pre></details>')
        sections.append('</details>')
    return ''.join(sections)
