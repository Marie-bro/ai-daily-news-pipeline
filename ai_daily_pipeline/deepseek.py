from __future__ import annotations

import json
import errno
import logging
import os
import re
import socket
import ssl
import time
from http.client import RemoteDisconnected
from http.client import IncompleteRead
from datetime import datetime, timezone
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .deepseek_transport import open_request, proxy_settings, read_response


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, *, usage: dict[str, object] | None = None, model: str | None = None,
                 retry_history: list[dict[str, object]] | None = None, model_request_failed: bool = False,
                 finish_reason: str = "not_observable", response_parse_status: str = "not_observable") -> None:
        super().__init__(message)
        self.usage = usage
        self.model = model
        self.retry_history = retry_history or []
        self.model_request_failed = model_request_failed
        self.finish_reason = finish_reason
        self.response_parse_status = response_parse_status


_LOGGER = logging.getLogger(__name__)
_RETRY_ERRNOS = {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
                 errno.ENETRESET, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE,
                 10054, 10060, 10065}


def _transient_network_error(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, URLError):
        return _transient_network_error(exc.reason) if isinstance(exc.reason, BaseException) else False
    if isinstance(exc, socket.gaierror):
        return exc.errno == socket.EAI_AGAIN
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError,
                        ConnectionRefusedError, BrokenPipeError, RemoteDisconnected, IncompleteRead, ssl.SSLEOFError)):
        return True
    if isinstance(exc, ssl.SSLError):
        return "UNEXPECTED_EOF_WHILE_READING" in str(exc).upper() or "CONNECTION RESET" in str(exc).upper()
    return isinstance(exc, OSError) and exc.errno in _RETRY_ERRNOS


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    message = re.sub(r"https?://[^\s)]+", "[URL]", message)
    message = re.sub(r"(?i)(bearer|api[_ -]?key|secret|access[_ -]?token|password)(?:\s*[:=]\s*|\s+)\S+", r"\1=[REDACTED]", message)
    return message[:300]


def _read_existing_local_settings() -> dict[str, str]:
    """Use the Phase 1 local settings without copying credentials into this repo."""
    settings: dict[str, str] = {}
    settings_file = Path(__file__).resolve().parents[2] / "feishu-deepseek-assistant" / ".env"
    if not settings_file.exists():
        return settings
    for raw_line in settings_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"}:
            settings[key.strip()] = value.strip().strip("\"'")
    return settings


class DeepSeekClient:
    endpoint = "https://api.deepseek.com/chat/completions"
    max_attempts = 3
    retry_delays = (2.0, 5.0)

    def __init__(self, *, opener=None) -> None:
        local_settings = _read_existing_local_settings()
        self.key = os.getenv("DEEPSEEK_API_KEY", local_settings.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = os.getenv("DEEPSEEK_MODEL", local_settings.get("DEEPSEEK_MODEL", "")).strip()
        self.opener = opener
        self.proxy_mode = os.getenv("DEEPSEEK_PROXY_MODE", "direct").strip().lower()
        try:
            self.proxies = proxy_settings(self.proxy_mode, os.getenv("DEEPSEEK_PROXY_URL", ""))
            self.connect_timeout = float(os.getenv("DEEPSEEK_CONNECT_TIMEOUT_SECONDS", "15"))
            self.read_timeout = float(os.getenv("DEEPSEEK_READ_TIMEOUT_SECONDS", "90"))
            if not 0 < self.connect_timeout <= 90 or not 0 < self.read_timeout <= 300:
                raise ValueError("DeepSeek network timeouts are outside supported bounds")
        except ValueError as exc:
            raise DeepSeekError(str(exc)) from exc
        self.request_context: dict[str, object] = {}
        self.last_attempt_history: list[dict[str, object]] = []
        if not self.key:
            raise DeepSeekError("DEEPSEEK_API_KEY is missing. Set it in the environment or the existing Phase 1 .env file.")
        if not self.model:
            raise DeepSeekError("DEEPSEEK_MODEL is missing. Set it in the environment or the existing Phase 1 .env file.")

    def complete_json(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[str, dict[str, object], str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        self.last_attempt_history = []
        request_id = uuid.uuid4().hex
        payload_metrics = {"article_count": len(self.request_context.get("article_ids", [])), "clean_text_chars": None}
        if user_prompt.startswith("Verified candidates (process every one):\n"):
            try:
                candidates = json.loads(user_prompt.split("\n", 1)[1])["candidates"]
                payload_metrics = {"article_count": len(candidates),
                                   "clean_text_chars": sum(len(item.get("clean_text", "")) for item in candidates)}
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # Metrics never alter a model request or a business decision.
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            complete_response = False
            trace = {"request_id": request_id, "start_time": datetime.now(timezone.utc).isoformat(),
                     "batch_size": len(self.request_context.get("article_ids", [])), "max_output_tokens": max_tokens,
                     "payload_bytes": len(request.data), "proxy_mode": self.proxy_mode if self.opener is None else "injected",
                     "connect_timeout_seconds": self.connect_timeout, "read_timeout_seconds": self.read_timeout,
                     "failure_phase": "injected_transport" if self.opener is not None else "dns_resolve",
                     "http_status": None, "response_headers_received": False, "response_body_started": False,
                     **payload_metrics}
            try:
                response_context = (self.opener(request, timeout=self.read_timeout) if self.opener is not None else
                                    open_request(request, trace=trace, proxies=self.proxies,
                                                 connect_timeout=self.connect_timeout, read_timeout=self.read_timeout))
                with response_context as response:
                    raw_body = read_response(response, trace, measured=self.opener is None)
                    complete_response = True
                trace["failure_phase"] = "response_json_decode"
                body = json.loads(raw_body.decode("utf-8"))
            except (HTTPError, URLError, OSError, ValueError, IncompleteRead, RemoteDisconnected) as exc:
                retryable = _transient_network_error(exc)
                if isinstance(exc, HTTPError):
                    trace.update(response_headers_received=True, failure_phase="http_status", http_status=exc.code)
                event = {
                    **trace,
                    "batch_id": self.request_context.get("batch_id"),
                    "article_ids": self.request_context.get("article_ids", []),
                    "attempt": attempt,
                    "exception_type": type(exc).__name__,
                    "http_status": exc.code if isinstance(exc, HTTPError) else trace["http_status"],
                    "error_message": _safe_error(exc).replace(self.key, "[REDACTED]"),
                    "exception_message": _safe_error(exc).replace(self.key, "[REDACTED]"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "complete_response_received": complete_response,
                    "usage_recorded": False,
                    "retryable": retryable,
                    "finish_reason": "not_observable", "output_limit_reached": "not_observable",
                    "response_parse_status": "response_json_invalid" if trace["failure_phase"] == "response_json_decode" else "transport_failed",
                }
                self._record_attempt(event)
                if retryable and attempt < self.max_attempts:
                    time.sleep(self.retry_delays[attempt - 1])
                    continue
                message = (f"DeepSeek HTTP {exc.code}" if isinstance(exc, HTTPError)
                           else f"DeepSeek connection failed: {_safe_error(exc).replace(self.key, '[REDACTED]')}" if isinstance(exc, URLError)
                           else f"DeepSeek response could not be read: {_safe_error(exc).replace(self.key, '[REDACTED]')}")
                raise DeepSeekError(message, retry_history=list(self.last_attempt_history),
                                    model_request_failed=retryable and attempt == self.max_attempts) from exc
            choice_observed = (body.get("choices", [None])[0] if isinstance(body, dict)
                               and isinstance(body.get("choices"), list) and body["choices"] else None)
            finish = choice_observed.get("finish_reason") if isinstance(choice_observed, dict) else None
            observed_usage = body.get("usage") if isinstance(body, dict) else None
            completion = observed_usage.get("completion_tokens") if isinstance(observed_usage, dict) else None
            self._record_attempt({
                **trace,
                "finish_reason": finish if isinstance(finish, str) else "not_observable",
                "usage": {k: v for k, v in observed_usage.items() if type(v) is int} if isinstance(observed_usage, dict) else None,
                "prompt_tokens": observed_usage.get("prompt_tokens", "not_observable") if isinstance(observed_usage, dict) else "not_observable",
                "completion_tokens": completion if type(completion) is int else "not_observable",
                "total_tokens": observed_usage.get("total_tokens", "not_observable") if isinstance(observed_usage, dict) else "not_observable",
                "output_limit_reached": completion >= max_tokens if type(completion) is int else "not_observable",
                "response_parse_status": "response_json_parsed",
                "batch_id": self.request_context.get("batch_id"),
                "article_ids": self.request_context.get("article_ids", []),
                "attempt": attempt, "exception_type": None, "http_status": trace["http_status"],
                "error_message": None, "exception_message": None, "failure_phase": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "complete_response_received": True, "usage_recorded": False, "retryable": False,
            })
            break
        if not isinstance(body, dict):
            raise DeepSeekError("DeepSeek response root is not an object")
        usage = body.get("usage")
        if not isinstance(usage, dict):
            usage = None
        model = body.get("model") if isinstance(body.get("model"), str) else self.model
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek response has no assistant content", usage=usage, model=model) from exc
        if not isinstance(choice, dict):
            raise DeepSeekError("DeepSeek response has no assistant content", usage=usage, model=model)
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("DeepSeek returned an empty assistant content", usage=usage, model=model)
        if choice.get("finish_reason") not in (None, "stop"):
            raise DeepSeekError("DeepSeek response was not fully generated", usage=usage, model=model,
                                finish_reason=choice["finish_reason"], response_parse_status="incomplete_generation_not_parsed")
        return content.strip(), usage or {}, model

    def _record_attempt(self, event: dict[str, object]) -> None:
        self.last_attempt_history.append(event)
        _LOGGER.info("deepseek_request_attempt %s", json.dumps(event, ensure_ascii=False))
        callback = getattr(self, "on_attempt", None)
        if callback is not None:
            callback(event)

    def mark_usage_recorded(self) -> None:
        if not self.last_attempt_history or not self.last_attempt_history[-1]["complete_response_received"]:
            return
        self.last_attempt_history[-1]["usage_recorded"] = True
        _LOGGER.info("deepseek_usage_recorded batch_id=%s attempt=%s",
                     self.last_attempt_history[-1]["batch_id"], self.last_attempt_history[-1]["attempt"])
