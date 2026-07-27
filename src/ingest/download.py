from __future__ import annotations

import logging
from pathlib import Path

import yt_dlp

from src.render.ffmpeg_utils import ffmpeg_dir, require_ffmpeg

logger = logging.getLogger(__name__)


def download_video(url: str, output_dir: Path, video_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / f"{video_id}.%(ext)s")
    # Ensure ffmpeg is resolvable for yt-dlp merges (server shells often miss PATH).
    require_ffmpeg()
    ydl_opts: dict = {
        "outtmpl": outtmpl,
        "format": (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "bv*+ba/"
            "b[ext=mp4]/"
            "b"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    bin_dir = ffmpeg_dir()
    if bin_dir:
        ydl_opts["ffmpeg_location"] = str(bin_dir)

    logger.info("Downloading %s -> %s", url, output_dir)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if path.suffix != ".mp4":
            mp4 = path.with_suffix(".mp4")
            if mp4.exists():
                path = mp4
        if not path.exists():
            candidates = list(output_dir.glob(f"{video_id}.*"))
            video_candidates = [
                p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm"}
            ]
            if video_candidates:
                path = video_candidates[0]
        if not path.exists():
            raise FileNotFoundError(f"Download finished but file missing: {path}")
        return path
