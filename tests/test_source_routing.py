"""Offline coverage for source-level network routing and health records."""
from __future__ import annotations

import json
import socket
import sqlite3
import ssl
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from ai_daily_pipeline.pipeline import run_collection
from ai_daily_pipeline.sources import FetchResponse, SourceDefinition, fetch_response, load_sources
from ai_daily_pipeline.store import ArticleStore


ROOT = Path(__file__).resolve().parents[1]


class Response:
    status = 200
    headers = Message()

    def __init__(self, body: bytes = b"<rss/>"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return "https://example.com/feed"

    def read(self, limit):
        return self.body[:limit]


class RoutingTests(unittest.TestCase):
    def source(self, **changes):
        base = SourceDefinition("one", "Official", "official_blog", "rss",
                                "https://example.com/feed", ("example.com",), 1)
        return replace(base, **changes)

    def test_direct_never_reads_system_proxy(self):
        opener = Mock()
        opener.open.return_value = Response()
        with patch("ai_daily_pipeline.sources.urllib.request.getproxies") as proxies, \
             patch("ai_daily_pipeline.sources.urllib.request.build_opener", return_value=opener) as build, \
             patch("ai_daily_pipeline.sources.urllib.request.urlopen") as system:
            result = fetch_response(self.source().url, proxy_mode="direct", source_id="one")
        proxies.assert_not_called()
        system.assert_not_called()
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(result.request_history[0]["source_id"], "one")
        self.assertFalse(result.request_history[0]["proxy_configured"])

    def test_system_uses_urllib_automatic_proxy(self):
        with patch("ai_daily_pipeline.sources.urllib.request.getproxies", return_value={"https": "http://proxy.test:7890"}) as proxies, \
             patch("ai_daily_pipeline.sources.urllib.request.urlopen", return_value=Response()) as system:
            result = fetch_response(self.source().url)
        proxies.assert_called_once()
        system.assert_called_once()
        self.assertEqual(result.request_history[0]["network_route"], "system")
        self.assertTrue(result.request_history[0]["proxy_configured"])

    def test_tls_failure_has_distinct_class(self):
        opener = Mock()
        opener.open.side_effect = URLError(ssl.SSLEOFError("EOF"))
        with patch("ai_daily_pipeline.sources.urllib.request.build_opener", return_value=opener), \
             patch("ai_daily_pipeline.sources.time.sleep"), self.assertRaises(URLError) as caught:
            fetch_response(self.source().url, proxy_mode="direct")
        last = caught.exception.source_request_history[-1]
        self.assertEqual(last["failure_class"], "tls_failure")
        self.assertEqual(last["tls_result"], "failed")
        self.assertEqual(last["retry_count"], 1)

    def test_dns_failure_has_distinct_class(self):
        opener = Mock()
        opener.open.side_effect = URLError(socket.gaierror(-2, "name lookup failed"))
        with patch("ai_daily_pipeline.sources.urllib.request.build_opener", return_value=opener), \
             patch("ai_daily_pipeline.sources.time.sleep"), self.assertRaises(URLError) as caught:
            fetch_response(self.source().url, proxy_mode="direct")
        last = caught.exception.source_request_history[-1]
        self.assertEqual(last["failure_class"], "dns_failure")
        self.assertEqual(last["connect_result"], "not_attempted")

    def test_http_500_is_not_tls(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(self.source().url, 500, "server error", Message(), None)
        with patch("ai_daily_pipeline.sources.urllib.request.build_opener", return_value=opener), \
             patch("ai_daily_pipeline.sources.time.sleep"), self.assertRaises(HTTPError) as caught:
            fetch_response(self.source().url, proxy_mode="direct")
        last = caught.exception.source_request_history[-1]
        self.assertEqual((last["http_status"], last["failure_class"]), (500, "http_error"))

    def test_parse_failure_distinct_from_successful_http(self):
        with TemporaryDirectory() as directory, patch("ai_daily_pipeline.pipeline.load_sources", return_value=[self.source()]), \
             patch("ai_daily_pipeline.sources.fetch_response", return_value=FetchResponse("<rss", None, None,
                 request_history=({"http_status": 200, "failure_class": None},))):
            result = run_collection(Path(directory), dry_run=False, now=datetime(2026, 10, 12, tzinfo=UTC))
        self.assertEqual(result.source_errors[0]["failure_class"], "parse_failure")
        self.assertEqual(result.source_errors[0]["http_status"], 200)

    def test_health_persists_route_and_failure_class(self):
        with TemporaryDirectory() as directory:
            store = ArticleStore(Path(directory) / "test.sqlite3")
            try:
                store.record_source_health("one", "error", "now", 0, duration_ms=10,
                                           error_kind="ssl", network_route="direct",
                                           failure_class="tls_failure")
                row = store.source_health_rows()["one"]
                self.assertEqual((row["last_network_route"], row["last_failure_class"]),
                                 ("direct", "tls_failure"))
                store.record_source_health("one", "ok", "later", 1, network_route="system")
                self.assertEqual(store.source_health_rows()["one"]["last_failure_class"], None)
            finally:
                store.close()

    def test_legacy_health_events_migrate_without_rewriting_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE source_health_events (id INTEGER PRIMARY KEY, source_id TEXT, status TEXT, checked_at TEXT, duration_ms REAL, error_kind TEXT)")
            connection.execute("INSERT INTO source_health_events VALUES (1,'one','error','before',5,'timeout')")
            connection.commit()
            connection.close()
            store = ArticleStore(path)
            try:
                row = store.source_health_metrics("one")
                self.assertEqual(row["sample_count"], 1)
                self.assertIsNone(row["last_network_route"])
            finally:
                store.close()

    def test_unconfigured_sources_keep_system_default_and_legacy_alias(self):
        sources = load_sources(ROOT / "config" / "sources.json")
        self.assertEqual(len(sources), 29)
        self.assertEqual(sum(source.network_route == "system" for source in sources), 21)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            source = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))[0]
            source["proxy_mode"] = "direct"
            path.write_text(json.dumps([source]), encoding="utf-8")
            self.assertEqual(load_sources(path)[0].network_route, "direct")
            source["network_route"] = "system"
            path.write_text(json.dumps([source]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicts"):
                load_sources(path)

    def test_explicit_proxy_is_available_without_logging_credentials(self):
        opener = Mock()
        opener.open.return_value = Response()
        with patch.dict("os.environ", {"SOURCE_EXPLICIT_PROXY_URL": "http://user:secret@proxy.test:7890"}), \
             patch("ai_daily_pipeline.sources.urllib.request.build_opener", return_value=opener) as build:
            result = fetch_response(self.source().url, proxy_mode="explicit")
        self.assertEqual(build.call_args.args[0].proxies["https"], "http://user:secret@proxy.test:7890")
        self.assertNotIn("secret", json.dumps(result.request_history))
        with patch.dict("os.environ", {"SOURCE_EXPLICIT_PROXY_URL": ""}), self.assertRaisesRegex(ValueError, "not configured"):
            fetch_response(self.source().url, proxy_mode="explicit")

    def test_eight_reviewed_sources_use_explicit_direct_route(self):
        raw = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
        expected = {"cas-research", "miit-policy", "moe-updates", "national-statistics",
                    "most-updates", "sciencenet-news", "jiemian-industry", "baidu-hotlist"}
        configured = {item["id"] for item in raw if item.get("network_route") == "direct"}
        self.assertEqual(configured, expected)
        self.assertTrue(all("proxy_mode" not in item for item in raw if item["id"] in expected))


if __name__ == "__main__":
    unittest.main()
