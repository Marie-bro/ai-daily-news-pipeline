from datetime import UTC, datetime
import unittest

from ai_daily_pipeline.models import Article
from ai_daily_pipeline.pipeline import _collapse_release_bursts
from ai_daily_pipeline.text import clean_text, fingerprint, parse_datetime


class TextTests(unittest.TestCase):
    def test_clean_text_collapses_whitespace_and_caps_length(self):
        self.assertEqual(clean_text(" one\n\n two\u00a0three "), "one two three")
        self.assertEqual(len(clean_text("x" * 100, limit=12)), 12)

    def test_fingerprint_is_stable_for_whitespace_variants(self):
        self.assertEqual(fingerprint("A title", "one two"), fingerprint(" A   title ", "one  two"))

    def test_parse_datetime_returns_utc(self):
        self.assertEqual(parse_datetime("Sun, 14 Sep 2026 10:00:00 +0800"), datetime(2026, 9, 14, 2, tzinfo=UTC))

    def test_release_burst_keeps_only_newest_same_day_series(self):
        base = dict(category="ai", title="Release v1.2.3", original_title="Release", source="Official SDK", source_type="official_changelog", language="en", raw_text="raw" * 100, clean_text="clean" * 100, fingerprint="a", created_at="2026-09-14T00:00:00+00:00", verification_status="source_verified")
        older = Article(id="a", published_at="2026-09-14T01:00:00+00:00", original_url="https://example.com/1", **base)
        newer = Article(id="b", title="Release v1.2.4", published_at="2026-09-14T02:00:00+00:00", original_url="https://example.com/2", fingerprint="b", **{key: value for key, value in base.items() if key not in {"title", "fingerprint"}})
        self.assertEqual(_collapse_release_bursts([older, newer]), [newer])
