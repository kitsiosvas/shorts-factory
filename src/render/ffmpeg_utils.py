from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


class FFmpegNotFoundError(RuntimeError):
    pass


def _winget_ffmpeg_bins() -> list[Path]:
    root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if not root.exists():
        return []
    bins: list[Path] = []
    for package in root.glob("Gyan.FFmpeg*"):
        bins.extend(package.glob("ffmpeg-*/bin"))
    return bins


@lru_cache
def ffmpeg_dir() -> Path | None:
    """Directory containing ffmpeg.exe / ffprobe.exe."""
    explicit = os.environ.get("FFMPEG_PATH") or os.environ.get("FFMPEG_DIR")
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path.parent
        if (path / "ffmpeg.exe").exists() or (path / "ffmpeg").exists():
            return path

    which = shutil.which("ffmpeg")
    if which:
        return Path(which).resolve().parent

    for bin_dir in _winget_ffmpeg_bins():
        if (bin_dir / "ffmpeg.exe").exists():
            return bin_dir
    return None


def require_ffmpeg() -> str:
    directory = ffmpeg_dir()
    if directory:
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    raise FFmpegNotFoundError(
        "ffmpeg not found. Install with `winget install Gyan.FFmpeg`, then restart "
        "the server (or set FFMPEG_PATH to the bin folder)."
    )


def require_ffprobe() -> str:
    directory = ffmpeg_dir()
    if directory:
        for name in ("ffprobe.exe", "ffprobe"):
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    raise FFmpegNotFoundError(
        "ffprobe not found. Install ffmpeg (includes ffprobe) and restart the server."
    )


def run_ffmpeg(args: list[str]) -> None:
    ffmpeg = require_ffmpeg()
    cmd = [ffmpeg, "-y", *args]
    logger.debug("ffmpeg %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


def probe_duration(path: Path) -> float:
    ffprobe = require_ffprobe()
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def detect_silences(
    path: Path,
    *,
    noise_db: float = -30.0,
    min_silence: float = 0.35,
) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) seconds."""
    ffmpeg = require_ffmpeg()
    result = subprocess.run(
        [
            ffmpeg,
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    stderr = result.stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", stderr)]
    pairs: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else start + min_silence
        pairs.append((start, end))
    return pairs


def find_phrase_window(
    path: Path,
    *,
    max_seconds: float = 45.0,
    min_seconds: float = 18.0,
    target_seconds: float = 35.0,
) -> tuple[float, float]:
    """
    Pick a clip that starts/ends on silence (sentence-ish pauses), not mid-speech.
    Falls back to a mid-video window if silence data is weak.
    """
    duration = probe_duration(path)
    max_seconds = min(max_seconds, max(duration - 0.25, 1.0))
    min_seconds = min(min_seconds, max_seconds)

    if duration <= max_seconds + 0.5:
        return 0.0, duration

    silences = detect_silences(path)
    start_cuts = [0.0] + [end for _, end in silences]
    end_cuts = [start for start, _ in silences] + [duration]

    # Speech segments for scoring
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s0, s1 in silences:
        if s0 > cursor + 0.05:
            speech.append((cursor, s0))
        cursor = s1
    if cursor < duration - 0.05:
        speech.append((cursor, duration))

    def speech_ratio(a: float, b: float) -> float:
        if b <= a:
            return 0.0
        covered = 0.0
        for s0, s1 in speech:
            covered += max(0.0, min(b, s1) - max(a, s0))
        return covered / (b - a)

    best: tuple[float, float] | None = None
    best_score = -1.0

    for start in start_cuts:
        for end in end_cuts:
            length = end - start
            if length < min_seconds or length > max_seconds:
                continue
            # Prefer ending in a pause and high speech density, near target length
            ratio = speech_ratio(start, end)
            if ratio < 0.45:
                continue
            length_score = 1.0 - min(abs(length - target_seconds) / target_seconds, 1.0)
            # Prefer not starting at 0 unless necessary (intros/branding)
            intro_penalty = 0.15 if start < 5.0 else 0.0
            score = ratio * 2.0 + length_score - intro_penalty
            if score > best_score:
                best_score = score
                best = (start, end)

    if best is not None:
        return best

    # Fallback: loudest-ish mid window, then snap ends to nearest silence cuts
    window = max_seconds
    rough_start = max((duration - window) / 2, 0.0)
    rough_end = rough_start + window

    def nearest(candidates: list[float], value: float, *, prefer: str) -> float:
        if not candidates:
            return value
        if prefer == "before":
            before = [c for c in candidates if c <= value]
            return max(before) if before else min(candidates, key=lambda c: abs(c - value))
        after = [c for c in candidates if c >= value]
        return min(after) if after else min(candidates, key=lambda c: abs(c - value))

    start = nearest(start_cuts, rough_start, prefer="after")
    end = nearest(end_cuts, rough_end, prefer="before")
    if end - start < min_seconds:
        start = rough_start
        end = min(rough_start + window, duration)
    if end - start > max_seconds:
        end = start + max_seconds
    return max(0.0, start), min(duration, end)


def find_loudest_window(path: Path, window_sec: float) -> tuple[float, float]:
    """Backward-compatible wrapper → phrase-aware window."""
    return find_phrase_window(path, max_seconds=window_sec)
