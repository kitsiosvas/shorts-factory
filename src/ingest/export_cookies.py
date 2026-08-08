"""Export YouTube cookies from a browser into secrets/youtube_cookies.txt.

Chrome/Edge usually must be FULLY closed on Windows or the copy fails.
Firefox often works even while open.

Usage:
  python -m src.ingest.export_cookies
  python -m src.ingest.export_cookies --browser firefox
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yt_dlp

from src.config import ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Export yt-dlp YouTube cookies")
    parser.add_argument(
        "--browser",
        default="chrome",
        help="Browser to read cookies from (chrome, edge, firefox). Default: chrome",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "secrets" / "youtube_cookies.txt"),
        help="Output Netscape cookies path",
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Exporting cookies from {args.browser} -> {out}")
    print("If this fails with 'Could not copy cookie database', quit the browser fully and retry.")
    # yt-dlp writes cookies when both cookiesfrombrowser and cookiefile are set
    # and a URL is processed (skip download).
    opts = {
        "cookiesfrombrowser": (args.browser.lower(),),
        "cookiefile": str(out),
        "skip_download": True,
        "quiet": False,
        "no_warnings": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        print(
            "Tips: close Chrome completely (tray too), or try --browser firefox, "
            "or use the 'Get cookies.txt LOCALLY' Chrome extension and save to "
            "secrets/youtube_cookies.txt",
            file=sys.stderr,
        )
        return 1

    if not out.is_file() or out.stat().st_size < 50:
        print("FAILED: cookie file was not written", file=sys.stderr)
        return 1
    print(f"OK wrote {out} ({out.stat().st_size} bytes)")
    print("Set in .env: YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt")
    print("Then restart the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
