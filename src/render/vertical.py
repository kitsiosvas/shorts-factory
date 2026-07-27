from __future__ import annotations

import logging
from pathlib import Path

from src.render.ffmpeg_utils import run_ffmpeg

logger = logging.getLogger(__name__)


def to_vertical_916(input_path: Path, output_path: Path) -> Path:
    """Fit full frame into 1080x1920 with blurred background (no hard crop of content)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Verticalizing %s (blur-pad)", input_path.name)
    # Background: cover + blur. Foreground: contain (full video visible).
    fc = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=18,eq=brightness=-0.05[bg];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-filter_complex",
            fc,
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
    return output_path
