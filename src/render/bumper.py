from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.render.ffmpeg_utils import run_ffmpeg

logger = logging.getLogger(__name__)

W, H = 1080, 1920


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
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


def _draw_card(brand: str, *, tagline: str | None, kind: str) -> Image.Image:
    """Dark slate card — brand-forward, no purple/glow clutter."""
    img = Image.new("RGB", (W, H), (14, 18, 24))
    draw = ImageDraw.Draw(img)
    # Soft top/bottom wash so the card isn't a flat slab
    for i in range(60):
        shade = 8 + i // 4
        draw.rectangle([0, i * 6, W, i * 6 + 6], fill=(shade, shade + 2, shade + 4))
        draw.rectangle(
            [0, H - (i + 1) * 6, W, H - i * 6],
            fill=(shade, shade + 2, shade + 4),
        )

    accent = (90, 200, 180) if kind == "intro" else (220, 180, 100)
    cy = H // 2
    draw.rectangle([W // 2 - 48, cy - 120, W // 2 + 48, cy - 114], fill=accent)

    brand_font = _load_font(72)
    tag_font = _load_font(36)
    max_w = int(W * 0.82)
    brand_lines = _wrap(brand.strip() or "Shorts", brand_font, max_w, draw)
    total_h = len(brand_lines) * int(brand_font.size * 1.2)
    y = cy - total_h // 2 - 20
    for line in brand_lines:
        tw = draw.textlength(line, font=brand_font)
        x = (W - tw) / 2
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x + dx, y + dy), line, font=brand_font, fill=(0, 0, 0))
        draw.text((x, y), line, font=brand_font, fill=(245, 247, 250))
        y += int(brand_font.size * 1.2)

    label = (tagline or ("facts in under a minute" if kind == "intro" else "thanks for watching")).strip()
    if label:
        y += 28
        for line in _wrap(label, tag_font, max_w, draw)[:2]:
            tw = draw.textlength(line, font=tag_font)
            draw.text(((W - tw) / 2, y), line, font=tag_font, fill=(160, 170, 180))
            y += int(tag_font.size * 1.25)

    return img


def make_brand_bumper(
    brand: str,
    output_path: Path,
    *,
    duration: float = 0.8,
    kind: str = "intro",
    tagline: str | None = None,
) -> Path:
    """Silent 1080x1920 brand card with matching stereo silence for concat."""
    if duration < 0.25:
        raise ValueError(f"bumper duration too short: {duration}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    card = _draw_card(brand, tagline=tagline, kind=kind)

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "card.png"
        card.save(png)
        # Fade in/out so the cut into content isn't a hard flash.
        fade_in = min(0.12, duration * 0.25)
        fade_out = min(0.18, duration * 0.35)
        fade_out_st = max(duration - fade_out, 0.0)
        vf = (
            f"fade=t=in:st=0:d={fade_in:.3f},"
            f"fade=t=out:st={fade_out_st:.3f}:d={fade_out:.3f},"
            f"setsar=1"
        )
        logger.info(
            "Brand bumper %s kind=%s dur=%.2fs -> %s",
            brand[:40],
            kind,
            duration,
            output_path.name,
        )
        run_ffmpeg(
            [
                "-loop",
                "1",
                "-i",
                str(png),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                f"{duration:.3f}",
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                "20",
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


def wrap_with_bumpers(
    main_path: Path,
    output_path: Path,
    *,
    brand: str,
    intro_sec: float = 0.75,
    outro_sec: float = 0.9,
    intro_tagline: str | None = None,
    outro_tagline: str | None = None,
) -> Path:
    """Prepend/append brand cards around an already-vertical Short."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        intro = tmp_path / "intro.mp4"
        outro = tmp_path / "outro.mp4"
        make_brand_bumper(
            brand,
            intro,
            duration=intro_sec,
            kind="intro",
            tagline=intro_tagline,
        )
        make_brand_bumper(
            brand,
            outro,
            duration=outro_sec,
            kind="outro",
            tagline=outro_tagline,
        )
        # Normalize fps / timebase so concat is clean across still+live segments.
        fc = (
            "[0:v]fps=30,format=yuv420p,setsar=1[v0];"
            "[1:v]fps=30,format=yuv420p,setsar=1[v1];"
            "[2:v]fps=30,format=yuv420p,setsar=1[v2];"
            "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[a1];"
            "[2:a]aformat=sample_rates=44100:channel_layouts=stereo[a2];"
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]"
        )
        logger.info(
            "Wrap bumpers around %s (intro=%.2fs outro=%.2fs)",
            main_path.name,
            intro_sec,
            outro_sec,
        )
        run_ffmpeg(
            [
                "-i",
                str(intro),
                "-i",
                str(main_path),
                "-i",
                str(outro),
                "-filter_complex",
                fc,
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
    return output_path
