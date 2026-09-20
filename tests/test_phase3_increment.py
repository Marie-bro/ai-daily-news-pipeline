from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
import urllib.error
from unittest.mock import patch

from ai_daily_pipeline.extract import CleaningRules, extract_article
from ai_daily_pipeline.pipeline import check_sources
from ai_daily_pipeline.sources import FetchResponse, SourceDefinition, collect_source, fetch_response, load_sources
from ai_daily_pipeline.store import ArticleStore


class Phase3IncrementTests(unittest.TestCase):
    def test_config_rejects_bad_fields_and_supports_disabled_source(self):
        base = {"id": "one", "name": "Official", "source_type": "official_blog", "adapter": "rss",
                "url": "https://example.com/feed", "allow_hosts": ["example.com"], "priority": 1,
                "enabled": False, "region": "US", "category": ["software"], "tier": 1, "language": "en",
                "fetch_method": "rss", "health_status": "active", "cleaning": {"include": "article.story", "exclude": [".promo"]}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(json.dumps([base]), encoding="utf-8")
            source = load_sources(path)[0]
            self.assertFalse(source.enabled)
            self.assertEqual(source.cleaning.include, "article.story")
            for altered in ({**base, "enabled": "false"}, {**base, "adapter": "unknown"},
                            {**base, "enabeld": False},
                            {**base, "cleaning": {"include": "article .story"}}):
                path.write_text(json.dumps([altered]), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_sources(path)
            path.write_text(json.dumps([base, base]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_sources(path)

    def test_conditional_request_reuses_cached_body_on_304(self):
        cached = {"body": "<rss/>", "etag": '"v1"', "last_modified": "Mon, 14 Sep 2026 00:00:00 GMT"}
        def not_modified(request, timeout):
            self.assertEqual(request.get_header("If-none-match"), '"v1"')
            self.assertEqual(request.get_header("If-modified-since"), cached["last_modified"])
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", Message(), None)
        with patch("ai_daily_pipeline.sources.urllib.request.urlopen", side_effect=not_modified):
            response = fetch_response("https://example.com/feed", cached=cached)
        self.assertTrue(response.not_modified)
        self.assertEqual(response.body, "<rss/>")

    def test_source_fetch_rejects_redirect_outside_allowed_host(self):
        class RedirectedResponse:
            status = 200
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def geturl(self): return "https://other.example/story"
            def read(self, *_): return b"<rss/>"
        with patch("ai_daily_pipeline.sources.urllib.request.urlopen", return_value=RedirectedResponse()):
            with self.assertRaisesRegex(ValueError, "redirected outside"):
                fetch_response("https://example.com/feed", allow_hosts=("example.com",))

    def test_source_adapter_cache_survives_runs_and_disabled_source_is_not_fetched(self):
        source = SourceDefinition("official", "Official", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        xml = "<rss><channel><item><title>New AI model</title><link>https://example.com/story</link></item></channel></rss>"
        with TemporaryDirectory() as directory:
            store = ArticleStore(Path(directory) / "articles.sqlite3")
            try:
                with patch("ai_daily_pipeline.sources.fetch_response", return_value=FetchResponse(xml, '"v1"', None)):
                    first = collect_source(source, store)
                self.assertEqual(len(first.items), 1)
                cached = store.get_source_cache("official", source.url)
                self.assertEqual(cached["etag"], '"v1"')
                with patch("ai_daily_pipeline.sources.fetch_response", return_value=FetchResponse(xml, '"v1"', None, True)) as fetch:
                    second = collect_source(source, store)
                self.assertEqual(fetch.call_args.kwargs["cached"], cached)
                self.assertTrue(second.not_modified)
                self.assertEqual(len(second.items), 1)
                with patch("ai_daily_pipeline.sources.fetch_response") as fetch:
                    disabled = collect_source(SourceDefinition("off", "Off", "official_blog", "rss", source.url, ("example.com",), 1, enabled=False), store)
                fetch.assert_not_called()
                self.assertEqual(disabled.status, "disabled")
            finally:
                store.close()

    def test_site_cleaning_rule_removes_noise_and_fails_when_selector_drifts(self):
        html = '<h1>New AI model</h1><main><article class="story"><p>' + ('AI research advances. ' * 12) + '</p><div class="promo">ADVERTISEMENT</div></article></main>'
        rules = CleaningRules("article.story", (".promo",))
        extracted = extract_article(html, "Fallback", rules)
        self.assertIn("AI research advances", extracted.clean_text)
        self.assertNotIn("ADVERTISEMENT", extracted.clean_text)
        with self.assertRaisesRegex(ValueError, "did not match"):
            extract_article(html, "Fallback", CleaningRules("#missing"))

    def test_health_check_reports_each_source_independently(self):
        sources = [SourceDefinition("ok", "OK", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1),
                   SourceDefinition("off", "Off", "official_blog", "rss", "https://example.com/off", ("example.com",), 2, enabled=False),
                   SourceDefinition("bad", "Bad", "official_blog", "rss", "https://example.com/bad", ("example.com",), 3)]
        with patch("ai_daily_pipeline.pipeline.load_sources", return_value=sources), patch("ai_daily_pipeline.sources.fetch_response", side_effect=[FetchResponse("<rss/>", None, None), ValueError("unavailable")]):
            results = check_sources(Path("."))
        self.assertEqual([result["status"] for result in results], ["empty", "disabled", "error"])
