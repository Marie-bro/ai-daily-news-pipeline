from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Article

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
"""


class ArticleStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)

    def is_known(self, url: str, body_fingerprint: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM articles WHERE original_url = ? OR fingerprint = ? LIMIT 1", (url, body_fingerprint)).fetchone()
        return row is not None

    def add(self, article: Article) -> bool:
        if self.is_known(article.original_url, article.fingerprint):
            return False
        self.connection.execute(
            """INSERT INTO articles VALUES (:id,:category,:title,:original_title,:source,:source_type,:published_at,:original_url,:language,:raw_text,:clean_text,:fingerprint,:created_at,:verification_status)""",
            article.to_dict(),
        )
        self.connection.commit()
        return True

    def close(self) -> None:
        self.connection.close()
