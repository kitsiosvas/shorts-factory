from __future__ import annotations

import logging
from pathlib import Path

from src.render.ffmpeg_utils import probe_duration, run_ffmpeg

logger = logging.getLogger(__name__)


def mux_vo_over_video(
    video_path: Path,
    vo_path: Path,
    output_path: Path,
    *,
    max_seconds: float = 59.0,
) -> float:
    """Replace clip audio with VO. Freeze last frame if VO is longer than video.

    Returns the final duration in seconds (capped at max_seconds).
    """
    video_dur = probe_duration(video_path)
    vo_dur = probe_duration(vo_path)
    target = min(vo_dur, max_seconds)
    if target < 1.0:
        raise RuntimeError(f"VO duration too short: {vo_dur:.2f}s")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Trim video to available footage for the target window, then pad by freezing
    # the last frame if the narration outlasts the clip.
    trim_dur = min(video_dur, target)
    pad = max(0.0, target - trim_dur)
    vf = f"trim=duration={trim_dur:.3f},setpts=PTS-STARTPTS"
    if pad > 0.05:
        vf += f",tpad=stop_mode=clone:stop_duration={pad:.3f}"
    af = f"atrim=duration={target:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.05,afade=t=out:st={max(target - 0.12, 0):.3f}:d=0.12"

    logger.info(
        "Mux VO over video: video=%.1fs vo=%.1fs -> target=%.1fs (pad=%.1fs)",
        video_dur,
        vo_dur,
        target,
        pad,
    )
    run_ffmpeg(
        [
            "-i",
            str(video_path),
            "-i",
            str(vo_path),
            "-filter_complex",
            f"[0:v]{vf}[v];[1:a]{af}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
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
    return target
