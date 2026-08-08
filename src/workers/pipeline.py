from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from src.config import Settings, TopicConfig, get_settings
from src.db import Database, JobStatus, RenderStatus
from src.discover import discover_for_topic
from src.ingest import download_video
from src.intelligence import get_transcript, pick_highlights
from src.intelligence.highlights import verify_clip_gemini
from src.intelligence.models import HighlightClip, TranscriptSegment
from src.intelligence.narration import write_narration_script
from src.intelligence.owned_brief import (
    extract_research_brief,
    write_owned_narration_script,
)
from src.intelligence.scene_plan import (
    assign_beat_times_from_words,
    build_scene_plan,
)
from src.intelligence.visual_mode import classify_visual_mode
from src.intelligence.words import (
    Word,
    snap_clip_to_sentences,
    transcribe_audio_words,
    transcribe_window_words,
)
from src.publish.youtube import YouTubePublisher
from src.render import (
    burn_hook_text,
    extract_clip,
    generate_description,
    generate_hook_title,
    to_vertical_916,
)
from src.render.audio_mix import mux_vo_over_video
from src.render.bumper import wrap_with_bumpers
from src.render.ffmpeg_utils import probe_duration, run_ffmpeg
from src.render.kinetic import render_kinetic_video
from src.render.motion import apply_subtle_zoom
from src.render.subtitles import ass_filter_arg, segments_to_ass, words_to_ass
from src.render.tts import synthesize_speech

OWNED_CLIP_INDEX = 900

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

                clip_text = (
                    " ".join(w.text for w in clip_words)
                    if clip_words
                    else transcript.text_between(start, end)
                )
                narration_script = None
                vo_path = None
                vo_words: list[Word] = []
                mixed_path = None
                motion_path = None
                bumper_path = None
                render_input = clip_path
                visual_mode = "broll"
                use_vo = False

                needs_classify = (
                    pipeline.reject_talking_heads
                    or (
                        pipeline.enable_narration
                        and (pipeline.narration_routing or "auto").lower().strip()
                        == "auto"
                    )
                )
                if needs_classify or pipeline.enable_narration:
                    if pipeline.llm_provider != "gemini" or not self.settings.gemini_api_key:
                        raise RuntimeError(
                            "visual routing / narration requires llm_provider=gemini "
                            "and GEMINI_API_KEY"
                        )

                routing = (pipeline.narration_routing or "auto").lower().strip()
                if routing == "always_vo":
                    visual_mode = "broll"
                elif routing == "always_source":
                    visual_mode = "talking_head"
                elif needs_classify:
                    visual_mode, vm_meta = classify_visual_mode(
                        clip_path,
                        start_sec=0.0,
                        end_sec=max(end - start, 1.0),
                        clip_text=clip_text,
                        api_key=self.settings.gemini_api_key or "",
                        model=pipeline.gemini_model,
                    )
                    logger.info(
                        "Clip %d visual_mode=%s (%s)",
                        clip_index,
                        visual_mode,
                        vm_meta.get("reason") or vm_meta.get("error") or "",
                    )

                if pipeline.reject_talking_heads and visual_mode == "talking_head":
                    reason = (
                        f"{start:.0f}-{end:.0f}s: rejected talking_head "
                        "(want animation/diagram/B-roll only)"
                    )
                    rejections.append(reason)
                    logger.info("Skipping clip %d: %s", clip_index, reason)
                    if clip_path.exists():
                        clip_path.unlink(missing_ok=True)
                    continue

                use_vo = pipeline.enable_narration and visual_mode == "broll"

                if use_vo:
                    try:
                        narration_script, narr_meta = write_narration_script(
                            clip_text,
                            topic_name=topic_name,
                            hook_title=cand.hook_title,
                            api_key=self.settings.gemini_api_key or "",
                            model=pipeline.gemini_model,
                            max_words=pipeline.narration_max_words,
                            min_novelty=pipeline.min_script_novelty,
                        )
                        logger.info(
                            "Narration novelty=%.2f words=%d: %s",
                            float(narr_meta.get("novelty") or 0),
                            narration_script.word_count,
                            narration_script.full_text[:120],
                        )
                        vo_path = (
                            self.settings.media_raw_dir
                            / f"{source.youtube_video_id}{suffix}_vo.mp3"
                        )
                        synthesize_speech(
                            narration_script.full_text,
                            vo_path,
                            voice=pipeline.tts_voice,
                        )
                        mixed_path = (
                            self.settings.media_raw_dir
                            / f"{source.youtube_video_id}{suffix}_vo_clip.mp4"
                        )
                        bumper_budget = 0.0
                        if pipeline.enable_brand_bumper:
                            bumper_budget = float(pipeline.bumper_intro_sec) + float(
                                pipeline.bumper_outro_sec
                            )
                        mux_vo_over_video(
                            clip_path,
                            vo_path,
                            mixed_path,
                            max_seconds=max(59.0 - bumper_budget, 50.0),
                        )
                        render_input = mixed_path
                        try:
                            vo_words = transcribe_audio_words(
                                vo_path,
                                model_name=pipeline.snap_whisper_model,
                            )
                        except Exception:
                            logger.exception(
                                "VO word timestamps failed; captions will use plain lines"
                            )
                    except Exception as narr_exc:  # noqa: BLE001
                        logger.warning(
                            "Narration failed for clip %d (%.0f-%.0fs): %s",
                            clip_index,
                            start,
                            end,
                            narr_exc,
                        )
                        for temp in (vo_path, mixed_path, clip_path):
                            if temp is not None and temp.exists():
                                temp.unlink(missing_ok=True)
                        continue

                ass_path = None
                if pipeline.burn_captions:
                    ass_path = (
                        self.settings.media_raw_dir
                        / f"{source.youtube_video_id}{suffix}_subs.ass"
                    )
                    if use_vo and narration_script is not None:
                        if vo_words:
                            words_to_ass(vo_words, clip_start=0.0, output_path=ass_path)
                        else:
                            vo_dur = probe_duration(vo_path) if vo_path else 30.0
                            chunks = [
                                s.strip()
                                for s in re.split(
                                    r"(?<=[.!?])\s+",
                                    narration_script.full_text,
                                )
                                if s.strip()
                            ] or [narration_script.full_text]
                            per = vo_dur / max(len(chunks), 1)
                            segs = [
                                TranscriptSegment(
                                    start=i * per,
                                    end=min(vo_dur, (i + 1) * per),
                                    text=chunk,
                                )
                                for i, chunk in enumerate(chunks)
                            ]
                            segments_to_ass(
                                segs,
                                clip_start=0.0,
                                clip_end=vo_dur,
                                output_path=ass_path,
                            )
                    elif clip_words:
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
                to_vertical_916(render_input, vertical_path, ass_path=ass_path)

                hook_input = vertical_path
                if (
                    pipeline.enable_narration
                    and not use_vo
                    and pipeline.talking_head_motion
                ):
                    motion_path = (
                        self.settings.media_raw_dir
                        / f"{source.youtube_video_id}{suffix}_motion.mp4"
                    )
                    try:
                        apply_subtle_zoom(
                            vertical_path,
                            motion_path,
                            zoom_end=float(pipeline.talking_head_zoom_end),
                        )
                        hook_input = motion_path
                    except Exception:
                        logger.exception(
                            "Talking-head motion failed; using static vertical"
                        )
                        motion_path = None

                if narration_script is not None:
                    title = generate_hook_title(narration_script.hook_line, None)
                else:
                    title = generate_hook_title(cand.hook_title or source.title, None)
                description = generate_description(
                    source.title,
                    source.url,
                    topic_name,
                    narrated=narration_script is not None,
                )
                ready_path = (
                    self.settings.media_ready_dir
                    / f"{source.youtube_video_id}{suffix}.mp4"
                )
                burn_hook_text(hook_input, ready_path, title)

                bumper_path = None
                if use_vo and pipeline.enable_brand_bumper:
                    brand = (pipeline.brand_name or topic_name or "Shorts").strip()
                    bumper_path = (
                        self.settings.media_raw_dir
                        / f"{source.youtube_video_id}{suffix}_bumper.mp4"
                    )
                    try:
                        wrap_with_bumpers(
                            ready_path,
                            bumper_path,
                            brand=brand,
                            intro_sec=float(pipeline.bumper_intro_sec),
                            outro_sec=float(pipeline.bumper_outro_sec),
                            intro_tagline=pipeline.bumper_intro_tagline,
                            outro_tagline=pipeline.bumper_outro_tagline,
                        )
                        ready_path.unlink(missing_ok=True)
                        shutil.move(str(bumper_path), str(ready_path))
                        bumper_path = None
                        logger.info(
                            "Brand bumper applied for clip %d (%s)",
                            clip_index,
                            brand,
                        )
                    except Exception:
                        logger.exception(
                            "Brand bumper failed for clip %d; keeping hooked cut",
                            clip_index,
                        )
                        if bumper_path is not None and bumper_path.exists():
                            bumper_path.unlink(missing_ok=True)
                        bumper_path = None

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
                        "narrated": narration_script is not None,
                        "visual_mode": visual_mode,
                    }
                )
                if first_ready is None:
                    first_ready = ready_path
                    first_title = title
                    first_description = description
                    first_start = start
                    first_end = end

                for temp in (
                    clip_path,
                    vertical_path,
                    ass_path,
                    vo_path,
                    mixed_path,
                    motion_path,
                    bumper_path,
                ):
                    if temp is not None and temp.exists():
                        temp.unlink(missing_ok=True)

            if not rendered_out:
                raise RuntimeError(
                    "No clips rendered"
                    + (
                        f" (talking heads / narration failures): {rejections}"
                        if rejections
                        else ""
                    )
                )

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

    def process_owned_next(
        self,
        *,
        source_id: int | None = None,
        topic_id: str | None = None,
    ) -> dict:
        """Parallel owned path: research brief → VO → kinetic templates (no B-roll)."""
        pipeline = self.settings.pipeline()
        if pipeline.llm_provider != "gemini" or not self.settings.gemini_api_key:
            return {
                "status": "failed",
                "error": "owned path requires llm_provider=gemini and GEMINI_API_KEY",
            }

        if source_id is not None:
            source = self.db.get_source(source_id)
            if source is None:
                return {"status": "failed", "error": f"source_id={source_id} not found"}
        else:
            source = self.db.next_source_for_owned(topic_id=topic_id)
            if source is None:
                # Fall back: discover queue item we can download for research
                source = self.db.next_by_status(JobStatus.DISCOVERED)
                if source is not None and topic_id and source.topic_id != topic_id:
                    source = None
            if source is None:
                return {
                    "status": "idle",
                    "message": (
                        "No source ready for owned render "
                        "(need downloaded/rendered without owned, or discovered)"
                    ),
                }

        if not self._topic_enabled(source.topic_id):
            return {
                "status": "skipped",
                "source_id": source.id,
                "reason": "topic disabled",
            }
        if self.db.source_has_owned_render(source.id):
            return {
                "status": "skipped",
                "source_id": source.id,
                "reason": "owned render already exists",
            }

        llm_used_today = self.db.get_quota_today("llm")
        if llm_used_today >= pipeline.max_llm_highlights_per_day:
            return {
                "status": "budget",
                "source_id": source.id,
                "error": (
                    f"LLM daily cap reached ({llm_used_today}/"
                    f"{pipeline.max_llm_highlights_per_day})"
                ),
            }

        topic = self._topic_by_id(source.topic_id)
        topic_name = topic.display_name if topic else source.topic_id
        brand = (pipeline.brand_name or topic_name or "Shorts").strip()

        vo_path: Path | None = None
        kinetic_path: Path | None = None
        ass_path: Path | None = None
        captioned_path: Path | None = None
        bumper_tmp: Path | None = None

        try:
            raw_path: Path | None = None
            if source.raw_path and Path(source.raw_path).exists():
                raw_path = Path(source.raw_path)
            else:
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

            transcript = get_transcript(
                video_id=source.youtube_video_id,
                video_path=raw_path,
                captions_dir=self.settings.captions_dir,
                use_whisper_fallback=pipeline.use_whisper,
                whisper_model=pipeline.whisper_model,
            )
            if not transcript.segments:
                raise RuntimeError(
                    "No transcript available for research brief "
                    "(YouTube captions empty; enable use_whisper or skip)."
                )

            brief, brief_meta = extract_research_brief(
                transcript,
                topic_name=topic_name,
                source_title=source.title,
                api_key=self.settings.gemini_api_key or "",
                model=pipeline.gemini_model,
            )
            script, script_meta = write_owned_narration_script(
                brief,
                topic_name=topic_name,
                api_key=self.settings.gemini_api_key or "",
                model=pipeline.gemini_model,
                max_words=pipeline.owned_max_words,
                min_novelty=pipeline.owned_min_script_novelty,
            )
            self.db.add_quota("llm", 1)

            plan = build_scene_plan(script, brand=brand, brief=brief)
            scenes_json = (
                self.settings.media_raw_dir
                / f"owned_{source.youtube_video_id}_scenes.json"
            )
            brief_json = (
                self.settings.media_raw_dir
                / f"owned_{source.youtube_video_id}_brief.json"
            )
            brief_json.write_text(
                json.dumps(brief.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            vo_path = (
                self.settings.media_raw_dir / f"owned_{source.youtube_video_id}_vo.mp3"
            )
            synthesize_speech(
                script.full_text,
                vo_path,
                voice=pipeline.tts_voice,
            )
            vo_dur = probe_duration(vo_path)

            vo_words: list[Word] = []
            try:
                vo_words = transcribe_audio_words(
                    vo_path, model_name=pipeline.snap_whisper_model
                )
            except Exception:
                logger.exception("Owned VO word timestamps failed; equal beat split")

            bumper_budget = 0.0
            if pipeline.owned_enable_bumper:
                bumper_budget = float(pipeline.bumper_intro_sec) + float(
                    pipeline.bumper_outro_sec
                )
            max_content = max(58.0 - bumper_budget, 50.0)
            assign_beat_times_from_words(plan, vo_words, total_duration=min(vo_dur, max_content))
            plan.write_json(scenes_json)

            kinetic_path = (
                self.settings.media_raw_dir / f"owned_{source.youtube_video_id}_kinetic.mp4"
            )
            content_dur = render_kinetic_video(
                plan, vo_path, kinetic_path, max_seconds=max_content
            )

            hook_input = kinetic_path
            if pipeline.burn_captions:
                ass_path = (
                    self.settings.media_raw_dir
                    / f"owned_{source.youtube_video_id}_subs.ass"
                )
                if vo_words:
                    words_to_ass(vo_words, clip_start=0.0, output_path=ass_path)
                else:
                    chunks = [
                        s.strip()
                        for s in re.split(r"(?<=[.!?])\s+", script.full_text)
                        if s.strip()
                    ] or [script.full_text]
                    per = content_dur / max(len(chunks), 1)
                    segs = [
                        TranscriptSegment(
                            start=i * per,
                            end=min(content_dur, (i + 1) * per),
                            text=chunk,
                        )
                        for i, chunk in enumerate(chunks)
                    ]
                    segments_to_ass(
                        segs,
                        clip_start=0.0,
                        clip_end=content_dur,
                        output_path=ass_path,
                    )
                captioned_path = (
                    self.settings.media_raw_dir
                    / f"owned_{source.youtube_video_id}_cap.mp4"
                )
                # Burn ASS onto kinetic video (already 9:16)
                run_ffmpeg(
                    [
                        "-i",
                        str(kinetic_path),
                        "-vf",
                        f"{ass_filter_arg(ass_path)},setsar=1",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "19",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-movflags",
                        "+faststart",
                        str(captioned_path),
                    ]
                )
                hook_input = captioned_path

            title = generate_hook_title(script.hook_line, None)
            description = generate_description(
                source.title,
                source.url,
                topic_name,
                owned=True,
            )
            ready_path = (
                self.settings.media_ready_dir / f"owned_{source.youtube_video_id}.mp4"
            )
            burn_hook_text(hook_input, ready_path, title)

            if pipeline.owned_enable_bumper:
                bumper_tmp = (
                    self.settings.media_raw_dir
                    / f"owned_{source.youtube_video_id}_bumper.mp4"
                )
                try:
                    wrap_with_bumpers(
                        ready_path,
                        bumper_tmp,
                        brand=brand,
                        intro_sec=float(pipeline.bumper_intro_sec),
                        outro_sec=float(pipeline.bumper_outro_sec),
                        intro_tagline=pipeline.bumper_intro_tagline,
                        outro_tagline=pipeline.bumper_outro_tagline,
                    )
                    ready_path.unlink(missing_ok=True)
                    shutil.move(str(bumper_tmp), str(ready_path))
                    bumper_tmp = None
                except Exception:
                    logger.exception("Owned brand bumper failed; keeping hooked cut")
                    if bumper_tmp is not None and bumper_tmp.exists():
                        bumper_tmp.unlink(missing_ok=True)
                    bumper_tmp = None

            render_id = self.db.insert_render(
                source_id=source.id,
                clip_index=OWNED_CLIP_INDEX,
                start_sec=0.0,
                end_sec=content_dur,
                ready_path=str(ready_path),
                generated_title=title,
                generated_description=description,
                verifier_score=None,
                status=RenderStatus.RENDERED.value,
                kind="owned",
            )
            # Do not force source status away from clip-factory RENDERED;
            # only upgrade discovered/downloaded so publish/status stay sensible.
            if source.status in {
                JobStatus.DISCOVERED.value,
                JobStatus.DOWNLOADED.value,
                JobStatus.FAILED.value,
            }:
                self.db.update_source(
                    source.id,
                    status=JobStatus.RENDERED.value,
                    error=None,
                )

            logger.info(
                "Owned Short ready source_id=%s render_id=%s path=%s novelty=%.2f",
                source.id,
                render_id,
                ready_path,
                float(script_meta.get("novelty") or 0),
            )
            return {
                "status": "rendered",
                "kind": "owned",
                "source_id": source.id,
                "render_id": render_id,
                "ready_path": str(ready_path),
                "title": title,
                "topic_focus": brief.topic_focus,
                "scenes_json": str(scenes_json),
                "brief_json": str(brief_json),
                "novelty": script_meta.get("novelty"),
                "brief_meta": brief_meta,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Owned process failed source_id=%s", source.id)
            return {
                "status": "failed",
                "kind": "owned",
                "source_id": source.id,
                "error": str(exc),
            }
        finally:
            for temp in (vo_path, kinetic_path, ass_path, captioned_path, bumper_tmp):
                if temp is not None and temp.exists():
                    # Keep scenes/brief JSON; drop heavy intermediates
                    if temp.suffix.lower() in {".mp4", ".mp3", ".ass"}:
                        temp.unlink(missing_ok=True)

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
