from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from .extract import CleaningRules, validate_selector
from .models import SourceItem
from .text import parse_datetime

USER_AGENT = "AI-Daily-Collector/0.1 (+personal research; contact: local operator)"
TECH_TERMS = re.compile(r"\b(ai|deepseek|anthropic|claude|gemini|agent|mcp|model|llm|api|machine learning|generative|chip|semiconductor|software|robot|robotics|vehicle|ev|mobility|space|satellite|science|research|internet|cloud|security|processor|device|smartphone|quantum)\b", re.I)
AI_TERMS = TECH_TERMS  # Backward-compatible import name.
BLOCKED_CONTENT_HOSTS = ("github.com", "openai.com")
BLOCKED_CONTENT_TERMS = re.compile(r"\b(openai|chatgpt|codex|github)\b", re.I)
TECH_CATEGORIES = {"ai", "chips", "consumer_tech", "software", "robotics", "mobility", "space", "science", "internet", "other_tech"}


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    name: str
    source_type: str
    adapter: str
    url: str
    allow_hosts: tuple[str, ...]
    priority: int
    article_path_pattern: str | None = None
    enabled: bool = True
    cleaning: CleaningRules = CleaningRules()
    conditional_requests: bool = True
    region: str = "unknown"
    categories: tuple[str, ...] = ("other_tech",)
    tier: int = 3
    language: str = "en"
    fetch_method: str = "html_index"
    health_status: str = "active"


@dataclass(frozen=True)
class SourceCollection:
    items: tuple[SourceItem, ...]
    status: str
    not_modified: bool = False
    used_conditional_request: bool = False


class SourceCache(Protocol):
    def get_source_cache(self, source_id: str, url: str) -> dict[str, str] | None: ...
    def save_source_cache(self, source_id: str, url: str, body: str, etag: str | None, last_modified: str | None) -> None: ...


def _required_string(item: dict, key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sources[{index}].{key} must be a non-empty string")
    return value.strip()


def load_sources(path: Path) -> list[SourceDefinition]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("sources.json must contain an array")
    sources: list[SourceDefinition] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"sources[{index}] must be an object")
        unknown = set(item) - {"id", "name", "source_type", "adapter", "url", "allow_hosts", "priority",
                               "article_path_pattern", "enabled", "cleaning", "conditional_requests", "region",
                               "category", "tier", "language", "fetch_method", "health_status"}
        if unknown:
            raise ValueError(f"sources[{index}] has unknown fields: {', '.join(sorted(unknown))}")
        source_id = _required_string(item, "id", index)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source_id) or source_id in seen_ids:
            raise ValueError(f"sources[{index}].id must be unique and use lowercase letters, numbers, _ or -")
        seen_ids.add(source_id)
        adapter = _required_string(item, "adapter", index)
        if adapter not in ADAPTERS:
            raise ValueError(f"sources[{index}].adapter is unsupported: {adapter}")
        url = _required_string(item, "url", index)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"sources[{index}].url must be a public HTTPS URL")
        hosts = item.get("allow_hosts")
        if not isinstance(hosts, list) or not hosts or any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in hosts):
            raise ValueError(f"sources[{index}].allow_hosts must be a non-empty host list")
        if not any(parsed.hostname == host.lower() or parsed.hostname.endswith("." + host.lower()) for host in hosts):
            raise ValueError(f"sources[{index}].url host must be in allow_hosts")
        if _blocked_host(parsed.hostname) or any(_blocked_host(host) for host in hosts):
            raise ValueError(f"Blocked content host in source configuration: {source_id}")
        priority = item.get("priority")
        if type(priority) is not int or not 1 <= priority <= 100:
            raise ValueError(f"sources[{index}].priority must be an integer from 1 to 100")
        region = _required_string(item, "region", index)
        language = _required_string(item, "language", index)
        source_type = _required_string(item, "source_type", index)
        fetch_method = _required_string(item, "fetch_method", index)
        health_status = _required_string(item, "health_status", index)
        if fetch_method != adapter:
            raise ValueError(f"sources[{index}].fetch_method must match adapter")
        if health_status not in {"active", "degraded", "paused"}:
            raise ValueError(f"sources[{index}].health_status is invalid")
        tier = item.get("tier")
        if type(tier) is not int or tier not in {1, 2, 3, 4}:
            raise ValueError(f"sources[{index}].tier must be 1, 2, 3 or 4")
        categories = item.get("category")
        if not isinstance(categories, list) or not categories or any(not isinstance(value, str) or not value for value in categories):
            raise ValueError(f"sources[{index}].category must be a non-empty string array")
        if any(value not in TECH_CATEGORIES for value in categories):
            raise ValueError(f"sources[{index}].category contains an unsupported technology category")
        for key in ("enabled", "conditional_requests"):
            if key in item and type(item[key]) is not bool:
                raise ValueError(f"sources[{index}].{key} must be a boolean")
        path_pattern = item.get("article_path_pattern")
        if path_pattern is not None:
            if not isinstance(path_pattern, str):
                raise ValueError(f"sources[{index}].article_path_pattern must be a string")
            try:
                re.compile(path_pattern)
            except re.error as exc:
                raise ValueError(f"sources[{index}].article_path_pattern is invalid: {exc}") from exc
        cleaning_data = item.get("cleaning", {})
        if not isinstance(cleaning_data, dict) or set(cleaning_data) - {"include", "exclude"}:
            raise ValueError(f"sources[{index}].cleaning supports only include and exclude")
        include = cleaning_data.get("include")
        exclude = cleaning_data.get("exclude", [])
        if include is not None:
            validate_selector(include)
        if not isinstance(exclude, list):
            raise ValueError(f"sources[{index}].cleaning.exclude must be an array")
        for selector in exclude:
            validate_selector(selector)
        sources.append(SourceDefinition(
            source_id=source_id, name=_required_string(item, "name", index),
            source_type=source_type, adapter=adapter, url=url,
            allow_hosts=tuple(host.lower() for host in hosts), priority=priority,
            article_path_pattern=path_pattern, enabled=item.get("enabled", True),
            cleaning=CleaningRules(include, tuple(exclude)),
            conditional_requests=item.get("conditional_requests", True),
            region=region, categories=tuple(categories), tier=tier, language=language,
            fetch_method=fetch_method, health_status=health_status,
        ))
    return sources


def fetch(url: str, timeout: int = 12, allow_hosts: tuple[str, ...] | None = None) -> str:
    return fetch_response(url, timeout=timeout, allow_hosts=allow_hosts).body


@dataclass(frozen=True)
class FetchResponse:
    body: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


def fetch_response(url: str, timeout: int = 12, cached: dict[str, str] | None = None,
                   allow_hosts: tuple[str, ...] | None = None) -> FetchResponse:
    if not _safe_fetch_url(url, allow_hosts):
        raise ValueError(f"Fetch URL is not an allowed HTTPS source: {url}")
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/xml;q=0.9,*/*;q=0.2"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 304 or cached is None:
            raise
        return FetchResponse(cached["body"], cached.get("etag"), cached.get("last_modified"), True)
    with response:
        final_url = response.geturl()
        if not _safe_fetch_url(final_url, allow_hosts):
            raise ValueError(f"Fetch redirected outside allowed HTTPS hosts: {final_url}")
        if getattr(response, "status", 200) == 304:
            if cached is None:
                raise ValueError("304 response without cached content")
            return FetchResponse(cached["body"], cached.get("etag"), cached.get("last_modified"), True)
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Source response exceeds 2 MB")
        charset = response.headers.get_content_charset() or "utf-8"
        return FetchResponse(raw.decode(charset, errors="replace"), response.headers.get("ETag"), response.headers.get("Last-Modified"))


def _blocked_host(hostname: str) -> bool:
    hostname = hostname.lower().strip(".")
    return any(hostname == host or hostname.endswith("." + host) for host in BLOCKED_CONTENT_HOSTS)


def _safe_fetch_url(url: str, allow_hosts: tuple[str, ...] | None) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and bool(hostname) and not parsed.username and not parsed.password
            and not _blocked_host(hostname)
            and (allow_hosts is None or any(hostname == host or hostname.endswith("." + host) for host in allow_hosts)))


def _allowed(url: str, source: SourceDefinition) -> bool:
    return _safe_fetch_url(url, source.allow_hosts)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_url: str | None = None
        self.current_text: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.current_url = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_url:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_url:
            self.anchors.append((self.current_url, " ".join(self.current_text)))
            self.current_url, self.current_text = None, []


def _feed_items(source: SourceDefinition, xml_text: str) -> list[SourceItem]:
    root = ET.fromstring(xml_text)
    items: list[SourceItem] = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        published = parse_datetime(node.findtext("pubDate") or node.findtext("published"))
        if title and url and _allowed(url, source):
            items.append(SourceItem(source.source_id, source.name, source.source_type, title, url, published, source.priority,
                                    source.region, source.categories, source.tier, source.language))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for node in root.findall(".//atom:entry", ns):
        title = (node.findtext("atom:title", namespaces=ns) or "").strip()
        link = next((entry.attrib.get("href", "") for entry in node.findall("atom:link", ns) if entry.attrib.get("rel", "alternate") == "alternate"), "")
        published = parse_datetime(node.findtext("atom:published", namespaces=ns) or node.findtext("atom:updated", namespaces=ns))
        if title and link and _allowed(link, source):
            items.append(SourceItem(source.source_id, source.name, source.source_type, title, link, published, source.priority,
                                    source.region, source.categories, source.tier, source.language))
    # Bound oversized feeds before downstream scheduling; feeds are newest-first.
    return items[:100]


class SourceAdapter(Protocol):
    def parse(self, source: SourceDefinition, body: str) -> list[SourceItem]: ...


class RSSAdapter:
    def parse(self, source: SourceDefinition, body: str) -> list[SourceItem]:
        return _feed_items(source, body)


class AtomAdapter:
    def parse(self, source: SourceDefinition, body: str) -> list[SourceItem]:
        return _feed_items(source, body)


class HTMLIndexAdapter:
    def parse(self, source: SourceDefinition, body: str) -> list[SourceItem]:
        parser = _AnchorParser()
        parser.feed(body)
        deduped: dict[str, SourceItem] = {}
        for href, title in parser.anchors:
            url = urljoin(source.url, href)
            title = " ".join(title.split())
            if not title or len(title) < 12 or BLOCKED_CONTENT_TERMS.search(title) or not AI_TERMS.search(title) or not _allowed(url, source):
                continue
            if source.article_path_pattern and not re.search(source.article_path_pattern, urlparse(url).path):
                continue
            if url.rstrip("/") == source.url.rstrip("/"):
                continue
            date_match = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", title, re.I)
            published = parse_datetime(date_match.group(0)) if date_match else None
            deduped.setdefault(url, SourceItem(source.source_id, source.name, source.source_type, title, url, published, source.priority,
                                               source.region, source.categories, source.tier, source.language))
        return list(deduped.values())


ADAPTERS: dict[str, SourceAdapter] = {"rss": RSSAdapter(), "atom": AtomAdapter(), "html_index": HTMLIndexAdapter()}


def collect_source_items(source: SourceDefinition) -> list[SourceItem]:
    """Collect without changing persistent cache; useful for checks and one-off callers."""
    if not source.enabled:
        return []
    return ADAPTERS[source.adapter].parse(source, fetch(source.url, allow_hosts=source.allow_hosts))


def collect_source(source: SourceDefinition, cache: SourceCache | None = None) -> SourceCollection:
    if not source.enabled:
        return SourceCollection((), "disabled")
    cached = cache.get_source_cache(source.source_id, source.url) if cache and source.conditional_requests else None
    response = fetch_response(source.url, cached=cached, allow_hosts=source.allow_hosts)
    items = tuple(ADAPTERS[source.adapter].parse(source, response.body))
    if cache and not response.not_modified:
        cache.save_source_cache(source.source_id, source.url, response.body, response.etag, response.last_modified)
    return SourceCollection(items, "ok" if items else "empty", response.not_modified,
                            bool(cached and (cached.get("etag") or cached.get("last_modified"))))
