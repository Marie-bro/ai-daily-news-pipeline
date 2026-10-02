"""One index plus at most one detail per affected source; no production run or SQLite writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_daily_pipeline.sources import collect_source, fetch_response, load_sources
from ai_daily_pipeline.extract import extract_article
from ai_daily_pipeline.diagnostics import failure_details

IDS = {'cas-research', 'miit-policy', 'moe-updates', 'national-statistics', 'most-updates',
       'sciencenet-news', 'jiemian-industry', 'baidu-hotlist'}


def check(source):
    started = time.monotonic()
    row = {'source_id': source.source_id, 'url': source.url, 'fetch_method': source.fetch_method,
           'proxy_mode': source.proxy_mode, 'timeout_seconds': source.request_timeout_seconds,
           'max_attempts': source.request_attempts}
    try:
        collection = collect_source(source)  # No cache/store argument.
        row.update(status=collection.status, items=len(collection.items), index_duration_ms=round((time.monotonic()-started)*1000, 2),
                   request_history=list(collection.request_history))
        if source.source_role == 'discovery':
            row['detail_check'] = 'not_applicable_discovery'
        elif collection.items:
            item = collection.items[0]
            detail_start = time.monotonic()
            response = fetch_response(item.url, **source.fetch_options())
            extracted = extract_article(response.body, item.title, source.cleaning)
            row['detail_check'] = {'url': item.url, 'body_chars': len(extracted.clean_text),
                                   'published_at': extracted.published_at.isoformat() if extracted.published_at else None,
                                   'duration_ms': round((time.monotonic()-detail_start)*1000, 2),
                                   'request_history': list(response.request_history)}
    except Exception as exc:
        row['failure'] = failure_details(exc, 'collection')
        row['failed_request_history'] = list(getattr(exc, 'source_request_history', ()))
        if 'status' not in row:
            row['status'] = 'failed'
    row['total_duration_ms'] = round((time.monotonic()-started)*1000, 2)
    return row


if __name__ == '__main__':
    sources = [s for s in load_sources(ROOT / 'config/sources.json') if s.source_id in IDS]
    with ThreadPoolExecutor(max_workers=4) as executor:  # Diagnostic concurrency only.
        rows = list(executor.map(check, sources))
    payload = {'checked_at': datetime.now(UTC).isoformat(), 'production_run': False, 'sqlite_written': False,
               'model_calls': 0, 'probes': rows}
    target = ROOT / 'data/source-network-diagnostics-2026-10-02/post-fix.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
