from __future__ import annotations

import logging
import re
from pathlib import Path

import yt_dlp

from src.intelligence.models import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)


def _parse_vtt_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def _parse_vtt(content: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n\n+", content.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # Skip WEBVTT header / NOTE
        if lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        time_line = None
        text_lines: list[str] = []
        for ln in lines:
            if "-->" in ln:
                time_line = ln
            elif time_line is not None and not ln.isdigit():
                # strip VTT tags
                cleaned = re.sub(r"<[^>]+>", "", ln).strip()
                if cleaned:
                    text_lines.append(cleaned)
        if not time_line or not text_lines:
            continue
        start_s, end_s = [p.strip() for p in time_line.split("-->")]
        start_s = start_s.split(" ")[0]
        end_s = end_s.split(" ")[0]
        text = " ".join(text_lines)
        # Dedup consecutive identical auto-caption spam
        if segments and segments[-1].text == text:
            segments[-1].end = _parse_vtt_timestamp(end_s)
            continue
        segments.append(
            TranscriptSegment(
                start=_parse_vtt_timestamp(start_s),
                end=_parse_vtt_timestamp(end_s),
                text=text,
            )
        )
    return segments


def fetch_youtube_captions(video_id: str, work_dir: Path) -> Transcript | None:
    """Download auto/manual English captions via yt-dlp (free, no Whisper)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(work_dir / f"{video_id}.%(ext)s")
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB"],
        "subtitlesformat": "vtt",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        logger.warning("YouTube captions download failed for %s: %s", video_id, exc)
        return None

    candidates = sorted(work_dir.glob(f"{video_id}*.vtt"))
    if not candidates:
        logger.info("No VTT captions found for %s", video_id)
        return None

    # Prefer manual en over auto if both exist (filename heuristics)
    preferred = sorted(
        candidates,
        key=lambda p: (
            0 if ".en." in p.name or p.name.endswith(".en.vtt") else 1,
            len(p.name),
        ),
    )
    content = preferred[0].read_text(encoding="utf-8", errors="ignore")
    segments = _parse_vtt(content)
    if not segments:
        return None
    logger.info(
        "Loaded %s caption segments from %s (%s)",
        len(segments),
        preferred[0].name,
        video_id,
    )
    return Transcript(segments=segments, source="youtube_captions")


def transcribe_whisper_segments(
    video_path: Path,
    *,
    model_name: str = "tiny",
) -> Transcript | None:
    try:
        import whisper
    except ImportError:
        logger.warning("openai-whisper not installed; cannot fallback-transcribe")
        return None
    try:
        logger.info("Whisper transcribe model=%s path=%s", model_name, video_path.name)
        model = whisper.load_model(model_name)
        result = model.transcribe(str(video_path), fp16=False, verbose=False)
        segments: list[TranscriptSegment] = []
        for item in result.get("segments") or []:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    start=float(item.get("start") or 0.0),
                    end=float(item.get("end") or 0.0),
                    text=text,
                )
            )
        if not segments:
            return None
        return Transcript(segments=segments, source="whisper")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Whisper failed: %s", exc)
        return None


def get_transcript(
    *,
    video_id: str,
    video_path: Path,
    captions_dir: Path,
    use_whisper_fallback: bool = False,
    whisper_model: str = "tiny",
) -> Transcript:
    transcript = fetch_youtube_captions(video_id, captions_dir)
    if transcript and transcript.segments:
        return transcript
    if use_whisper_fallback:
        whisper_tx = transcribe_whisper_segments(video_path, model_name=whisper_model)
        if whisper_tx and whisper_tx.segments:
            return whisper_tx
    logger.warning("No transcript available for %s; highlight LLM will be skipped", video_id)
    return Transcript(segments=[], source="empty")
