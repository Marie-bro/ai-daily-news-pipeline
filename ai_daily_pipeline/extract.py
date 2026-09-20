from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .text import clean_text, parse_datetime

DROP_TAGS = {"script", "style", "svg", "nav", "footer", "header", "aside", "form", "noscript"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SELECTOR = re.compile(r"^(?P<tag>[a-z][a-z0-9-]*)?(?:(?P<kind>[.#])(?P<value>[a-zA-Z_][\w-]*))?$", re.I)


@dataclass(frozen=True)
class CleaningRules:
    include: str | None = None
    exclude: tuple[str, ...] = ()


def validate_selector(selector: str) -> None:
    if not isinstance(selector, str) or not selector or not SELECTOR.fullmatch(selector):
        raise ValueError(f"Unsupported cleaning selector: {selector!r}; use tag, .class, #id, tag.class or tag#id")


def _matches(selector: str, tag: str, attrs: dict[str, str | None]) -> bool:
    match = SELECTOR.fullmatch(selector)
    assert match is not None
    if match.group("tag") and match.group("tag").lower() != tag:
        return False
    kind, value = match.group("kind"), match.group("value")
    if kind == "#":
        return attrs.get("id") == value
    if kind == ".":
        return value in (attrs.get("class") or "").split()
    return True


class _ArticleParser(HTMLParser):
    def __init__(self, rules: CleaningRules) -> None:
        super().__init__()
        self.rules = rules
        self.stack: list[tuple[str, bool, bool]] = []
        self.include_found = False
        self.text: list[str] = []
        self.title: list[str] = []
        self.in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        parent_included = self.stack[-1][1] if self.stack else False
        parent_excluded = self.stack[-1][2] if self.stack else False
        included = parent_included or bool(self.rules.include and _matches(self.rules.include, tag, attrs_map))
        excluded = parent_excluded or tag in DROP_TAGS or any(_matches(rule, tag, attrs_map) for rule in self.rules.exclude)
        self.include_found |= included
        if tag not in VOID_TAGS:
            self.stack.append((tag, included, excluded))
        if tag == "h1" and not excluded:
            self.in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data: str) -> None:
        included = self.stack[-1][1] if self.stack else False
        excluded = self.stack[-1][2] if self.stack else False
        if not excluded and (self.rules.include is None or included):
            self.text.append(data)
        if self.in_h1 and not excluded:
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


def extract_article(html: str, fallback_title: str, rules: CleaningRules | None = None) -> ExtractedArticle:
    rules = rules or CleaningRules()
    parser = _ArticleParser(rules)
    parser.feed(html)
    if rules.include and not parser.include_found:
        raise ValueError(f"Cleaning include selector did not match: {rules.include}")
    raw = clean_text(" ".join(parser.text), limit=60_000)
    body = clean_text(raw)
    title = clean_text(_meta(html, "og:title") or " ".join(parser.title) or fallback_title, limit=500)
    date_value = _meta(html, "article:published_time") or _meta(html, "date")
    if not date_value:
        match = re.search(r'<time[^>]+datetime=["\']([^"\']+)', html, re.I)
        date_value = match.group(1) if match else None
    return ExtractedArticle(title=title, raw_text=raw, clean_text=body, published_at=parse_datetime(date_value))
