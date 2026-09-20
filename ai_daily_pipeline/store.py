from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .models import Article, Enrichment

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  original_title TEXT NOT NULL,
  source TEXT NOT NULL,
  source_type TEXT NOT NULL,
  published_at TEXT NOT NULL,
  original_url TEXT NOT NULL UNIQUE,
  language TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  clean_text TEXT NOT NULL,
  fingerprint TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  verification_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_published_at_idx ON articles(published_at);
CREATE TABLE IF NOT EXISTS article_enrichments (
  article_id TEXT PRIMARY KEY REFERENCES articles(id),
  task TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  model TEXT NOT NULL,
  title_cn TEXT NOT NULL,
  title_original TEXT NOT NULL,
  source TEXT NOT NULL,
  published_at TEXT NOT NULL,
  original_url TEXT NOT NULL,
  key_points_original TEXT NOT NULL,
  translation TEXT NOT NULL,
  summary_cn TEXT NOT NULL,
  summary_en TEXT NOT NULL,
  relevance TEXT NOT NULL,
  useful_expressions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  prompt_cache_hit_tokens INTEGER,
  prompt_cache_miss_tokens INTEGER,
  reasoning_tokens INTEGER,
  raw_usage_json TEXT
);
CREATE INDEX IF NOT EXISTS model_usage_created_at_idx ON model_usage(created_at);
CREATE TABLE IF NOT EXISTS daily_delivery_sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_date TEXT NOT NULL,
  report_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  status TEXT NOT NULL,
  sent_at TEXT,
  message_id TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  force_resend INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_delivery_sends_lookup_idx
  ON daily_delivery_sends(report_date, report_id, target_type, target_id, force_resend, created_at DESC);
CREATE TABLE IF NOT EXISTS source_fetch_cache (
  source_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  body TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT
);
CREATE TABLE IF NOT EXISTS tech_enrichments (
  article_id TEXT PRIMARY KEY REFERENCES articles(id), task TEXT NOT NULL, generated_at TEXT NOT NULL,
  model TEXT NOT NULL, title_cn TEXT NOT NULL, title_original TEXT NOT NULL, source TEXT NOT NULL,
  published_at TEXT NOT NULL, original_url TEXT NOT NULL, category TEXT NOT NULL,
  original_language TEXT NOT NULL, what_happened TEXT NOT NULL, why_it_matters TEXT NOT NULL,
  importance_score INTEGER NOT NULL
);
"""


class ArticleStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(model_usage)")}
        if "raw_usage_json" not in columns:
            self.connection.execute("ALTER TABLE model_usage ADD COLUMN raw_usage_json TEXT")
            self.connection.commit()
        article_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(articles)")}
        if "source_region" not in article_columns:
            self.connection.execute("ALTER TABLE articles ADD COLUMN source_region TEXT NOT NULL DEFAULT 'unknown'")
        if "source_tier" not in article_columns:
            self.connection.execute("ALTER TABLE articles ADD COLUMN source_tier INTEGER NOT NULL DEFAULT 3")
        tech_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(tech_enrichments)")}
        for column in ("title_en", "what_happened_en", "why_it_matters_en"):
            if column not in tech_columns:
                self.connection.execute(f"ALTER TABLE tech_enrichments ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

    def is_known(self, url: str, body_fingerprint: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM articles WHERE original_url = ? OR fingerprint = ? LIMIT 1", (url, body_fingerprint)).fetchone()
        return row is not None

    def get_source_cache(self, source_id: str, url: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT body, etag, last_modified FROM source_fetch_cache WHERE source_id = ? AND url = ?", (source_id, url)
        ).fetchone()
        return {"body": row[0], "etag": row[1], "last_modified": row[2]} if row else None

    def save_source_cache(self, source_id: str, url: str, body: str, etag: str | None, last_modified: str | None) -> None:
        self.connection.execute(
            """INSERT INTO source_fetch_cache (source_id,url,body,etag,last_modified) VALUES (?,?,?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET url=excluded.url,body=excluded.body,
               etag=excluded.etag,last_modified=excluded.last_modified""",
            (source_id, url, body, etag, last_modified),
        )
        self.connection.commit()

    def add(self, article: Article) -> bool:
        if self.is_known(article.original_url, article.fingerprint):
            return False
        self.connection.execute(
            """INSERT INTO articles
            (id,category,title,original_title,source,source_type,published_at,original_url,language,raw_text,clean_text,fingerprint,created_at,verification_status,source_region,source_tier)
            VALUES (:id,:category,:title,:original_title,:source,:source_type,:published_at,:original_url,:language,:raw_text,:clean_text,:fingerprint,:created_at,:verification_status,:source_region,:source_tier)""",
            article.to_dict(),
        )
        self.connection.commit()
        return True

    def unenriched_articles(self, task: str, limit: int, *, published_after: str | None = None,
                            published_before: str | None = None) -> list[Article]:
        rows = self.connection.execute(
            """SELECT a.* FROM articles a
               LEFT JOIN tech_enrichments e ON e.article_id = a.id AND e.task = ?
               WHERE e.article_id IS NULL AND a.verification_status = 'source_verified'
                 AND (? IS NULL OR a.published_at >= ?)
                 AND (? IS NULL OR a.published_at <= ?)
               ORDER BY a.published_at DESC, a.id LIMIT ?""",
            (task, published_after, published_after, published_before, published_before, limit),
        ).fetchall()
        fields = [column[0] for column in self.connection.execute("SELECT * FROM articles LIMIT 0").description]
        return [Article(**dict(zip(fields, row, strict=True))) for row in rows]

    def save_enrichment(self, enrichment: Enrichment, replace: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.connection.execute(
            f"""{verb} INTO tech_enrichments
            (article_id,task,generated_at,model,title_cn,title_en,title_original,source,published_at,original_url,
             category,original_language,what_happened,what_happened_en,why_it_matters,why_it_matters_en,importance_score)
            VALUES (:article_id,:task,:generated_at,:model,:title_cn,:title_en,:title_original,:source,:published_at,:original_url,
                    :category,:original_language,:what_happened,:what_happened_en,:why_it_matters,:why_it_matters_en,:importance_score)""",
            enrichment.to_record(),
        )

    def record_usage(self, *, task: str, model: str, created_at: str, usage: dict[str, object]) -> None:
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        self.connection.execute(
            """INSERT INTO model_usage
            (task,model,created_at,input_tokens,output_tokens,total_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,reasoning_tokens,raw_usage_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task, model, created_at, usage.get("prompt_tokens"), usage.get("completion_tokens"),
             usage.get("total_tokens"), usage.get("prompt_cache_hit_tokens"),
             usage.get("prompt_cache_miss_tokens"), reasoning, json.dumps(usage, ensure_ascii=False)),
        )

    def commit(self) -> None:
        self.connection.commit()

    def daily_total_tokens(self, day_prefix: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM model_usage WHERE created_at LIKE ?", (f"{day_prefix}%",)
        ).fetchone()
        return int(row[0])

    def purge_articles_for_hosts(self, blocked_hosts: tuple[str, ...]) -> int:
        rows = self.connection.execute("SELECT id, original_url FROM articles").fetchall()
        article_ids = []
        for article_id, original_url in rows:
            hostname = (urlparse(original_url).hostname or "").lower()
            if any(hostname == host or hostname.endswith("." + host) for host in blocked_hosts):
                article_ids.append(article_id)
        for article_id in article_ids:
            self.connection.execute("DELETE FROM article_enrichments WHERE article_id = ?", (article_id,))
            self.connection.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        self.connection.commit()
        return len(article_ids)

    def usage_rows(self) -> list[dict[str, object]]:
        cursor = self.connection.execute("SELECT * FROM model_usage ORDER BY id")
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def delivery_already_recorded(self, *, report_date: str, report_id: str, target_type: str, target_id: str) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM daily_delivery_sends
               WHERE report_date = ? AND report_id = ? AND target_type = ? AND target_id = ?
                 AND force_resend = 0
               LIMIT 1""",
            (report_date, report_id, target_type, target_id),
        ).fetchone()
        return row is not None

    def create_delivery_attempt(self, *, report_date: str, report_id: str, target_type: str, target_id: str,
                                request_id: str, created_at: str, force_resend: bool) -> int:
        cursor = self.connection.execute(
            """INSERT INTO daily_delivery_sends
               (report_date, report_id, target_type, target_id, request_id, status, force_resend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (report_date, report_id, target_type, target_id, request_id, int(force_resend), created_at, created_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_delivery_attempt(self, attempt_id: int, *, status: str, updated_at: str, message_id: str | None = None,
                                retry_count: int = 0, error_summary: str | None = None) -> None:
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("invalid delivery status")
        self.connection.execute(
            """UPDATE daily_delivery_sends
               SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                   message_id = ?, retry_count = ?, error_summary = ?, updated_at = ?
               WHERE id = ?""",
            (status, status, updated_at, message_id, retry_count, error_summary, updated_at, attempt_id),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
