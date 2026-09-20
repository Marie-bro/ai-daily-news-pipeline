from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class SourceItem:
    source_id: str
    source: str
    source_type: str
    title: str
    url: str
    published_at: datetime | None
    priority: int
    region: str = "unknown"
    categories: tuple[str, ...] = ("other_tech",)
    tier: int = 3
    language: str = "en"


@dataclass(frozen=True)
class Article:
    id: str
    category: str
    title: str
    original_title: str
    source: str
    source_type: str
    published_at: str
    original_url: str
    language: str
    raw_text: str
    clean_text: str
    fingerprint: str
    created_at: str
    verification_status: str
    source_region: str = "unknown"
    source_tier: int = 3

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Enrichment:
    """A compact, source-grounded Tech Daily rendering."""

    article_id: str
    task: str
    generated_at: str
    model: str
    title_cn: str
    title_en: str
    title_original: str
    source: str
    published_at: str
    original_url: str
    category: str
    original_language: str
    what_happened: str
    what_happened_en: str
    why_it_matters: str
    why_it_matters_en: str
    importance_score: int

    def to_record(self) -> dict[str, str]:
        return asdict(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "title_cn": self.title_cn,
            "title_en": self.title_en,
            "title_original": self.title_original,
            "source": self.source,
            "published_at": self.published_at,
            "original_url": self.original_url,
            "category": self.category,
            "original_language": self.original_language,
            "what_happened": self.what_happened,
            "what_happened_en": self.what_happened_en,
            "why_it_matters": self.why_it_matters,
            "why_it_matters_en": self.why_it_matters_en,
            "importance_score": self.importance_score,
        }
