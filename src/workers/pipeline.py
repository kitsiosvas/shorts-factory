from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.config import Settings, TopicConfig, get_settings
from src.db import Database, JobStatus
from src.discover import discover_for_topic
from src.ingest import download_video
from src.intelligence import get_transcript, pick_highlights
from src.publish.youtube import YouTubePublisher
from src.render import (
    burn_hook_text,
    extract_clip,
    generate_description,
    generate_hook_title,
    to_vertical_916,
)
from src.render.ffmpeg_utils import probe_duration

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class Pipeline:
    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self.db = db or Database(self.settings.database_path)
        self.youtube = YouTubePublisher()

    def discover(self, topic_id: str | None = None) -> dict:
        topics = self.settings.enabled_topics()
        if topic_id:
            topics = [t for t in topics if t.id == topic_id]
            if not topics:
                all_topics = self.settings.load_topics_file().topics
                match = next((t for t in all_topics if t.id == topic_id), None)
                if match and not match.enabled:
                    return {
                        "inserted": 0,
                        "skipped": 0,
                        "error": f"Topic '{topic_id}' is disabled (kill-switch)",
                    }
                return {"inserted": 0, "skipped": 0, "error": f"Unknown topic '{topic_id}'"}

        inserted = 0
        skipped = 0
        for topic in topics:
            try:
                candidates = discover_for_topic(topic)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Discover failed for %s", topic.id)
                return {"inserted": inserted, "skipped": skipped, "error": str(exc)}
            for video in candidates:
                row_id = self.db.insert_source(
                    youtube_video_id=video.youtube_video_id,
                    topic_id=topic.id,
                    title=video.title,
                    channel_title=video.channel_title,
                    url=video.url,
                    duration_sec=video.duration_sec,
                    view_count=video.view_count,
                )
                if row_id is None:
                    skipped += 1
                else:
                    inserted += 1
        return {"inserted": inserted, "skipped": skipped}

    def process_next(self) -> dict:
        source = self.db.next_by_status(JobStatus.DISCOVERED)
        if source is None:
            source = self._next_retryable()
        if source is None:
            return {"status": "idle", "message": "No discovered sources to process"}

        if not self._topic_enabled(source.topic_id):
            self.db.update_source(source.id, status=JobStatus.SKIPPED.value, error="topic disabled")
            return {"status": "skipped", "source_id": source.id, "reason": "topic disabled"}

        pipeline = self.settings.pipeline()
        try:
            if pipeline.llm_provider == "none":
                raise RuntimeError(
                    "llm_provider is 'none'. Set pipeline.llm_provider to 'gemini' "
                    "(or 'ollama') in topics.yaml — silence-based clipping is disabled."
                )
            if pipeline.llm_provider == "gemini" and not self.settings.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY missing. Add it to .env then restart the server."
                )

            try:
                raw_path = download_video(
                    source.url,
                    self.settings.media_raw_dir,
                    source.youtube_video_id,
                )
            except Exception as download_exc:  # noqa: BLE001
                msg = str(download_exc)
                if "not available" in msg.lower() or "private" in msg.lower():
                    self.db.update_source(
                        source.id,
                        status=JobStatus.SKIPPED.value,
                        error=msg[:2000],
                    )
                    return {
                        "status": "skipped",
                        "source_id": source.id,
                        "reason": "source video unavailable",
                        "error": msg,
                    }
                raise
            self.db.update_source(
                source.id,
                status=JobStatus.DOWNLOADED.value,
                raw_path=str(raw_path),
                error=None,
            )

            duration = probe_duration(raw_path)
            topic = self._topic_by_id(source.topic_id)
            topic_name = topic.display_name if topic else source.topic_id

            llm_used_today = self.db.get_quota_today("llm")
            if llm_used_today >= pipeline.max_llm_highlights_per_day:
                raise RuntimeError(
                    f"LLM daily cap reached ({llm_used_today}/"
                    f"{pipeline.max_llm_highlights_per_day}). Raise "
                    "max_llm_highlights_per_day or wait until tomorrow."
                )

            transcript = get_transcript(
                video_id=source.youtube_video_id,
                video_path=raw_path,
                captions_dir=self.settings.captions_dir,
                use_whisper_fallback=pipeline.use_whisper,
                whisper_model=pipeline.whisper_model,
            )
            if not transcript.segments:
                raise RuntimeError(
                    "No transcript available (YouTube captions empty). "
                    "Enable use_whisper: true in topics.yaml or skip this source."
                )

            clips, highlight_meta = pick_highlights(
                transcript,
                provider=pipeline.llm_provider,
                video_duration=duration,
                min_sec=float(pipeline.clip_min_seconds),
                max_sec=float(pipeline.clip_max_seconds),
                topic_name=topic_name,
                gemini_api_key=self.settings.gemini_api_key,
                gemini_model=pipeline.gemini_model,
                ollama_base_url=self.settings.ollama_base_url,
                ollama_model=pipeline.ollama_model,
            )
            if not clips:
                raise RuntimeError(
                    f"LLM returned no usable clips ({highlight_meta}). "
                    "Source skipped rather than silence-cutting."
                )

            best = clips[0]
            start, end = best.start_sec, best.end_sec
            hook_from_llm = best.hook_title
            self.db.add_quota("llm", 1)
            logger.info(
                "LLM highlight %.1f-%.1fs score=%.0f cost~$%s provider=%s reason=%s",
                start,
                end,
                best.score,
                highlight_meta.get("estimated_cost_usd"),
                highlight_meta.get("provider"),
                best.reason[:120],
            )

            clip_path = self.settings.media_raw_dir / f"{source.youtube_video_id}_clip.mp4"
            start, end = extract_clip(
                raw_path,
                clip_path,
                max_seconds=pipeline.clip_max_seconds,
                start_sec=start,
                end_sec=end,
            )

            vertical_path = self.settings.media_raw_dir / f"{source.youtube_video_id}_vert.mp4"
            to_vertical_916(clip_path, vertical_path)

            title = generate_hook_title(hook_from_llm or source.title, None)
            description = generate_description(source.title, source.url, topic_name)

            ready_path = self.settings.media_ready_dir / f"{source.youtube_video_id}.mp4"
            burn_hook_text(vertical_path, ready_path, title)

            self.db.update_source(
                source.id,
                status=JobStatus.RENDERED.value,
                ready_path=str(ready_path),
                clip_start_sec=start,
                clip_end_sec=end,
                generated_title=title,
                generated_description=description,
                error=None,
            )
            for temp in (clip_path, vertical_path):
                if temp.exists():
                    temp.unlink(missing_ok=True)

            return {
                "status": "rendered",
                "source_id": source.id,
                "ready_path": str(ready_path),
                "title": title,
                "clip": {"start": start, "end": end},
                "highlight": highlight_meta,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Process failed source_id=%s", source.id)
            self.db.mark_failed(source.id, str(exc))
            return {"status": "failed", "source_id": source.id, "error": str(exc)}

    def publish_next(self) -> dict:
        pipeline = self.settings.pipeline()
        published_today = self.db.count_posts_today("youtube")
        if published_today >= pipeline.max_publishes_per_day:
            return {
                "status": "budget",
                "message": f"Daily publish cap reached ({pipeline.max_publishes_per_day})",
            }

        quota_used = self.db.get_quota_today("youtube")
        if quota_used + pipeline.upload_quota_cost > pipeline.youtube_daily_quota_budget:
            return {
                "status": "budget",
                "message": (
                    f"YouTube quota budget exhausted "
                    f"({quota_used}/{pipeline.youtube_daily_quota_budget})"
                ),
            }

        source = self.db.next_by_status(JobStatus.RENDERED)
        if source is None:
            return {"status": "idle", "message": "No rendered Shorts ready to publish"}

        if not self._topic_enabled(source.topic_id):
            self.db.update_source(source.id, status=JobStatus.SKIPPED.value, error="topic disabled")
            return {"status": "skipped", "source_id": source.id, "reason": "topic disabled"}

        if not source.ready_path or not Path(source.ready_path).exists():
            self.db.mark_failed(source.id, "ready_path missing")
            return {"status": "failed", "source_id": source.id, "error": "ready_path missing"}

        result = self.youtube.publish_video(
            video_path=Path(source.ready_path),
            title=source.generated_title or f"{source.title} #Shorts",
            description=source.generated_description or f"{source.title}\n#Shorts",
        )
        if not result.success:
            self.db.mark_failed(source.id, result.error or "upload failed")
            self.db.insert_post(
                source_id=source.id,
                platform="youtube",
                platform_post_id=None,
                status="failed",
                error=result.error,
            )
            return {"status": "failed", "source_id": source.id, "error": result.error}

        self.db.add_quota("youtube", pipeline.upload_quota_cost)
        self.db.insert_post(
            source_id=source.id,
            platform="youtube",
            platform_post_id=result.platform_post_id,
            status="published",
        )
        self.db.update_source(source.id, status=JobStatus.PUBLISHED.value, error=None)

        ready = Path(source.ready_path)
        archive = self.settings.media_archive_dir / ready.name
        try:
            shutil.move(str(ready), str(archive))
            self.db.update_source(source.id, ready_path=str(archive))
        except OSError:
            logger.warning("Could not archive %s", ready)

        return {
            "status": "published",
            "source_id": source.id,
            "youtube_id": result.platform_post_id,
            "url": f"https://youtube.com/shorts/{result.platform_post_id}",
        }

    def status(self) -> dict:
        pipeline = self.settings.pipeline()
        return {
            "sources": self.db.status_counts(),
            "youtube_posts_today": self.db.count_posts_today("youtube"),
            "youtube_quota_today": self.db.get_quota_today("youtube"),
            "llm_highlights_today": self.db.get_quota_today("llm"),
            "max_llm_highlights_per_day": pipeline.max_llm_highlights_per_day,
            "llm_provider": pipeline.llm_provider,
            "quota_budget": pipeline.youtube_daily_quota_budget,
            "max_publishes_per_day": pipeline.max_publishes_per_day,
            "enabled_topics": [t.id for t in self.settings.enabled_topics()],
            "gemini_cost_hint": {
                "model": pipeline.gemini_model,
                "per_video_usd_paid_tier_approx": "0.002 - 0.01",
                "note": "Free tier often $0; default model gemini-2.0-flash with automatic fallbacks",
            },
        }

    def _topic_enabled(self, topic_id: str) -> bool:
        return any(t.id == topic_id for t in self.settings.enabled_topics())

    def _topic_by_id(self, topic_id: str) -> TopicConfig | None:
        for topic in self.settings.load_topics_file().topics:
            if topic.id == topic_id:
                return topic
        return None

    def _next_retryable(self):
        from src.db import Source

        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE status = ? AND retry_count < ?
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (JobStatus.FAILED.value, MAX_RETRIES),
            ).fetchone()
            if not row:
                return None
            source = Source(**dict(row))
        self.db.update_source(source.id, status=JobStatus.DISCOVERED.value, error=None)
        return self.db.get_source(source.id)
