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

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Enrichment:
    """A source-grounded bilingual rendering produced for one stored article."""

    article_id: str
    task: str
    generated_at: str
    model: str
    title_cn: str
    title_original: str
    source: str
    published_at: str
    original_url: str
    key_points_original: tuple[str, ...]
    translation: tuple[str, ...]
    summary_cn: str
    summary_en: str
    relevance: str
    useful_expressions: tuple[str, ...]

    def to_record(self) -> dict[str, str]:
        data = asdict(self)
        for field in ("key_points_original", "translation", "useful_expressions"):
            data[field] = json.dumps(data[field], ensure_ascii=False)
        return data

    def to_dict(self) -> dict[str, object]:
        return {
            "title_cn": self.title_cn,
            "title_original": self.title_original,
            "source": self.source,
            "published_at": self.published_at,
            "original_url": self.original_url,
            "key_points_original": list(self.key_points_original),
            "translation": list(self.translation),
            "summary_cn": self.summary_cn,
            "summary_en": self.summary_en,
            "relevance": self.relevance,
            "useful_expressions": list(self.useful_expressions),
        }
