from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, *, usage: dict[str, object] | None = None, model: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage
        self.model = model


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

    def __init__(self, *, opener=urlopen) -> None:
        local_settings = _read_existing_local_settings()
        self.key = os.getenv("DEEPSEEK_API_KEY", local_settings.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = os.getenv("DEEPSEEK_MODEL", local_settings.get("DEEPSEEK_MODEL", "")).strip()
        self.opener = opener
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
        try:
            with self.opener(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise DeepSeekError(f"DeepSeek HTTP {exc.code}") from exc
        except URLError as exc:
            raise DeepSeekError(f"DeepSeek connection failed: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise DeepSeekError(f"DeepSeek response could not be read: {exc}") from exc
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
            raise DeepSeekError("DeepSeek response was not fully generated", usage=usage, model=model)
        return content.strip(), usage or {}, model
