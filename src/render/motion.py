from __future__ import annotations

import logging
from pathlib import Path

from src.render.ffmpeg_utils import probe_duration, run_ffmpeg

logger = logging.getLogger(__name__)


def apply_subtle_zoom(
    input_path: Path,
    output_path: Path,
    *,
    zoom_end: float = 1.08,
) -> Path:
    """Gentle Ken Burns zoom on an already-vertical (or any) clip. Keeps audio."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(probe_duration(input_path), 0.5)
    fps = 30.0
    frames = max(int(duration * fps), 2)
    # zoompan z goes from 1 -> zoom_end over the clip
    z_expr = f"min(1+({zoom_end:.4f}-1)*on/{frames}, {zoom_end:.4f})"
    vf = (
        f"scale=8000:-1,"
        f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s=1080x1920:fps={fps:.0f},"
        f"setsar=1"
    )
    logger.info(
        "Subtle zoom %s -> %s (end=%.2f, ~%ds)",
        input_path.name,
        output_path.name,
        zoom_end,
        int(duration),
    )
    run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-vf",
            vf,
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
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return output_path
