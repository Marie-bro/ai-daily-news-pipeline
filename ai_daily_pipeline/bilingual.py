from __future__ import annotations

import re


_LITERAL_TOKEN = re.compile(r"\b(?:v?\d+(?:\.\d+)+|[A-Z]{2,}\d*|\d+)\b")


def validate_literal_consistency(english: str, chinese: str, field: str) -> None:
    """Require versions, numbers, and uppercase technical abbreviations in both paired renderings."""
    required = {token for token in _LITERAL_TOKEN.findall(english)}
    missing = [token for token in required if token not in chinese]
    if missing:
        raise ValueError(f"{field} is missing literals in Chinese: {', '.join(sorted(missing))}")
