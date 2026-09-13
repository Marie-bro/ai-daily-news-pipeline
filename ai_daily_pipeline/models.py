from __future__ import annotations

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
