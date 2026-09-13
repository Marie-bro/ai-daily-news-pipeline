from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .text import clean_text, parse_datetime

DROP_TAGS = {"script", "style", "svg", "nav", "footer", "header", "aside", "form", "noscript"}


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.drop_depth = 0
        self.text: list[str] = []
        self.title: list[str] = []
        self.in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_TAGS:
            self.drop_depth += 1
        if tag == "h1" and not self.drop_depth:
            self.in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_TAGS and self.drop_depth:
            self.drop_depth -= 1
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.text.append(data)
            if self.in_h1:
                self.title.append(data)


@dataclass(frozen=True)
class ExtractedArticle:
    title: str
    raw_text: str
    clean_text: str
    published_at: object | None


def _meta(html: str, key: str) -> str | None:
    pattern = rf'<meta[^>]+(?:property|name)=["\'](?:{re.escape(key)})["\'][^>]+content=["\']([^"\']+)'
    match = re.search(pattern, html, re.I)
    if not match:
        pattern = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:{re.escape(key)})["\']'
        match = re.search(pattern, html, re.I)
    return match.group(1).strip() if match else None


def extract_article(html: str, fallback_title: str) -> ExtractedArticle:
    parser = _ArticleParser()
    parser.feed(html)
    raw = clean_text(" ".join(parser.text), limit=60_000)
    body = clean_text(raw)
    title = clean_text(_meta(html, "og:title") or " ".join(parser.title) or fallback_title, limit=500)
    date_value = _meta(html, "article:published_time") or _meta(html, "date")
    if not date_value:
        match = re.search(r'<time[^>]+datetime=["\']([^"\']+)', html, re.I)
        date_value = match.group(1) if match else None
    return ExtractedArticle(title=title, raw_text=raw, clean_text=body, published_at=parse_datetime(date_value))
