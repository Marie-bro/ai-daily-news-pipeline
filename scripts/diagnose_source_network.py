"""Small read-only index probes; no collection run, cache, model, publish or send."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, getproxies

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_daily_pipeline.sources import ADAPTERS, USER_AGENT, load_sources

IDS = {'cas-research', 'miit-policy', 'moe-updates', 'national-statistics', 'most-updates',
       'sciencenet-news', 'jiemian-industry', 'baidu-hotlist'}


def probe(source, mode, output):
    started = time.monotonic()
    result = {'source_id': source.source_id, 'url': source.url, 'mode': mode,
              'fetch_method': source.fetch_method, 'timestamp': datetime.now(UTC).isoformat(),
              'http_status': None, 'failure_phase': None, 'response_size': None}
    phase = 'request_via_system_route' if mode == 'system' else 'dns'
    connection = None
    try:
        if mode == 'direct':
            target = urlsplit(source.url)
            mark = time.monotonic()
            addresses = socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)
            result.update(dns_ms=round((time.monotonic()-mark)*1000, 2),
                          dns_addresses=sorted({a[4][0] for a in addresses}))
            phase, mark = 'tcp_connect', time.monotonic()
            raw_socket = socket.create_connection((target.hostname, 443), timeout=12)
            result['tcp_ms'] = round((time.monotonic()-mark)*1000, 2)
            phase, mark = 'tls_handshake', time.monotonic()
            try:
                tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=target.hostname)
            except BaseException:
                raw_socket.close()
                raise
            result['tls_ms'] = round((time.monotonic()-mark)*1000, 2)
            connection = http.client.HTTPSConnection(target.hostname, timeout=12)
            connection.sock = tls_socket
            phase = 'request_headers_body'
            connection.request('GET', (target.path or '/') + ('?' + target.query if target.query else ''),
                               headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xml,*/*;q=0.2'})
            phase, mark = 'response_headers', time.monotonic()
            response = connection.getresponse()
            result.update(http_status=response.status, headers_wait_ms=round((time.monotonic()-mark)*1000, 2),
                          final_url=source.url, redirect_location=response.getheader('Location'),
                          response_headers={k: v for k, v in response.getheaders() if k.lower() in
                                            {'server', 'content-type', 'location', 'cf-ray', 'via', 'content-encoding'}})
            charset = response.headers.get_content_charset() or 'utf-8'
        else:
            response = urlopen(Request(source.url, headers={'User-Agent': USER_AGENT}), timeout=12)
            result.update(http_status=response.status, final_url=response.geturl(),
                          dns_addresses='unknown_proxy_route', dns_ms=None, tcp_ms=None, tls_ms=None,
                          response_headers={k: v for k, v in response.headers.items() if k.lower() in
                                            {'server', 'content-type', 'location', 'cf-ray', 'via', 'content-encoding'}})
            charset = response.headers.get_content_charset() or 'utf-8'
        phase = 'response_body'
        with response:
            first = response.read(1)
            result['first_byte_ms'] = round((time.monotonic()-started)*1000, 2)
            raw = first + response.read(2_000_000)
        result['response_size'] = len(raw)
        if len(raw) > 2_000_000:
            phase = 'size_limit'
            raise ValueError('Source response exceeds 2 MB; sample truncated')
        text = raw.decode(charset, errors='replace')
        output.mkdir(parents=True, exist_ok=True)
        (output / f'{source.source_id}-{mode}.html').write_text(text, encoding='utf-8')
        phase = 'parser'
        decisions = []
        items = ADAPTERS[source.adapter].parse(source, text, on_decision=lambda *a: decisions.append(a))
        from collections import Counter
        result.update(parsed_items=len(items), parser_reasons=dict(Counter(a[-1] for a in decisions)),
                      sample_urls=[item.url for item in items[:3]],
                      js_required_for_current_parser=False if items else 'unknown',
                      waf_evidence=[term for term in ('captcha', 'challenge-platform', 'cf-chl', '访问验证', '访问过于频繁') if term in text.lower()])
    except Exception as exc:
        result.update(failure_phase=phase, exception_type=type(exc).__name__, error=str(exc)[:240])
        if isinstance(exc, HTTPError):
            result['http_status'] = exc.code
    finally:
        if connection:
            connection.close()
    result['duration_ms'] = round((time.monotonic()-started)*1000, 2)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='append')
    parser.add_argument('--mode', choices=['system', 'direct', 'both'], default='both')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/source-network-diagnostics-2026-10-02')
    args = parser.parse_args()
    sources = [s for s in load_sources(ROOT / 'config/sources.json') if s.source_id in set(args.source or IDS)]
    modes = ['system', 'direct'] if args.mode == 'both' else [args.mode]
    def safe_proxy(value):
        parsed = urlsplit(value if '://' in value else 'http://' + value)
        return {'host': parsed.hostname, 'port': parsed.port}
    proxy = {k: safe_proxy(v) for k, v in getproxies().items() if k != 'no'}
    jobs = [(s, m) for s in sources for m in modes]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda j: probe(*j, args.output), jobs))
    payload = {'proxy_endpoints': proxy, 'probes': results}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'results.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
