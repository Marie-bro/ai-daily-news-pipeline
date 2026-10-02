"""Small, credential-safe failure details for the existing delivery run log."""
from __future__ import annotations

import json
import re
import sqlite3
import ssl
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError


STAGES = {"collection", "normalization", "enrichment", "validation", "persistence"}


def mark_failure(exc: Exception, stage: str, **context: object) -> Exception:
    if stage not in STAGES:
        raise ValueError("invalid diagnostic stage")
    setattr(exc, "radar_failure_stage", stage)
    setattr(exc, "radar_failure_context", context)
    return exc


def failure_stage(exc: Exception, default: str) -> str:
    marked = getattr(exc, "radar_failure_stage", None)
    if marked in STAGES:
        return marked
    if isinstance(exc, sqlite3.Error):
        return "persistence"
    if isinstance(exc, OSError) and not isinstance(exc, (HTTPError, URLError)):
        return "persistence" if default == "enrichment" else default
    return default


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, TypeError):
        if "dry_run" in str(exc) and "run_collection" in str(exc):
            return "run_collection is missing the required dry_run argument"
        return "invalid function arguments"
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, (TimeoutError,)):
        return "network timeout"
    if isinstance(exc, ssl.SSLError):
        return "SSL failure"
    if isinstance(exc, ET.ParseError):
        return f"XML parser error at {exc.position}"
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    if isinstance(exc, ValueError):
        message = str(exc)
        if message == "Source response exceeds 2 MB":
            return "source response exceeds 2 MB size limit"
        if message.startswith("Fetch redirected outside allowed HTTPS hosts:"):
            return "redirected outside allowed HTTPS hosts"
        return "invalid source or response data"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError,)):
            return "network timeout"
        if isinstance(reason, ssl.SSLError) or "SSL" in str(type(reason)):
            return "SSL failure"
        return "network request failed"
    if isinstance(exc, sqlite3.Error):
        return f"SQLite {getattr(exc, 'sqlite_errorname', type(exc).__name__)}"
    if isinstance(exc, OSError):
        return f"OS error {exc.errno}" if exc.errno is not None else "OS error"
    message = str(exc)
    if type(exc).__name__ in {"EnrichmentError", "DeepSeekError", "BilingualValidationError"}:
        message = re.sub(r"https?://[^\s)]+", "[URL]", message)
        message = re.sub(r"(?i)(bearer|api[_ -]?key|secret|access[_ -]?token|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", message)
        return message[:300]
    return "details withheld; exception type and stack location recorded"


def error_kind(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return "http"
    if type(exc).__name__ == "DeepSeekError" and re.fullmatch(r"DeepSeek HTTP \d{3}", str(exc)):
        return "http"
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "ssl"
    if isinstance(exc, URLError):
        if isinstance(exc.reason, TimeoutError):
            return "timeout"
        if isinstance(exc.reason, ssl.SSLError) or "SSL" in str(type(exc.reason)):
            return "ssl"
        return "network"
    if isinstance(exc, (ET.ParseError, json.JSONDecodeError)):
        return "parser"
    if isinstance(exc, ValueError) and str(exc) == "Source response exceeds 2 MB":
        return "size_limit"
    if isinstance(exc, ValueError) and str(exc).startswith("Fetch redirected outside allowed HTTPS hosts:"):
        return "redirect"
    return "other"


def http_status(exc: Exception) -> int | None:
    if isinstance(exc, HTTPError):
        return exc.code
    if type(exc).__name__ == "DeepSeekError":
        match = re.fullmatch(r"DeepSeek HTTP (\d{3})", str(exc))
        if match:
            return int(match.group(1))
    return None


def failure_details(exc: Exception, default_stage: str) -> dict[str, object]:
    stage = failure_stage(exc, default_stage)
    frames = traceback.extract_tb(exc.__traceback__)[-8:]
    context = getattr(exc, "radar_failure_context", {})
    safe_context = {key: value for key, value in context.items()
                    if key in {"source_id", "fetch_method", "article_id", "batch_index", "batch_article_ids",
                               "model", "request_id", "token_usage", "local_repair_triggered", "fallback",
                               "retry_history", "model_request_failed"}}
    return {
        "failure_stage": stage,
        "skipped_reason": "model_request_failed" if getattr(exc, "model_request_failed", False) else f"{stage}_failed",
        "error_type": type(exc).__name__,
        "error_kind": error_kind(exc),
        "error_summary": _safe_message(exc),
        "http_status": http_status(exc),
        "stack": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in frames],
        **safe_context,
    }
