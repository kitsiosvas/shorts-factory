from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COPY_FAIL_HINT = (
    "Chrome/Edge lock their cookie DB while open on Windows. "
    "Fix: (1) fully quit Chrome/Edge, then run "
    "`python -m src.ingest.export_cookies`, OR (2) export with the "
    "'Get cookies.txt LOCALLY' extension to secrets/youtube_cookies.txt "
    "and set YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt"
)


def _find_deno() -> str | None:
    which = shutil.which("deno")
    if which:
        return which
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WinGet"
        / "Links"
        / "deno.exe",
        Path(os.environ.get("USERPROFILE", "")) / ".deno" / "bin" / "deno.exe",
    ]
    winget_root = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )
    if winget_root.is_dir():
        candidates.extend(winget_root.glob("DenoLand.Deno*/deno.exe"))
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def apply_ytdlp_runtime(ydl_opts: dict[str, Any]) -> dict[str, Any]:
    """Enable YouTube JS challenge solving (EJS) via Deno when available."""
    deno = _find_deno()
    if deno:
        ydl_opts["js_runtimes"] = {"deno": {"path": deno}}
        ydl_opts.setdefault("remote_components", ["ejs:github"])
        logger.info("yt-dlp JS runtime deno=%s", deno)
    else:
        logger.warning(
            "Deno not found on PATH — YouTube downloads may only see storyboard "
            "formats. Install Deno (winget install DenoLand.Deno) and restart."
        )
    return ydl_opts


def apply_youtube_auth(ydl_opts: dict[str, Any]) -> dict[str, Any]:
    """Attach cookies so yt-dlp can pass YouTube bot checks.

    Prefer a Netscape cookies file (works with Chrome open). Browser extraction
    often fails on Windows while Chromium browsers are running.

    Set in .env:
      YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt
      YTDLP_COOKIES_FROM_BROWSER=firefox   # optional fallback; chrome often locked
    """
    from src.config import get_settings

    settings = get_settings()
    cookie_file = settings.ytdlp_cookies_file
    if cookie_file is not None:
        path = Path(cookie_file)
        if not path.is_absolute():
            from src.config import ROOT

            path = ROOT / path
        if path.is_file() and path.stat().st_size > 50:
            ydl_opts["cookiefile"] = str(path)
            logger.info("yt-dlp using cookie file %s", path)
            return ydl_opts
        logger.warning("YTDLP_COOKIES_FILE set but missing/empty: %s", path)

    raw = (settings.ytdlp_cookies_from_browser or "").strip().lower()
    if not raw:
        return ydl_opts

    browsers = [b.strip() for b in raw.split(",") if b.strip()]
    ydl_opts["cookiesfrombrowser"] = (browsers[0],)
    ydl_opts["_cookies_browser_candidates"] = browsers
    logger.info("yt-dlp using cookies from browser=%s", browsers[0])
    return ydl_opts


def cookie_copy_hint(exc: BaseException | str) -> str | None:
    msg = str(exc).lower()
    if "could not copy" in msg or "cookie database" in msg or "permission denied" in msg:
        return _COPY_FAIL_HINT
    return None
