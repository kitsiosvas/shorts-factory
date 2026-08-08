from __future__ import annotations

import logging
import time
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from src.ingest.yt_cookies import apply_youtube_auth, apply_ytdlp_runtime
from src.render.ffmpeg_utils import ffmpeg_dir, require_ffmpeg

logger = logging.getLogger(__name__)

_DOWNLOAD_ATTEMPTS = 3
_BOT_HINT = (
    "YouTube bot-check / cookies failed. Prefer a cookie FILE (works with Chrome open):\n"
    "  1) Fully quit Chrome, then: python -m src.ingest.export_cookies\n"
    "  2) Or export via 'Get cookies.txt LOCALLY' → secrets/youtube_cookies.txt\n"
    "  3) .env: YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt  then restart server"
)


def download_video(url: str, output_dir: Path, video_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = output_dir / f"{video_id}.mp4"
    if existing.is_file() and existing.stat().st_size > 100_000:
        logger.info("Reusing existing download %s (%d bytes)", existing.name, existing.stat().st_size)
        return existing

    outtmpl = str(output_dir / f"{video_id}.%(ext)s")
    # Ensure ffmpeg is resolvable for yt-dlp merges (server shells often miss PATH).
    require_ffmpeg()
    ydl_opts: dict = {
        "outtmpl": outtmpl,
        # Prefer mp4 when available; fall back broadly (cookies alone may still
        # only expose storyboards until the JS challenge solver runs).
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # YouTube n-challenge / EJS (needs Deno or Node on PATH).
        "remote_components": ["ejs:github"],
        # Truncated CDN responses ("bytes read, N more expected") are common;
        # retry at http + fragment level before giving up.
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "retry_sleep_functions": {
            "http": lambda n: min(2**n, 20),
            "fragment": lambda n: min(2**n, 20),
        },
        "continuedl": True,
    }
    apply_youtube_auth(ydl_opts)
    apply_ytdlp_runtime(ydl_opts)
    bin_dir = ffmpeg_dir()
    if bin_dir:
        ydl_opts["ffmpeg_location"] = str(bin_dir)

    last_error: Exception | None = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        logger.info(
            "Downloading %s -> %s (attempt %d/%d)",
            url,
            output_dir,
            attempt,
            _DOWNLOAD_ATTEMPTS,
        )
        try:
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
                        p
                        for p in candidates
                        if p.suffix.lower() in {".mp4", ".mkv", ".webm"}
                    ]
                    if video_candidates:
                        path = video_candidates[0]
                if not path.exists():
                    raise FileNotFoundError(f"Download finished but file missing: {path}")
                return path
        except DownloadError as exc:
            last_error = exc
            logger.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt,
                _DOWNLOAD_ATTEMPTS,
                video_id,
                exc,
            )
            if attempt < _DOWNLOAD_ATTEMPTS:
                time.sleep(min(2**attempt, 15))

    assert last_error is not None
    msg = str(last_error)
    from src.ingest.yt_cookies import cookie_copy_hint

    hint = cookie_copy_hint(msg)
    if hint or "not a bot" in msg.lower() or "cookies" in msg.lower():
        raise DownloadError(f"{msg}\n{hint or _BOT_HINT}") from last_error
    raise last_error
