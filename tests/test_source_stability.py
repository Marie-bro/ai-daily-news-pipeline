from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
import json
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ai_daily_pipeline.pipeline import _candidate_to_article, run_collection
from ai_daily_pipeline.sources import (ADAPTERS, FetchResponse, SourceDefinition,
    _AllowedRedirect, collect_source, fetch_response, load_sources)
from ai_daily_pipeline.store import ArticleStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, tzinfo=UTC)


class Response:
    status = 200
    def __init__(self, body=b'<rss/>', url='https://example.com/feed'):
        self.body, self.url = body, url
        self.headers = Message()
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def geturl(self): return self.url
    def read(self, limit): return self.body[:limit]


class SourceStabilityTests(unittest.TestCase):
    def source(self, **values):
        return replace(SourceDefinition('one', 'Official', 'official_blog', 'rss',
            'https://example.com/feed', ('example.com',), 1), **values)

    def direct(self, side_effect):
        opener = Mock()
        opener.open.side_effect = side_effect
        return opener

    def test_direct_success_has_history_and_bypasses_system_proxy(self):
        opener = self.direct([Response()])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener) as build, \
             patch('ai_daily_pipeline.sources.urllib.request.urlopen') as system:
            result = fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(build.call_args.args[0].proxies, {})
        system.assert_not_called()
        self.assertEqual(result.request_history[0]['http_status'], 200)
        self.assertGreaterEqual(result.request_history[0]['elapsed_ms'], 0)

    def test_system_route_default_unchanged(self):
        with patch('ai_daily_pipeline.sources.urllib.request.urlopen', return_value=Response()) as fetch, \
             patch('ai_daily_pipeline.sources.urllib.request.build_opener') as build:
            fetch_response(self.source().url)
        fetch.assert_called_once()
        build.assert_not_called()

    def test_timeout_then_success_and_new_opener_per_attempt(self):
        first, second = self.direct([TimeoutError('timed out')]), self.direct([Response()])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', side_effect=[first, second]) as build, \
             patch('ai_daily_pipeline.sources.time.sleep'):
            result = fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(build.call_count, 2)
        self.assertEqual([r['error_kind'] for r in result.request_history], ['timeout', None])

    def test_tls_eof_then_success(self):
        opener = self.direct([URLError(ssl.SSLEOFError('EOF')), Response()])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), patch('ai_daily_pipeline.sources.time.sleep'):
            result = fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(result.request_history[0]['error_kind'], 'tls')

    def test_timeout_limit_keeps_history(self):
        opener = self.direct([URLError(TimeoutError('timed out'))]*2)
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), patch('ai_daily_pipeline.sources.time.sleep'), \
             self.assertRaises(URLError) as context:
            fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(len(context.exception.source_request_history), 2)
        self.assertEqual(opener.open.call_count, 2)

    def test_http_403_not_retried(self):
        opener = self.direct([HTTPError(self.source().url, 403, 'Forbidden', Message(), None)])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), self.assertRaises(HTTPError):
            fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(opener.open.call_count, 1)

    def test_http_429_retried(self):
        self.check_http_retry(429)

    def test_http_503_retried(self):
        self.check_http_retry(503)

    def check_http_retry(self, code):
        opener = self.direct([HTTPError(self.source().url, code, 'Temporary', Message(), None), Response()])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), patch('ai_daily_pipeline.sources.time.sleep'):
            result = fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual([r['http_status'] for r in result.request_history], [code, 200])

    def test_certificate_failure_not_retried_or_bypassed(self):
        opener = self.direct([URLError(ssl.SSLCertVerificationError('invalid certificate'))])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), self.assertRaises(URLError):
            fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(opener.open.call_count, 1)

    def test_redirect_accepts_same_host_and_rejects_other_host_before_request(self):
        handler = _AllowedRedirect(('example.com',))
        request = Request(self.source().url)
        self.assertEqual(handler.redirect_request(request, None, 302, '', Message(), 'https://example.com/new').full_url, 'https://example.com/new')
        for url in ('https://other.example/new', 'http://example.com/new'):
            with self.assertRaisesRegex(ValueError, 'redirected outside'):
                handler.redirect_request(request, None, 302, '', Message(), url)

    def test_read_timeout_is_retried_for_direct_only(self):
        bad = Response()
        bad.read = Mock(side_effect=TimeoutError('read timeout'))
        opener = self.direct([bad, Response()])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), patch('ai_daily_pipeline.sources.time.sleep'):
            result = fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(result.request_history[0]['failure_phase'], 'response_body')
        with patch('ai_daily_pipeline.sources.urllib.request.urlopen', return_value=bad) as system, self.assertRaises(TimeoutError):
            fetch_response(self.source().url)
        self.assertEqual(system.call_count, 1)

    def test_size_limit_not_retried(self):
        opener = self.direct([Response(b'x'*2_000_001)])
        with patch('ai_daily_pipeline.sources.urllib.request.build_opener', return_value=opener), self.assertRaisesRegex(ValueError, '2 MB'):
            fetch_response(self.source().url, proxy_mode='direct')
        self.assertEqual(opener.open.call_count, 1)

    def test_empty_and_selector_mismatch_are_observable(self):
        for body, reason in [('<html/>', None), ('<a href="https://example.com/wrong">Quantum computing research advances</a>', 'article_path_pattern_mismatch')]:
            events = []
            source = self.source(adapter='html_index', article_path_pattern=r'^/stories/')
            with patch('ai_daily_pipeline.sources.fetch_response', return_value=FetchResponse(body, None, None)):
                result = collect_source(source, on_decision=lambda *a: events.append(a))
            self.assertEqual(result.status, 'empty')
            if reason: self.assertIn(reason, [event[-1] for event in events])

    def test_collection_failure_isolated_with_timeout_health(self):
        sources = [self.source(), self.source(source_id='two')]
        with TemporaryDirectory() as directory, patch('ai_daily_pipeline.pipeline.load_sources', return_value=sources), \
             patch('ai_daily_pipeline.sources.fetch_response', side_effect=[TimeoutError('timeout'), FetchResponse('<rss/>', None, None)]):
            result = run_collection(Path(directory), dry_run=False, now=NOW)
            store = ArticleStore(Path(directory)/'data/ai_daily.sqlite3')
            try:
                health = store.source_health_rows()
                self.assertEqual(health['one']['timeout_count'], 1)
                self.assertEqual(health['two']['empty_count'], 1)
            finally: store.close()
        self.assertEqual([row['status'] for row in result.source_health], ['error', 'empty'])
        self.assertTrue(result.source_errors[0]['isolated'])
        self.assertGreaterEqual(result.source_health[0]['duration_ms'], 0)

    def test_index_and_article_use_same_source_network_policy(self):
        source = self.source(proxy_mode='direct', request_timeout_seconds=6, request_attempts=1)
        xml='<rss><channel><item><title>Quantum research</title><link>https://example.com/story</link></item></channel></rss>'
        with patch('ai_daily_pipeline.sources.fetch_response', return_value=FetchResponse(xml,None,None)) as fetch:
            item=collect_source(source).items[0]
        self.assertEqual(fetch.call_args.kwargs['proxy_mode'], 'direct')
        with patch('ai_daily_pipeline.pipeline.fetch', return_value='<html/>') as fetch:
            _candidate_to_article(item,NOW,source)
        self.assertEqual(fetch.call_args.kwargs, source.fetch_options())

    def test_baidu_real_fixture_and_discovery_cannot_enter_article_pool(self):
        source=next(s for s in load_sources(ROOT/'config/sources.json') if s.source_id=='baidu-hotlist')
        body=(ROOT/'tests/fixtures/baidu-board-2026-10-02.json').read_text(encoding='utf-8')
        items=ADAPTERS[source.adapter].parse(source,body)
        self.assertGreater(len(items),0)
        self.assertTrue(all(i.source_role=='discovery' and i.tier==4 for i in items))
        with patch('ai_daily_pipeline.pipeline.fetch') as fetch:
            self.assertIsNone(_candidate_to_article(items[0],NOW,source))
        fetch.assert_not_called()
        with self.assertRaisesRegex(ValueError,'restricted'):
            ADAPTERS[source.adapter].parse(replace(source,source_role='primary',tier=1),body)

    def test_baidu_empty_and_changed_api_schema(self):
        source=self.source(adapter='discovery_json',source_role='discovery',tier=4)
        self.assertEqual(ADAPTERS[source.adapter].parse(source,'{"success":true,"data":{"cards":[]}}'),[])
        for body in ('[]','{}','{"success":false,"data":{"cards":[]}}'):
            with self.assertRaisesRegex(ValueError,'schema unavailable'):
                ADAPTERS[source.adapter].parse(source,body)

    def test_network_config_validation_and_discovery_low_cost(self):
        sources=load_sources(ROOT/'config/sources.json')
        direct=[s for s in sources if s.proxy_mode=='direct']
        self.assertEqual(len(direct),8)
        self.assertEqual(len([s for s in sources if s.enabled]),29)
        baidu=next(s for s in direct if s.source_id=='baidu-hotlist')
        self.assertEqual((baidu.request_timeout_seconds,baidu.request_attempts),(6,1))
        with TemporaryDirectory() as directory:
            path=Path(directory)/'sources.json'
            base=json.loads((ROOT/'config/sources.json').read_text(encoding='utf-8'))[0]
            for key,value in [('proxy_mode','invalid'),('request_attempts',0),('request_attempts',True),('request_timeout_seconds',100)]:
                path.write_text(json.dumps([{**base,key:value}]),encoding='utf-8')
                with self.assertRaises(ValueError): load_sources(path)

    def test_source_health_counters_failure_streak_and_p95(self):
        with TemporaryDirectory() as directory:
            store=ArticleStore(Path(directory)/'test.sqlite3')
            try:
                for i in range(1,21): store.record_source_health('one','ok',str(i),1,duration_ms=i)
                store.record_source_health('one','empty','21',0,duration_ms=None)
                store.record_source_health('one','error','22',0,duration_ms=30,error_kind='timeout')
                store.record_source_health('one','error','23',0,duration_ms=40,error_kind='ssl')
                metrics=store.source_health_metrics('one')
                self.assertEqual((metrics['success_count'],metrics['empty_count'],metrics['timeout_count'],metrics['failure_count']),(20,1,1,1))
                self.assertEqual(metrics['consecutive_failures'],2)
                self.assertEqual(metrics['p95_duration_ms'],30)
                self.assertEqual(metrics['duration_sample_count'],22)
                self.assertEqual(metrics['last_success_at'],'20')
                store.record_source_health('one','ok','24',1,duration_ms=1)
                self.assertEqual(store.source_health_metrics('one')['consecutive_failures'],0)
            finally: store.close()

    def test_legacy_health_has_no_invented_history(self):
        with TemporaryDirectory() as directory:
            store=ArticleStore(Path(directory)/'test.sqlite3')
            try:
                store.connection.execute("INSERT INTO source_health VALUES ('old','ok','old-time','old-time',3,NULL)")
                metrics=store.source_health_rows()['old']
                self.assertEqual(metrics['sample_count'],0)
                self.assertIsNone(metrics['p95_duration_ms'])
                self.assertIsNone(metrics['average_duration_ms'])
                self.assertEqual(metrics['last_success_at'],'old-time')
            finally: store.close()


if __name__=='__main__': unittest.main()
