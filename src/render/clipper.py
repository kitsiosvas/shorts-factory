from __future__ import annotations

import logging
from pathlib import Path

from src.render.ffmpeg_utils import run_ffmpeg

logger = logging.getLogger(__name__)


def extract_clip(
    input_path: Path,
    output_path: Path,
    *,
    max_seconds: float = 45.0,
    fade_seconds: float = 0.20,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> tuple[float, float]:
    """Cut an LLM-chosen window. start_sec/end_sec are required."""
    if start_sec is None or end_sec is None or end_sec <= start_sec:
        raise ValueError(
            "extract_clip requires LLM-provided start_sec/end_sec "
            "(silence-based auto-cut is disabled)."
        )
    start, end = float(start_sec), float(end_sec)
    if end - start > max_seconds:
        end = start + max_seconds
    duration = max(end - start, 1.0)
    fade = min(fade_seconds, duration / 4)
    fade_out_start = max(duration - fade, 0.0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Clipping %s -> %.1f-%.1fs (fade=%.2fs)",
        input_path.name,
        start,
        end,
        fade,
    )
    vf = f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"
    af = (
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"
    )
    run_ffmpeg(
        [
            "-ss",
            f"{start:.3f}",
            "-i",
            str(input_path),
            "-t",
            f"{duration:.3f}",
            "-vf",
            vf,
            "-af",
            af,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return start, end
