import unittest

from ai_daily_pipeline.sources import SourceDefinition, _feed_items


class SourceTests(unittest.TestCase):
    def test_rss_parsing_keeps_source_metadata(self):
        source = SourceDefinition("test", "Official", "official_blog", "rss", "https://example.com/feed", ("example.com",), 1)
        xml = """<rss><channel><item><title>New AI model</title><link>https://example.com/news/model</link><pubDate>Sun, 14 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
        items = _feed_items(source, xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_type, "official_blog")
        self.assertIsNotNone(items[0].published_at)
