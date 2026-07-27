from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.config import Settings, TopicConfig, get_settings
from src.db import Database, JobStatus, RenderStatus
from src.discover import discover_for_topic
from src.ingest import download_video
from src.intelligence import get_transcript, pick_highlights
from src.intelligence.highlights import verify_clip_gemini
from src.intelligence.models import HighlightClip
from src.intelligence.words import Word, snap_clip_to_sentences, transcribe_window_words
from src.publish.youtube import YouTubePublisher
from src.render import (
    burn_hook_text,
    extract_clip,
    generate_description,
    generate_hook_title,
    to_vertical_916,
)
from src.render.ffmpeg_utils import probe_duration
from src.render.subtitles import segments_to_ass, words_to_ass

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _windows_overlap(
    start_a: float,
    end_a: float,
    start_b: float,
    end_b: float,
    *,
    min_gap: float,
) -> bool:
    return not (end_a + min_gap <= start_b or end_b + min_gap <= start_a)


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
                max_clips_per_source=pipeline.max_clips_per_source,
                picker_chunk_sec=float(pipeline.picker_chunk_sec),
                clip_min_gap_sec=float(pipeline.clip_min_gap_sec),
            )
            if not clips:
                raise RuntimeError(
                    f"LLM returned no usable clips ({highlight_meta}). "
                    "Source skipped rather than silence-cutting."
                )

            self.db.add_quota("llm", 1)

            accepted: list[tuple[HighlightClip, float, float, list[Word], float]] = []
            soft_candidates: list[tuple[float, HighlightClip, float, float, list[Word]]] = []
            rejections: list[str] = []
            min_gap = float(pipeline.clip_min_gap_sec)

            for cand in clips:
                if cand.score < pipeline.min_clip_score:
                    rejections.append(
                        f"{cand.start_sec:.0f}-{cand.end_sec:.0f}s: "
                        f"picker score {cand.score:.0f} < floor"
                    )
                    continue

                start, end = cand.start_sec, cand.end_sec
                clip_words: list[Word] = []
                if pipeline.snap_to_sentences:
                    try:
                        words = transcribe_window_words(
                            raw_path,
                            start,
                            end,
                            model_name=pipeline.snap_whisper_model,
                        )
                        snapped_start, snapped_end, clip_words = snap_clip_to_sentences(
                            words,
                            start,
                            end,
                            min_sec=float(pipeline.clip_min_seconds),
                            max_sec=float(pipeline.clip_max_seconds),
                        )
                        logger.info(
                            "Snap %.1f-%.1fs -> %.1f-%.1fs (%d words)",
                            start,
                            end,
                            snapped_start,
                            snapped_end,
                            len(clip_words),
                        )
                        start, end = snapped_start, snapped_end
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Sentence snapping failed; using raw LLM bounds"
                        )

                if any(
                    _windows_overlap(start, end, a_start, a_end, min_gap=min_gap)
                    for _, a_start, a_end, _, _ in accepted
                ):
                    rejections.append(
                        f"{start:.0f}-{end:.0f}s: overlaps an already-accepted clip"
                    )
                    continue

                clip_text = (
                    " ".join(w.text for w in clip_words)
                    if clip_words
                    else transcript.text_between(start, end)
                )

                verifier_score = float(cand.score)
                if pipeline.enable_verifier and pipeline.llm_provider == "gemini":
                    v = verify_clip_gemini(
                        clip_text,
                        api_key=self.settings.gemini_api_key or "",
                        model=pipeline.gemini_model,
                    )
                    verifier_score = float(v["score"])
                    logger.info(
                        "Verifier %.0f-%.0fs verdict=%s score=%.0f issues=%s",
                        start,
                        end,
                        v["verdict"],
                        v["score"],
                        v["issues"],
                    )
                    hard_pass = v["verdict"] == "pass" and (
                        v["score"] < 0 or v["score"] >= pipeline.min_clip_score
                    )
                    if not hard_pass:
                        rejections.append(
                            f"{start:.0f}-{end:.0f}s: verifier {v['verdict']} "
                            f"score={v['score']:.0f} ({v['issues']})"
                        )
                        if (
                            pipeline.verifier_soft_floor > 0
                            and v["score"] >= pipeline.verifier_soft_floor
                        ):
                            soft_candidates.append(
                                (v["score"], cand, start, end, clip_words)
                            )
                        continue
                elif cand.score < pipeline.min_clip_score:
                    continue

                accepted.append((cand, start, end, clip_words, verifier_score))
                if len(accepted) >= pipeline.max_clips_per_source:
                    break

            if not accepted and soft_candidates:
                soft_candidates.sort(key=lambda x: x[0], reverse=True)
                for score, cand, start, end, clip_words in soft_candidates:
                    if any(
                        _windows_overlap(start, end, a_start, a_end, min_gap=min_gap)
                        for _, a_start, a_end, _, _ in accepted
                    ):
                        continue
                    logger.warning(
                        "Soft-pass score=%.0f bounds=%.1f-%.1fs",
                        score,
                        start,
                        end,
                    )
                    accepted.append((cand, start, end, clip_words, score))
                    if len(accepted) >= pipeline.max_clips_per_source:
                        break

            if not accepted:
                logger.warning("All candidates rejected: %s", rejections)
                raise RuntimeError(f"All candidate clips rejected: {rejections}")

            if rejections:
                logger.info("Rejections / skips: %s", rejections)

            rendered_out: list[dict] = []
            first_ready: Path | None = None
            first_title = ""
            first_description = ""
            first_start = 0.0
            first_end = 0.0

            for clip_index, (cand, start, end, clip_words, verifier_score) in enumerate(
                accepted
            ):
                logger.info(
                    "Rendering clip %d/%.0f-%.0fs score=%.0f title=%s",
                    clip_index,
                    start,
                    end,
                    cand.score,
                    (cand.hook_title or "")[:80],
                )
                suffix = f"_{clip_index}" if len(accepted) > 1 else ""
                clip_path = (
                    self.settings.media_raw_dir
                    / f"{source.youtube_video_id}{suffix}_clip.mp4"
                )
                start, end = extract_clip(
                    raw_path,
                    clip_path,
                    max_seconds=pipeline.clip_max_seconds,
                    start_sec=start,
                    end_sec=end,
                )

                ass_path = None
                if pipeline.burn_captions:
                    ass_path = (
                        self.settings.media_raw_dir
                        / f"{source.youtube_video_id}{suffix}_subs.ass"
                    )
                    if clip_words:
                        words_to_ass(clip_words, clip_start=start, output_path=ass_path)
                    else:
                        segments_to_ass(
                            transcript.segments,
                            clip_start=start,
                            clip_end=end,
                            output_path=ass_path,
                        )

                vertical_path = (
                    self.settings.media_raw_dir
                    / f"{source.youtube_video_id}{suffix}_vert.mp4"
                )
                to_vertical_916(clip_path, vertical_path, ass_path=ass_path)

                title = generate_hook_title(cand.hook_title or source.title, None)
                description = generate_description(source.title, source.url, topic_name)
                ready_path = (
                    self.settings.media_ready_dir
                    / f"{source.youtube_video_id}{suffix}.mp4"
                )
                burn_hook_text(vertical_path, ready_path, title)

                render_id = self.db.insert_render(
                    source_id=source.id,
                    clip_index=clip_index,
                    start_sec=start,
                    end_sec=end,
                    ready_path=str(ready_path),
                    generated_title=title,
                    generated_description=description,
                    verifier_score=verifier_score,
                    status=RenderStatus.RENDERED.value,
                )
                rendered_out.append(
                    {
                        "render_id": render_id,
                        "clip_index": clip_index,
                        "ready_path": str(ready_path),
                        "title": title,
                        "start": start,
                        "end": end,
                        "verifier_score": verifier_score,
                    }
                )
                if first_ready is None:
                    first_ready = ready_path
                    first_title = title
                    first_description = description
                    first_start = start
                    first_end = end

                for temp in (clip_path, vertical_path, ass_path):
                    if temp is not None and temp.exists():
                        temp.unlink(missing_ok=True)

            self.db.update_source(
                source.id,
                status=JobStatus.RENDERED.value,
                ready_path=str(first_ready) if first_ready else None,
                clip_start_sec=first_start,
                clip_end_sec=first_end,
                generated_title=first_title,
                generated_description=first_description,
                error=None,
            )

            return {
                "status": "rendered",
                "source_id": source.id,
                "renders": rendered_out,
                "ready_path": str(first_ready) if first_ready else None,
                "title": first_title,
                "clip": {"start": first_start, "end": first_end},
                "highlight": highlight_meta,
                "rejections": rejections,
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

        render = self.db.next_render_by_status(RenderStatus.RENDERED)
        if render is not None:
            return self._publish_render(render, pipeline)

        # Legacy fallback: source-level ready_path with no renders rows
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

    def _publish_render(self, render, pipeline) -> dict:
        source = self.db.get_source(render.source_id)
        if source is None:
            self.db.update_render(
                render.id,
                status=RenderStatus.FAILED.value,
                error="parent source missing",
            )
            return {"status": "failed", "render_id": render.id, "error": "parent source missing"}

        if not self._topic_enabled(source.topic_id):
            self.db.update_source(source.id, status=JobStatus.SKIPPED.value, error="topic disabled")
            self.db.update_render(
                render.id,
                status=RenderStatus.FAILED.value,
                error="topic disabled",
            )
            return {"status": "skipped", "source_id": source.id, "reason": "topic disabled"}

        if not render.ready_path or not Path(render.ready_path).exists():
            self.db.update_render(
                render.id,
                status=RenderStatus.FAILED.value,
                error="ready_path missing",
            )
            return {
                "status": "failed",
                "source_id": source.id,
                "render_id": render.id,
                "error": "ready_path missing",
            }

        result = self.youtube.publish_video(
            video_path=Path(render.ready_path),
            title=render.generated_title or source.generated_title or f"{source.title} #Shorts",
            description=(
                render.generated_description
                or source.generated_description
                or f"{source.title}\n#Shorts"
            ),
        )
        if not result.success:
            self.db.update_render(
                render.id,
                status=RenderStatus.FAILED.value,
                error=(result.error or "upload failed")[:2000],
            )
            self.db.insert_post(
                source_id=source.id,
                platform="youtube",
                platform_post_id=None,
                status="failed",
                error=result.error,
                render_id=render.id,
            )
            return {
                "status": "failed",
                "source_id": source.id,
                "render_id": render.id,
                "error": result.error,
            }

        self.db.add_quota("youtube", pipeline.upload_quota_cost)
        self.db.insert_post(
            source_id=source.id,
            platform="youtube",
            platform_post_id=result.platform_post_id,
            status="published",
            render_id=render.id,
        )

        ready = Path(render.ready_path)
        archive = self.settings.media_archive_dir / ready.name
        archived = str(ready)
        try:
            shutil.move(str(ready), str(archive))
            archived = str(archive)
        except OSError:
            logger.warning("Could not archive %s", ready)

        self.db.update_render(
            render.id,
            status=RenderStatus.PUBLISHED.value,
            ready_path=archived,
            error=None,
        )

        if not self.db.source_has_unpublished_renders(source.id):
            self.db.update_source(
                source.id,
                status=JobStatus.PUBLISHED.value,
                ready_path=archived,
                error=None,
            )

        return {
            "status": "published",
            "source_id": source.id,
            "render_id": render.id,
            "clip_index": render.clip_index,
            "youtube_id": result.platform_post_id,
            "url": f"https://youtube.com/shorts/{result.platform_post_id}",
        }

    def status(self) -> dict:
        pipeline = self.settings.pipeline()
        return {
            "sources": self.db.status_counts(),
            "renders": self.db.render_status_counts(),
            "youtube_posts_today": self.db.count_posts_today("youtube"),
            "youtube_quota_today": self.db.get_quota_today("youtube"),
            "llm_highlights_today": self.db.get_quota_today("llm"),
            "max_llm_highlights_per_day": pipeline.max_llm_highlights_per_day,
            "max_clips_per_source": pipeline.max_clips_per_source,
            "llm_provider": pipeline.llm_provider,
            "quota_budget": pipeline.youtube_daily_quota_budget,
            "max_publishes_per_day": pipeline.max_publishes_per_day,
            "enabled_topics": [t.id for t in self.settings.enabled_topics()],
            "gemini_cost_hint": {
                "model": pipeline.gemini_model,
                "per_video_usd_paid_tier_approx": "0.002 - 0.01",
                "note": "Long sources use chunked Gemini calls; quota still 1 unit/source",
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
