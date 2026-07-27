from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.render.ffmpeg_utils import require_ffmpeg, run_ffmpeg

logger = logging.getLogger(__name__)


def generate_hook_title(source_title: str, transcript: str | None = None) -> str:
    if transcript:
        first = re.split(r"[.!?\n]", transcript.strip())[0].strip()
        if 12 <= len(first) <= 80:
            base = first
        else:
            base = source_title
    else:
        base = source_title
    base = re.sub(r"\s+", " ", base).strip()
    if len(base) > 70:
        base = base[:67].rstrip() + "..."
    if "#Shorts" not in base:
        base = f"{base} #Shorts"
    return base


def generate_description(source_title: str, source_url: str, topic_name: str) -> str:
    return (
        f"{source_title}\n\n"
        f"Topic: {topic_name}\n"
        f"#Shorts\n"
    )


def transcribe_whisper(video_path: Path, model_name: str = "tiny") -> str | None:
    try:
        import whisper
    except ImportError:
        logger.warning("openai-whisper not installed; skipping transcription")
        return None
    try:
        logger.info("Transcribing with whisper model=%s", model_name)
        model = whisper.load_model(model_name)
        result = model.transcribe(str(video_path), fp16=False)
        text = (result.get("text") or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Whisper failed: %s", exc)
        return None


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def burn_hook_text(input_path: Path, output_path: Path, hook: str) -> Path:
    """Burn a top hook caption via Pillow overlay + ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    short = hook.replace("#Shorts", "").strip()
    if len(short) > 48:
        short = short[:45] + "..."

    # Probe size via a one-frame PNG
    ffmpeg = require_ffmpeg()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        frame_path = tmp_path / "frame.png"
        overlay_path = tmp_path / "overlay.png"
        subprocess.run(
            [ffmpeg, "-y", "-i", str(input_path), "-frames:v", "1", str(frame_path)],
            check=True,
            capture_output=True,
        )
        with Image.open(frame_path) as frame:
            width, height = frame.size
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = _load_font(size=max(36, width // 24))
        lines = _wrap_text(short, font, max_width=int(width * 0.9), draw=draw)
        y = int(height * 0.08)
        for line in lines:
            tw = draw.textlength(line, font=font)
            x = (width - tw) / 2
            # Outline
            for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2)]:
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += int(font.size * 1.25)
        overlay.save(overlay_path)

        run_ffmpeg(
            [
                "-i",
                str(input_path),
                "-i",
                str(overlay_path),
                "-filter_complex",
                "[0:v][1:v]overlay=0:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
    return output_path
