"""Phase 6.5 pull-only worker that writes approved owner favorites into Obsidian."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_API = "https://news.mariespace.cn/api/favorites"
DEFAULT_RELATIVE_DIR = "待读清单"
REQUIRED_FIELDS = ("article_id", "title_en", "title_zh", "what_happened_en", "what_happened_zh", "why_it_matters_en", "why_it_matters_zh", "source", "published_at", "original_url", "lease_id")


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerSettings:
    api_base: str
    token: str
    vault_path: Path
    relative_dir: str

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.token:
            missing.append("MARIESPACE_WORKER_PULL_TOKEN")
        if self.vault_path == Path("."):
            missing.append("OBSIDIAN_VAULT_PATH")
        return missing


def load_local_environment(path: Path) -> None:
    if not path.is_file():
        return
    allowed = {"MARIESPACE_FAVORITES_API", "MARIESPACE_WORKER_PULL_TOKEN", "OBSIDIAN_VAULT_PATH", "OBSIDIAN_FAVORITES_RELATIVE_DIR"}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def settings_from_environment(environment: dict[str, str] | None = None) -> WorkerSettings:
    environment = environment or os.environ
    return WorkerSettings(
        api_base=environment.get("MARIESPACE_FAVORITES_API", DEFAULT_API).rstrip("/"),
        token=environment.get("MARIESPACE_WORKER_PULL_TOKEN", "").strip(),
        vault_path=Path(environment.get("OBSIDIAN_VAULT_PATH", "")).expanduser(),
        relative_dir=environment.get("OBSIDIAN_FAVORITES_RELATIVE_DIR", DEFAULT_RELATIVE_DIR),
    )


def _safe_relative_directory(value: str) -> Path:
    raw = value.strip() if isinstance(value, str) else ""
    # Validate each directory name separately: slash and backslash are allowed
    # only as separators, never as part of a filename.
    parts = re.split(r"[\\\\/]", raw)
    invalid = '<>:"|?*'
    if (
        not raw
        or not parts
        or any(not part or part in {".", ".."} or any(char in invalid for char in part) for part in parts)
    ):
        raise WorkerError("Invalid Obsidian favorites path configuration")
    path = Path(*parts)
    if path.is_absolute() or ".." in path.parts:
        raise WorkerError("Invalid Obsidian favorites path configuration")
    return path


def target_directory(settings: WorkerSettings) -> Path:
    if not str(settings.vault_path):
        raise WorkerError("OBSIDIAN_VAULT_PATH is required")
    root = settings.vault_path.resolve()
    if not root.is_dir():
        raise WorkerError("configured Obsidian vault does not exist")
    destination = (root / _safe_relative_directory(settings.relative_dir)).resolve()
    if root not in destination.parents and destination != root:
        raise WorkerError("favorite destination escapes the Obsidian vault")
    return destination


def validate_startup_configuration(settings: WorkerSettings) -> None:
    missing = settings.missing()
    if missing:
        raise WorkerError("missing required configuration: " + ", ".join(missing))
    # Resolve and probe the configured target before any network work so a bad
    # local setting cannot leave a queue task in a processing lease.
    directory = target_directory(settings)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", newline="\n", dir=directory, prefix=".mariespace-write-check-", suffix=".tmp", delete=False) as handle:
            handle.write("MarieSpace Obsidian write check\n")
            handle.flush()
            probe = Path(handle.name)
        try:
            if probe.read_text(encoding="utf-8") != "MarieSpace Obsidian write check\n":
                raise WorkerError("configured Obsidian vault is not writable")
        finally:
            probe.unlink(missing_ok=True)
    except OSError as exc:
        raise WorkerError("configured Obsidian vault is not writable") from exc


def _quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def validate_job(job: object) -> dict[str, str]:
    if not isinstance(job, dict):
        raise WorkerError("invalid favorite job")
    result: dict[str, str] = {}
    for field in REQUIRED_FIELDS:
        value = job.get(field)
        if not isinstance(value, str) or not value.strip():
            raise WorkerError(f"favorite job is missing {field}")
        result[field] = value.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", result["article_id"]):
        raise WorkerError("favorite job has invalid article ID")
    if not result["original_url"].startswith("https://"):
        raise WorkerError("favorite job has invalid original URL")
    return result


def render_markdown(job: dict[str, str], *, saved_at: str) -> str:
    return f'''---
type: reading
status: unread
category: ai
saved_at: {_quote_yaml(saved_at)}
source: {_quote_yaml(job["source"])}
original_url: {_quote_yaml(job["original_url"])}
article_id: {_quote_yaml(job["article_id"])}
---

# {job["title_en"]}

# {job["title_zh"]}

## What happened?

{job["what_happened_en"]}

## 发生了什么？

{job["what_happened_zh"]}

## Why it matters?

{job["why_it_matters_en"]}

## 为什么值得关注？

{job["why_it_matters_zh"]}

## Source / 来源

{job["source"]}

## Read Original / 查看原文

{job["original_url"]}

## Reading Status / 阅读状态

unread

## My Notes / 我的收获

### Key Ideas / 核心观点

### What I Learned / 我学到了什么

### Connections / 与已有知识的联系

### Actions / 可以实际尝试什么

### Questions / 后续问题
'''


def write_favorite(job: object, settings: WorkerSettings, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> tuple[Path, bool]:
    validated = validate_job(job)
    directory = target_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{validated['article_id']}.md"
    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        if f'article_id: "{validated["article_id"]}"' not in existing:
            raise WorkerError("existing favorite file could not be verified")
        return destination, False
    content = render_markdown(validated, saved_at=now().astimezone().isoformat(timespec="seconds"))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=directory, prefix=".favorite-", suffix=".tmp", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        if destination.exists():
            return destination, False
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if destination.read_text(encoding="utf-8") != content:
        raise WorkerError("favorite file read-back verification failed")
    return destination, True


def request_json(url: str, token: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"
    request = Request(url, data=data, method="POST", headers={"authorization": f"Bearer {token}", "content-type": "application/json", "user-agent": "MarieSpace-Obsidian-Worker/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise WorkerError(f"favorite backend request failed: HTTP {exc.code}") from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
        raise WorkerError("favorite backend request failed") from exc
    if not isinstance(value, dict) or not value.get("ok"):
        raise WorkerError("favorite backend rejected worker request")
    return value


def process_once(settings: WorkerSettings) -> str:
    validate_startup_configuration(settings)
    claimed = request_json(f"{settings.api_base}/claim", settings.token)
    job = claimed.get("job")
    if job is None:
        return "idle"
    try:
        validated = validate_job(job)
        _path, created = write_favorite(validated, settings)
        status = "completed"
        result = "completed" if created else "already_written"
    except Exception:
        validated = validate_job(job)
        status = "failed"
        result = "failed"
    request_json(f"{settings.api_base}/complete", settings.token, {"article_id": validated["article_id"], "lease_id": validated["lease_id"], "status": status})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull owner favorites and write them into the configured local Obsidian vault.")
    parser.add_argument("--once", action="store_true", help="Claim at most one task and exit.")
    parser.add_argument("--interval", type=int, default=45, help="Polling interval in seconds (default: 45).")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / "deploy" / "phase6.5-worker.env", help="Local private environment file.")
    parser.add_argument("--check-config", action="store_true", help="Validate local configuration without network access.")
    args = parser.parse_args()
    load_local_environment(args.env_file)
    settings = settings_from_environment()
    if args.check_config:
        try:
            validate_startup_configuration(settings)
        except WorkerError as exc:
            print(str(exc))
            return 2
        print("configuration is valid")
        return 0
    try:
        validate_startup_configuration(settings)
    except WorkerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.interval < 30 or args.interval > 60:
        parser.error("--interval must be between 30 and 60 seconds")
    while True:
        try:
            print(process_once(settings))
        except WorkerError as exc:
            print(f"worker_error: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
