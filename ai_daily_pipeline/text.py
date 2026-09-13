from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape


def clean_text(value: str, limit: int = 18_000) -> str:
    value = unescape(value).replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    # Repeated boilerplate is usually caused by sticky navigation or duplicated cards.
    words = value.split(" ")
    if len(words) > 18:
        chunks: list[str] = []
        seen: set[str] = set()
        for start in range(0, len(words), 18):
            chunk = " ".join(words[start : start + 18])
            if chunk not in seen:
                chunks.extend(words[start : start + 18])
                seen.add(chunk)
        value = " ".join(chunks)
    return value[:limit].strip()


def fingerprint(title: str, clean_body: str) -> str:
    stable = clean_text(title).lower() + "\n" + clean_text(clean_body)[:1_500].lower()
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for pattern in ("%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(value, pattern)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
