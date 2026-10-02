"""Deterministic supply policy: no model calls and no invented factual scores."""
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT = {"minimum": 10, "target": 14, "maximum": 18, "explore_minimum": 2,
           "personal_categories": ["ai", "robotics"], "major_score": 85,
           "category_share": 0.65, "media_limit": 2, "source_share": 0.25}
DEEP = re.compile(r"tutorial|how to|deep dive|analysis|lessons|review|report|study|research|paper|\u6559\u7a0b|\u5b9e\u64cd|\u7814\u7a76|\u8bba\u6587|\u62a5\u544a|\u590d\u76d8|\u6df1\u5ea6", re.I)
IMPACT = re.compile(r"breakthrough|first|critical|vulnerability|launch|release|discovery|clinical|\u7a81\u7834|\u9996\u6b21|\u91cd\u5927|\u6f0f\u6d1e|\u53d1\u5e03|\u4e34\u5e8a", re.I)

LOW_VALUE = re.compile(r"APOD:|image of the day|high school.*challenge|middle school.*challenge|named.*auditor|named.*top university|innovation fellow|hall of fame|showroom|new exhibition|podcast:|early careers|humanist lens|finding purpose|luxury yacht", re.I)

def editorial_candidate(article):
    return not LOW_VALUE.search(article.title)

def policy(root):
    path = root / "config" / "supply.json"
    value = {**DEFAULT, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}
    if not 10 <= value["minimum"] <= value["target"] <= value["maximum"] <= 18:
        raise ValueError("invalid daily supply limits")
    return value

def normalized_url(url):
    return url.split("#", 1)[0].rstrip("/")

def same_event(a, b):
    # Different explicit versions are a material update; mere headline rewording is not.
    versions = lambda s: set(re.findall(r"\bv?(\d+\.\d+)(?:\.\d+)?\b", s.lower()))
    if versions(a) and versions(b) and versions(a) != versions(b):
        return False
    clean = lambda s: re.sub(r"[^\w]", "", s.casefold())
    return SequenceMatcher(None, clean(a), clean(b)).ratio() >= .78

def history(site_root, now):
    items = []
    for path in (site_root / "data/daily/ai").glob("*.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("report_date", "9999") <= now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
            items.extend(report.get("items", []))
    return items

def deep_read(article):
    return bool(DEEP.search(article.title)) and len(article.clean_text) >= 1500 and article.source_tier <= 2

def level(article, now):
    age = (now - datetime.fromisoformat(article.published_at)).total_seconds() / 3600
    if age < -1/6: return None
    if age <= 24: return 1 if article.source_tier <= 2 else 2
    if age <= 72: return 3
    if age <= 168 and deep_read(article): return 4
    return None

def select(articles, now, rules, past=(), enrichments=None, limit=None, on_decision=None, supply_layers=None):
    enrichments = enrichments or {}
    urls = {normalized_url(x["original_url"]) for x in past}
    titles = [x.get("title_original", x.get("title_en", "")) for x in past]
    pool = []
    past_fingerprints = {x.get("fingerprint") for x in past}
    past_urls = {normalized_url(x["original_url"]): x for x in past}
    def emit(article, stage, status, reason, detail=None):
        if on_decision is not None:
            on_decision(article, stage, status, reason, detail or {})
    for a in articles:
        stage = level(a, now)
        if supply_layers and supply_layers.get(a.id) in {"deep_read", "evergreen"}:
            stage = 4  # Only prevalidated Reserve metadata may extend the historical window.
        if not editorial_candidate(a):
            emit(a, "quality_filter", "dropped", "editorial_exclusion")
            continue
        if not stage:
            age = (now - datetime.fromisoformat(a.published_at)).total_seconds() / 3600
            reason = "future_publication_time" if age < -1/6 else "outside_7_day_window" if age > 168 else "older_than_72h_not_deep_read"
            emit(a, "freshness", "dropped", reason, {"age_hours": round(age, 2), "published_at": a.published_at})
            continue
        emit(a, "freshness", "kept", f"level_{stage}", {"published_at": a.published_at})
        if a.source_tier > 3:
            emit(a, "quality_filter", "dropped", "tier_above_3", {"tier": a.source_tier})
            continue
        if a.source_role == "discovery":
            emit(a, "quality_filter", "dropped", "discovery_not_factual_source")
            continue
        if a.verification_status != "source_verified":
            emit(a, "quality_filter", "dropped", "source_not_verified")
            continue
        if a.fingerprint in past_fingerprints:
            emit(a, "historical_dedup", "dropped", "historical_fingerprint")
            continue
        if normalized_url(a.original_url) in urls:
            emit(a, "historical_dedup", "dropped", "historical_url", {"duplicate_of": past_urls[normalized_url(a.original_url)].get("article_id")})
            continue
        past_event = next((x for x in past if same_event(a.title, x.get("title_original", x.get("title_en", "")))), None)
        if past_event is not None:
            emit(a, "historical_dedup", "dropped", "historical_event", {"duplicate_of": past_event.get("article_id")})
            continue
        e = enrichments.get(a.id)
        importance = e.importance_score if e else 60 + 15 * bool(IMPACT.search(a.title))
        if e and importance < 60:
            emit(a, "quality_filter", "dropped", "enriched_importance_below_60", {"importance_score": importance})
            continue
        relevance = 100 if a.category in rules["personal_categories"] else 0
        score = .6 * importance + .2 * {1:100, 2:85, 3:65}[a.source_tier] + 10 + .1 * relevance
        pool.append((a, stage, score, importance))
        emit(a, "candidate_pool", "kept", "eligible", {"level": stage, "importance_score": importance})
    selected = []
    seen_fp = set()
    seen_url = set()
    stats = []
    deferred = []
    in_run_reasons = {}
    cap = min(limit or rules["maximum"], rules["target"])
    if sum(x[3] >= rules["major_score"] for x in pool if x[1] <= 2) >= 10:
        cap = min(limit or rules["maximum"], rules["maximum"])
    source_limit = max(1, int(cap * float(rules.get("source_share", .25))))
    for stage in (1,2,3,4):
        candidates = [x for x in pool if x[1] == stage]
        while candidates and len(selected) < cap:
            categories = Counter(x[0].category for x in selected)
            sources = Counter(x[0].source for x in selected)
            explore = sum(x[0].category not in rules["personal_categories"] for x in selected)
            def ordering(x):
                a, _, score, importance = x
                diversity = (20 if explore < rules["explore_minimum"] and a.category not in rules["personal_categories"] else 0)
                penalty = 0 if importance >= rules["major_score"] else (25 if categories[a.category] >= max(1,int(cap*rules["category_share"])) else 0) + (25 if a.source_tier > 1 and sources[a.source] >= rules["media_limit"] else 0) + (30 if sources[a.source] >= source_limit else 0)
                return (importance >= rules["major_score"], int(importance // 10), score + diversity - penalty, a.published_at, a.id)
            x = max(candidates, key=ordering); candidates.remove(x)
            a = x[0]
            duplicate = next((b[0] for b in selected if a.fingerprint == b[0].fingerprint), None)
            reason = "duplicate_fingerprint"
            if duplicate is None:
                duplicate = next((b[0] for b in selected if normalized_url(a.original_url) == normalized_url(b[0].original_url)), None)
                reason = "duplicate_url"
            if duplicate is None:
                duplicate = next((b[0] for b in selected if same_event(a.title, b[0].title)), None)
                reason = "duplicate_event"
            if duplicate is not None:
                in_run_reasons[a.id] = (reason, {"duplicate_of": duplicate.id})
                continue
            if x[3] < rules["major_score"] and ((a.source_tier > 1 and sources[a.source] >= rules["media_limit"]) or sources[a.source] >= source_limit or categories[a.category] >= max(1, int(cap * rules["category_share"]))):
                deferred.append(x)
                continue
            selected.append(x); seen_fp.add(a.fingerprint); seen_url.add(normalized_url(a.original_url))
        stats.append({"level":stage,"eligible":sum(x[1]==stage for x in pool),"selected_total":len(selected)})
        if len(selected) >= rules["target"]: break
    # Diversity is soft only when no qualified alternative exists; expose the exception.
    diversity_exception = False
    for x in sorted(deferred, key=lambda x:x[2], reverse=True):
        if len(selected) >= min(cap, rules["minimum"]): break
        a = x[0]
        if a.fingerprint in seen_fp:
            in_run_reasons[a.id] = ("duplicate_fingerprint", {"phase": "diversity_fill"})
            continue
        if normalized_url(a.original_url) in seen_url:
            in_run_reasons[a.id] = ("duplicate_url", {"phase": "diversity_fill"})
            continue
        if any(same_event(a.title,b[0].title) for b in selected):
            in_run_reasons[a.id] = ("duplicate_event", {"phase": "diversity_fill"})
            continue
        selected.append(x); seen_fp.add(a.fingerprint); seen_url.add(normalized_url(a.original_url))
        diversity_exception = True
    if any(count > source_limit for count in Counter(a.source for a, *_ in selected).values()):
        diversity_exception = True
    selected_ids = {a.id for a, *_ in selected}
    deferred_ids = {a.id for a, *_ in deferred}
    for a, stage, score, importance in pool:
        if a.id in selected_ids:
            emit(a, "selection", "kept", "ranked_selection", {"level": stage, "importance_score": importance, "ranking_score": round(score, 2)})
        else:
            processed_levels = {step["level"] for step in stats}
            default_reason = ("diversity_deferred_not_used" if a.id in deferred_ids else
                              "target_met_before_level" if stage not in processed_levels else "selection_capacity")
            reason, detail = in_run_reasons.get(a.id, (default_reason, {"level": stage, "cap": cap}))
            emit(a, "selection", "dropped", reason, detail)
    metadata = {}
    for a, stage, score, importance in selected:
        section = "deep_read" if deep_read(a) else "major_tech" if importance >= rules["major_score"] else "for_you" if a.category in rules["personal_categories"] else "explore"
        channel = "deep_read" if deep_read(a) else a.channel
        metadata[a.id] = {"supply_level":stage,"section":section,"ranking_score":round(score,2),
                          "content_type":"deep_read" if deep_read(a) else "news", "catch_up":stage==3,
                          "fingerprint":a.fingerprint, "article_id":a.id,
                          "source_region":a.source_region,"source_tier":a.source_tier,
                          "source_role":a.source_role,"channel":channel}
    shortfall = max(0, rules["minimum"] - len(selected))
    return [x[0] for x in selected], metadata, {"diversity_exception":diversity_exception,"levels":stats,"selected":len(selected),"shortfall":shortfall,"minimum_not_met_reason":"all real source-verified candidates were exhausted" if shortfall else None,"explore_count":sum(a.category not in rules["personal_categories"] for a,*_ in selected),"sources":len({a.source for a,*_ in selected}),"categories":dict(Counter(a.category for a,*_ in selected)),"channels":dict(Counter(("deep_read" if deep_read(a) else a.channel) for a,*_ in selected)),"source_counts":dict(Counter(a.source for a,*_ in selected)),"source_share_limit":source_limit,"deep_read_candidates":sum(deep_read(a) for a, *_ in pool),"score_mode":"semantic importance when cached; heuristic preselection otherwise"}
