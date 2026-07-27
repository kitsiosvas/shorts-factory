from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    RENDERED = "rendered"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Source:
    id: int
    youtube_video_id: str
    topic_id: str
    title: str
    channel_title: str
    url: str
    duration_sec: int | None
    view_count: int | None
    status: str
    raw_path: str | None
    ready_path: str | None
    clip_start_sec: float | None
    clip_end_sec: float | None
    generated_title: str | None
    generated_description: str | None
    error: str | None
    retry_count: int
    created_at: str
    updated_at: str


@dataclass
class Post:
    id: int
    source_id: int
    platform: str
    platform_post_id: str | None
    status: str
    error: str | None
    created_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id TEXT NOT NULL UNIQUE,
    topic_id TEXT NOT NULL,
    title TEXT NOT NULL,
    channel_title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    duration_sec INTEGER,
    view_count INTEGER,
    status TEXT NOT NULL,
    raw_path TEXT,
    ready_path TEXT,
    clip_start_sec REAL,
    clip_end_sec REAL,
    generated_title TEXT,
    generated_description TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    platform_post_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS quota_usage (
    day TEXT NOT NULL,
    platform TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, platform)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(**dict(row))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def source_exists(self, youtube_video_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sources WHERE youtube_video_id = ?",
                (youtube_video_id,),
            ).fetchone()
            return row is not None

    def insert_source(
        self,
        *,
        youtube_video_id: str,
        topic_id: str,
        title: str,
        channel_title: str,
        url: str,
        duration_sec: int | None,
        view_count: int | None,
    ) -> int | None:
        if self.source_exists(youtube_video_id):
            return None
        now = _utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sources (
                    youtube_video_id, topic_id, title, channel_title, url,
                    duration_sec, view_count, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    youtube_video_id,
                    topic_id,
                    title,
                    channel_title,
                    url,
                    duration_sec,
                    view_count,
                    JobStatus.DISCOVERED,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_source(self, source_id: int) -> Source | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            return _row_to_source(row) if row else None

    def next_by_status(self, status: JobStatus) -> Source | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (status.value,),
            ).fetchone()
            return _row_to_source(row) if row else None

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _utcnow()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [source_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE sources SET {columns} WHERE id = ?", values)

    def mark_failed(self, source_id: int, error: str, *, increment_retry: bool = True) -> None:
        source = self.get_source(source_id)
        retry = (source.retry_count + 1) if source and increment_retry else (source.retry_count if source else 0)
        self.update_source(
            source_id,
            status=JobStatus.FAILED.value,
            error=error[:2000],
            retry_count=retry,
        )

    def insert_post(
        self,
        *,
        source_id: int,
        platform: str,
        platform_post_id: str | None,
        status: str,
        error: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO posts (source_id, platform, platform_post_id, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_id, platform, platform_post_id, status, error, _utcnow()),
            )
            return int(cur.lastrowid)

    def count_posts_today(self, platform: str) -> int:
        today = date.today().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM posts
                WHERE platform = ? AND status = 'published' AND date(created_at) = ?
                """,
                (platform, today),
            ).fetchone()
            return int(row["c"]) if row else 0

    def add_quota(self, platform: str, units: int) -> int:
        day = date.today().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO quota_usage (day, platform, units) VALUES (?, ?, ?)
                ON CONFLICT(day, platform) DO UPDATE SET units = units + excluded.units
                """,
                (day, platform, units),
            )
            row = conn.execute(
                "SELECT units FROM quota_usage WHERE day = ? AND platform = ?",
                (day, platform),
            ).fetchone()
            return int(row["units"]) if row else units

    def get_quota_today(self, platform: str) -> int:
        day = date.today().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT units FROM quota_usage WHERE day = ? AND platform = ?",
                (day, platform),
            ).fetchone()
            return int(row["units"]) if row else 0

    def status_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM sources GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["c"]) for row in rows}
