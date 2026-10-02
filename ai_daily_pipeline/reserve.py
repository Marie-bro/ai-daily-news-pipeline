"""Bounded offline Reserve seeds, using existing Article/cache evidence only."""
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .models import Article, Enrichment
from .supply import editorial_candidate, normalized_url, same_event
from .supply_lock import classify_supply_layer, historical_age_days

VALIDATOR_VERSION = 'shared-fact-deterministic-reserve-v1'
RESERVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS article_reserve (
 article_id TEXT PRIMARY KEY REFERENCES articles(id),
 reserve_type TEXT, reserve_status TEXT NOT NULL, reserve_reason TEXT NOT NULL,
 supply_layer TEXT, published_at TEXT NOT NULL, historical_age_days REAL NOT NULL,
 policy_related INTEGER NOT NULL, policy_validity TEXT NOT NULL,
 validity_start TEXT, validity_end TEXT, superseded INTEGER NOT NULL DEFAULT 0, replaced_by TEXT,
 policy_evidence_url TEXT, eligibility_evidence_json TEXT NOT NULL,
 review_evidence_json TEXT NOT NULL,
 deep_read INTEGER NOT NULL, evergreen_score REAL,
 cache_available INTEGER NOT NULL, validation_status TEXT NOT NULL, importance_score INTEGER,
 content_fingerprint TEXT, enrichment_hash TEXT, validator_version TEXT,
 first_reserved_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
 used_in_daily INTEGER NOT NULL DEFAULT 0, used_at TEXT
);
"""

SHORT_LIVED = re.compile(r'flash sale|discount|promotion|stock price|shares (?:rose|fell)|outage|security incident|'
    r'launch event|product launch|announces? (?:a |the )?new|发布会|促销|股价|临时故障|安全事件|限时|抽奖', re.I)

def deep_read_evidence(article):
    """Body evidence, not headline keywords or a length-only test."""
    text = article.clean_text
    if len(text) < 1500 or article.source_tier > 2:
        return {}  # Preserve the existing Deep Read source/length floor; length alone is insufficient.
    patterns = {
        'mechanism': r'works by|underlying mechanism|principles? of|工作原理|作用机制|推导过程',
        'method': r'methodology|implementation details|step.by.step|工程实践|实现步骤|具体方法',
        'code': r'```|\bdef \w+\(|\bfunction \w+\(|pip install|代码示例',
        'comparative_evidence': r'comparative analysis|experimental results|systematic review|实验结果|系统综述|对比分析',
    }
    matches = {kind: match.group(0) for kind, pattern in patterns.items()
               if (match := re.search(pattern, text, re.I))}
    steps = re.findall(r'(?:step|步骤)\s*(\d+|[一二三四])', text, re.I)
    if len(set(steps)) >= 2:
        matches['procedural_steps'] = sorted(set(steps))
    return matches if len(matches) >= 2 else {}


def validate_cache(article, enrichment):
    # Reuse the exact production validator; no repair/model invocation, no fake version claims.
    from .enrich import TASK_NAME, _build_enrichment
    if enrichment is None:
        return None, 'cache_missing'
    if enrichment.task != TASK_NAME or not enrichment.model or not enrichment.generated_at:
        return None, 'cache_version_incompatible'
    if (enrichment.article_id != article.id or enrichment.original_url != article.original_url
            or enrichment.published_at != article.published_at or enrichment.source != article.source
            or enrichment.title_original != article.original_title):
        return None, 'cache_metadata_mismatch'
    try:
        data = enrichment.to_dict()
        data['fact_schema'] = json.loads(enrichment.fact_schema_json)
        validated = _build_enrichment(data, article, enrichment.model, enrichment.generated_at)
    except (ValueError, TypeError, RuntimeError, KeyError):
        return None, 'cache_validation_failed'
    return validated, None


def build_reserve_seed(articles, cache, past, now, existing=None):
    """Pure preview: eligible, pending_review and rejected metadata; never calls a model."""
    from .enrich import _eligible_content
    existing = existing or {}
    records = []
    for a in articles:
        age = historical_age_days(a, now)
        if age <= 1:
            continue  # Today is not a Reserve seed, regardless of collection origin.
        prior = existing.get(a.id, {})
        e, cache_error = validate_cache(a, cache.get(a.id))
        policy_related = a.category in {'policy','education','employment'} or a.channel == 'policy_economy' and a.source_role == 'primary'
        evidence = deep_read_evidence(a)
        digest = hashlib.sha256(json.dumps(cache[a.id].to_record(), sort_keys=True, ensure_ascii=False).encode()).hexdigest() if a.id in cache else None
        r = dict(article_id=a.id, reserve_type='policy' if policy_related else 'deep_read' if evidence else 'catch_up',
            reserve_status='eligible', reserve_reason='within_catchup_window', supply_layer='catch_up' if age<=3 else None,
            published_at=a.published_at, historical_age_days=round(age,6),
            policy_related=policy_related, policy_validity=prior.get('policy_validity','unknown'),
            validity_start=prior.get('validity_start'), validity_end=prior.get('validity_end'),
            superseded=bool(prior.get('superseded')), replaced_by=prior.get('replaced_by'),
            policy_evidence_url=prior.get('policy_evidence_url'), eligibility_evidence_json=json.dumps(evidence,ensure_ascii=False),
            review_evidence_json=json.dumps({k:prior[k] for k in ('policy_checked_at','policy_validity_evidence',
                'current_applicability_verified','applicability_evidence') if k in prior},ensure_ascii=False),
            deep_read=bool(evidence), evergreen_score=None, cache_available=a.id in cache,
            validation_status='passed' if e else 'failed' if cache_error=='cache_validation_failed' else 'unavailable',
            importance_score=e.importance_score if e else None, content_fingerprint=a.fingerprint,
            enrichment_hash=digest, validator_version=VALIDATOR_VERSION if e else None,
            first_reserved_at=prior.get('first_reserved_at') or now.isoformat(), last_checked_at=now.isoformat(),
            used_in_daily=bool(prior.get('used_in_daily')), used_at=prior.get('used_at'))
        def reject(reason, pending=False):
            r.update(reserve_status='pending_review' if pending else 'rejected',reserve_reason=reason)
        published = next((x for x in past if x.get('article_id')==a.id
            or x.get('fingerprint')==a.fingerprint or normalized_url(x['original_url'])==normalized_url(a.original_url)
            or same_event(a.title,x.get('title_original',x.get('title_en','')))),None)
        if published or r['used_in_daily']:
            reject('previously_published')
            r.update(used_in_daily=True,used_at=r['used_at'] or (published or {}).get('daily_published_at'))
        elif age > 365:
            reject('outside_reserve_design_window')
        elif not _eligible_content(a) or not editorial_candidate(a):
            reject('existing_quality_filter')
        elif cache_error:
            reject(cache_error,True)
        elif prior.get('content_fingerprint') not in (None,a.fingerprint):
            reject('content_fingerprint_changed',True)
        elif prior.get('enrichment_hash') not in (None,digest):
            reject('cache_content_changed',True)
        elif e.importance_score < 60:
            reject('importance_below_60')
        elif r['policy_validity'] in {'expired','superseded'} or r['superseded'] or r['replaced_by']:
            reject('policy_expired_or_superseded')
        elif r['validity_end'] and _expired(r['validity_end'],now):
            reject('policy_expired')
        elif age <= 3:
            pass
        elif SHORT_LIVED.search(a.title + ' ' + e.what_happened_en + ' ' + e.what_happened):
            reject('short_lived_content')
        elif policy_related:
            r.update(supply_layer='evergreen',reserve_type='policy')
            if r['policy_validity'] != 'valid':
                reject('policy_validity_unknown',True)
            elif (a.source_role != 'primary' or a.source_tier != 1 or
                  not _policy_evidence_valid(prior,now,a)):
                reject('policy_validity_evidence_missing_or_stale',True)
            else:
                r['reserve_reason']='verified_valid_policy'
        elif evidence and age <= 30:
            r.update(supply_layer='deep_read',reserve_type='deep_read',reserve_reason='body_supported_long_read')
        elif evidence and prior.get('current_applicability_verified') is True and prior.get('applicability_evidence'):
            r.update(supply_layer='evergreen',reserve_type='long_term_read',reserve_reason='reviewed_long_term_read')
        else:
            reject('long_term_value_not_proven',True)
        records.append(r)
    eligible=[r for r in records if r['reserve_status']=='eligible']
    summary=dict(total_considered=len(records),eligible=len(eligible),
        pending_review=sum(r['reserve_status']=='pending_review' for r in records),
        rejected=sum(r['reserve_status']=='rejected' for r in records),
        rejection_reasons=dict(Counter(r['reserve_reason'] for r in records if r['reserve_status']!='eligible')),
        supply_layers=dict(Counter(r['supply_layer'] for r in eligible)),
        cache_reuse=len(eligible),previously_published_excluded=sum(r['reserve_reason']=='previously_published' for r in records))
    by_id={a.id:a for a in articles}
    summary['source_distribution']=dict(Counter(by_id[r['article_id']].source for r in eligible))
    summary['age_distribution']=dict(Counter('1-3d' if r['historical_age_days']<=3 else '3-7d' if r['historical_age_days']<=7 else '7-30d' if r['historical_age_days']<=30 else '30-365d' for r in eligible))
    return {'generated_at':now.isoformat(),'summary':summary,'items':records}


def _expired(value,now):
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return True  # Ambiguous validity is never automatically accepted.
        return stamp <= now
    except (ValueError,TypeError):
        return True


def _policy_evidence_valid(prior,now,article):
    url=prior.get('policy_evidence_url','') or ''
    try:
        stamp=datetime.fromisoformat(prior.get('policy_checked_at',''))
        start=datetime.fromisoformat(prior['validity_start']) if prior.get('validity_start') else None
        host=urlparse(url).hostname or ''
        return (urlparse(url).scheme=='https' and (host==urlparse(article.original_url).hostname or host.endswith('.gov.cn'))
            and bool(prior.get('policy_validity_evidence')) and stamp.tzinfo is not None
            and 0 <= (now-stamp).total_seconds() <= 7*86400
            and (start is None or start.tzinfo is not None and start <= now))
    except (ValueError,TypeError):
        return False


def read_seed_inventory(database, now, limit=100):
    """Read-only, bounded rows. No ArticleStore constructor or schema initialization."""
    if not 1<=limit<=300:
        raise ValueError('reserve preview limit must be 1..300')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        columns={r[1] for r in db.execute('PRAGMA table_info(tech_enrichments)')}
        rows=db.execute('SELECT a.* FROM articles a LEFT JOIN tech_enrichments e ON e.article_id=a.id '
                        'ORDER BY (e.article_id IS NOT NULL) DESC, a.published_at DESC LIMIT ?', (limit,)).fetchall()
        articles=[Article(**dict(r)) for r in rows]
        cache={}
        for a in articles:
            row=db.execute('SELECT * FROM tech_enrichments WHERE article_id=?',(a.id,)).fetchone()
            if row and {'title_en','what_happened_en','why_it_matters_en','fact_schema_json'}<=columns:
                cache[a.id]=Enrichment(**dict(row))
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_reserve'").fetchone()
        existing={r['article_id']:decode_reserve_record(r) for r in db.execute('SELECT * FROM article_reserve')} if exists else {}
    return articles,cache,existing


def decode_reserve_record(row):
    value=dict(row)
    value.update(json.loads(value.get('review_evidence_json') or '{}'))
    return value


def write_seed_metadata(connection, preview):
    """Only the independent Reserve table is written. Article, cache, usage stay unchanged."""
    connection.executescript(RESERVE_SCHEMA)
    for row in preview['items']:
        columns=list(row)
        updates=', '.join(f'{key}=excluded.{key}' for key in columns if key not in {'article_id','first_reserved_at'})
        connection.execute(f"INSERT INTO article_reserve ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                           f"ON CONFLICT(article_id) DO UPDATE SET {updates}", [row[key] for key in columns])
    connection.commit()
