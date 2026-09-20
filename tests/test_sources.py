import unittest
from unittest.mock import patch

from ai_daily_pipeline.sources import SourceDefinition, _feed_items, collect_source_items, load_sources


class SourceTests(unittest.TestCase):
    def test_configured_sources_cover_required_regions_and_quality_metadata(self):
        from pathlib import Path
        sources = load_sources(Path(__file__).resolve().parents[1] / "config" / "sources.json")
        regions = {source.region for source in sources if source.enabled}
        self.assertTrue({"CN", "HK", "US", "EU", "JP", "KR"}.issubset(regions))
        self.assertTrue(all(source.tier in {1, 2, 3, 4} for source in sources))
        self.assertTrue(all(source.categories for source in sources))
        self.assertTrue(all(source.fetch_method == source.adapter for source in sources))

    def test_rss_parsing_keeps_source_metadata(self):
        source = SourceDefinition("test", "Official", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        xml = """<rss><channel><item><title>New AI model</title><link>https://example.com/news/model</link><pubDate>Sun, 14 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
        items = _feed_items(source, xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_type, "official_blog")
        self.assertIsNotNone(items[0].published_at)

    def test_source_configuration_rejects_github_and_openai_content_hosts(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text('[{"id":"blocked","name":"Blocked","source_type":"official","adapter":"html_index","url":"https://github.com/example","allow_hosts":["github.com"],"priority":1}]', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_sources(path)

    def test_html_index_ignores_navigation_and_keeps_card_date(self):
        source = SourceDefinition("deepseek", "DeepSeek", "official_newsroom", "html_index", "https://www.deepseek.com/en/news/", ("www.deepseek.com",), 1, r"^/en/news/[^/]+/?$")
        html = '''<a href="https://chat.deepseek.com/">DeepSeek Chat AI</a>
        <a href="/en/news/deepseek-v4-1-flash/">News September 10, 2026 Introducing DeepSeek-V4.1-Flash</a>'''
        with patch("ai_daily_pipeline.sources.fetch", return_value=html):
            items = collect_source_items(source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://www.deepseek.com/en/news/deepseek-v4-1-flash/")
        self.assertEqual(items[0].published_at.date().isoformat(), "2026-09-10")
