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


class RenderStatus(StrEnum):
    RENDERED = "rendered"
    PUBLISHED = "published"
    FAILED = "failed"


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
class Render:
    id: int
    source_id: int
    clip_index: int
    start_sec: float
    end_sec: float
    ready_path: str | None
    generated_title: str | None
    generated_description: str | None
    verifier_score: float | None
    status: str
    error: str | None
    created_at: str
    updated_at: str
    kind: str = "clip"  # clip | owned


@dataclass
class Post:
    id: int
    source_id: int
    platform: str
    platform_post_id: str | None
    status: str
    error: str | None
    created_at: str
    render_id: int | None = None


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

CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    clip_index INTEGER NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    ready_path TEXT,
    generated_title TEXT,
    generated_description TEXT,
    verifier_score REAL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'clip',
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    platform_post_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    render_id INTEGER,
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


def _row_to_render(row: sqlite3.Row) -> Render:
    data = dict(row)
    data.setdefault("kind", "clip")
    return Render(**data)


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
            post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
            if "render_id" not in post_cols:
                conn.execute("ALTER TABLE posts ADD COLUMN render_id INTEGER")
            render_cols = {r[1] for r in conn.execute("PRAGMA table_info(renders)")}
            if "kind" not in render_cols:
                conn.execute(
                    "ALTER TABLE renders ADD COLUMN kind TEXT NOT NULL DEFAULT 'clip'"
                )

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

    def insert_render(
        self,
        *,
        source_id: int,
        clip_index: int,
        start_sec: float,
        end_sec: float,
        ready_path: str | None,
        generated_title: str | None,
        generated_description: str | None,
        verifier_score: float | None,
        status: str = RenderStatus.RENDERED.value,
        error: str | None = None,
        kind: str = "clip",
    ) -> int:
        now = _utcnow()
        kind = (kind or "clip").strip().lower()
        if kind not in {"clip", "owned"}:
            kind = "clip"
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO renders (
                    source_id, clip_index, start_sec, end_sec, ready_path,
                    generated_title, generated_description, verifier_score,
                    status, error, created_at, updated_at, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    clip_index,
                    start_sec,
                    end_sec,
                    ready_path,
                    generated_title,
                    generated_description,
                    verifier_score,
                    status,
                    error,
                    now,
                    now,
                    kind,
                ),
            )
            return int(cur.lastrowid)

    def source_has_owned_render(self, source_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM renders
                WHERE source_id = ? AND kind = 'owned'
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            return row is not None

    def next_source_for_owned(
        self,
        *,
        topic_id: str | None = None,
    ) -> Source | None:
        """Next downloaded/rendered source with no owned render yet."""
        with self.connect() as conn:
            if topic_id:
                row = conn.execute(
                    """
                    SELECT s.* FROM sources s
                    WHERE s.status IN (?, ?)
                      AND s.topic_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM renders r
                          WHERE r.source_id = s.id AND r.kind = 'owned'
                      )
                    ORDER BY s.created_at ASC
                    LIMIT 1
                    """,
                    (
                        JobStatus.DOWNLOADED.value,
                        JobStatus.RENDERED.value,
                        topic_id,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT s.* FROM sources s
                    WHERE s.status IN (?, ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM renders r
                          WHERE r.source_id = s.id AND r.kind = 'owned'
                      )
                    ORDER BY s.created_at ASC
                    LIMIT 1
                    """,
                    (JobStatus.DOWNLOADED.value, JobStatus.RENDERED.value),
                ).fetchone()
            return _row_to_source(row) if row else None

    def get_render(self, render_id: int) -> Render | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
            return _row_to_render(row) if row else None

    def next_render_by_status(self, status: RenderStatus) -> Render | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM renders
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (status.value,),
            ).fetchone()
            return _row_to_render(row) if row else None

    def update_render(self, render_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _utcnow()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [render_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE renders SET {columns} WHERE id = ?", values)

    def list_renders_for_source(self, source_id: int) -> list[Render]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM renders
                WHERE source_id = ?
                ORDER BY clip_index ASC
                """,
                (source_id,),
            ).fetchall()
            return [_row_to_render(r) for r in rows]

    def render_status_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM renders GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["c"]) for row in rows}

    def source_has_unpublished_renders(self, source_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM renders
                WHERE source_id = ? AND status = ?
                LIMIT 1
                """,
                (source_id, RenderStatus.RENDERED.value),
            ).fetchone()
            return row is not None

    def insert_post(
        self,
        *,
        source_id: int,
        platform: str,
        platform_post_id: str | None,
        status: str,
        error: str | None = None,
        render_id: int | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO posts (
                    source_id, platform, platform_post_id, status, error, created_at, render_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, platform, platform_post_id, status, error, _utcnow(), render_id),
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
