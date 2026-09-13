from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .models import SourceItem
from .text import parse_datetime

USER_AGENT = "AI-Daily-Collector/0.1 (+personal research; contact: local operator)"
AI_TERMS = re.compile(r"\b(ai|openai|chatgpt|codex|deepseek|anthropic|claude|gemini|agent|mcp|model|llm|api|machine learning|generative)\b", re.I)


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    name: str
    source_type: str
    adapter: str
    url: str
    allow_hosts: tuple[str, ...]
    priority: int


def load_sources(path: Path) -> list[SourceDefinition]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        SourceDefinition(
            source_id=item["id"], name=item["name"], source_type=item["source_type"],
            adapter=item["adapter"], url=item["url"],
            allow_hosts=tuple(item["allow_hosts"]), priority=int(item["priority"]),
        )
        for item in data
    ]


def fetch(url: str, timeout: int = 12) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/xml;q=0.9,*/*;q=0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(2_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _allowed(url: str, source: SourceDefinition) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == host or hostname.endswith("." + host) for host in source.allow_hosts)


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
            items.append(SourceItem(source.source_id, source.name, source.source_type, title, url, published, source.priority))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for node in root.findall(".//atom:entry", ns):
        title = (node.findtext("atom:title", namespaces=ns) or "").strip()
        link = next((entry.attrib.get("href", "") for entry in node.findall("atom:link", ns) if entry.attrib.get("rel", "alternate") == "alternate"), "")
        published = parse_datetime(node.findtext("atom:published", namespaces=ns) or node.findtext("atom:updated", namespaces=ns))
        if title and link and _allowed(link, source):
            items.append(SourceItem(source.source_id, source.name, source.source_type, title, link, published, source.priority))
    return items


def collect_source_items(source: SourceDefinition) -> list[SourceItem]:
    body = fetch(source.url)
    if source.adapter in {"rss", "atom"}:
        return _feed_items(source, body)
    if source.adapter != "html_index":
        raise ValueError(f"Unsupported source adapter: {source.adapter}")
    parser = _AnchorParser()
    parser.feed(body)
    deduped: dict[str, SourceItem] = {}
    for href, title in parser.anchors:
        url = urljoin(source.url, href)
        title = " ".join(title.split())
        if not title or len(title) < 12 or not AI_TERMS.search(title) or not _allowed(url, source):
            continue
        if url.rstrip("/") == source.url.rstrip("/"):
            continue
        deduped.setdefault(url, SourceItem(source.source_id, source.name, source.source_type, title, url, None, source.priority))
    return list(deduped.values())
