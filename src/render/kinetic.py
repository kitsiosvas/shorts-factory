from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.intelligence.scene_plan import OwnedBeat, OwnedScenePlan
from src.render.ffmpeg_utils import probe_duration, run_ffmpeg

logger = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
CRF = 19


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


def _wrap(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
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


def _bg() -> Image.Image:
    img = Image.new("RGB", (W, H), (14, 18, 24))
    draw = ImageDraw.Draw(img)
    for i in range(50):
        shade = 8 + i // 3
        draw.rectangle([0, i * 8, W, i * 8 + 8], fill=(shade, shade + 2, shade + 4))
        draw.rectangle(
            [0, H - (i + 1) * 8, W, H - i * 8],
            fill=(shade, shade + 2, shade + 4),
        )
    return img


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.ImageFont,
    *,
    y_center: int,
    fill: tuple[int, int, int] = (245, 247, 250),
) -> None:
    line_h = int(font.size * 1.2)
    total_h = len(lines) * line_h
    y = y_center - total_h // 2
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (W - tw) / 2
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h


def render_beat_card(beat: OwnedBeat, *, brand: str) -> Image.Image:
    """Pillow card for one OwnedBeat visual_type."""
    img = _bg()
    draw = ImageDraw.Draw(img)
    accent = {
        "title_card": (90, 200, 180),
        "big_stat": (220, 180, 100),
        "bullets": (120, 170, 220),
        "mechanism": (90, 200, 180),
        "closer": (220, 180, 100),
    }.get(beat.visual_type, (90, 200, 180))

    # Top brand whisper
    brand_font = _load_font(28)
    bw = draw.textlength(brand, font=brand_font)
    draw.text(((W - bw) / 2, int(H * 0.08)), brand, font=brand_font, fill=(120, 130, 140))

    draw.rectangle(
        [W // 2 - 40, int(H * 0.14), W // 2 + 40, int(H * 0.14) + 5],
        fill=accent,
    )

    max_w = int(W * 0.84)
    vtype = beat.visual_type

    if vtype == "title_card":
        font = _load_font(68)
        lines = _wrap(beat.text, font, max_w, draw)[:6]
        _draw_centered(draw, lines, font, y_center=int(H * 0.45))
        if beat.emphasis:
            ef = _load_font(32)
            elines = _wrap(beat.emphasis, ef, max_w, draw)[:2]
            _draw_centered(
                draw, elines, ef, y_center=int(H * 0.68), fill=(160, 170, 180)
            )
    elif vtype == "big_stat":
        font = _load_font(72)
        lines = _wrap(beat.text, font, max_w, draw)[:5]
        _draw_centered(draw, lines, font, y_center=int(H * 0.48), fill=(245, 235, 200))
    elif vtype == "bullets":
        label_font = _load_font(30)
        draw.text(
            (int(W * 0.1), int(H * 0.28)),
            "KEY POINT",
            font=label_font,
            fill=accent,
        )
        font = _load_font(56)
        lines = _wrap(beat.text, font, max_w, draw)[:7]
        _draw_centered(draw, lines, font, y_center=int(H * 0.5))
    elif vtype == "mechanism":
        label_font = _load_font(30)
        draw.text(
            (int(W * 0.1), int(H * 0.28)),
            "HOW IT WORKS",
            font=label_font,
            fill=accent,
        )
        font = _load_font(52)
        lines = _wrap(beat.text, font, max_w, draw)[:8]
        _draw_centered(draw, lines, font, y_center=int(H * 0.52))
    else:  # closer
        label_font = _load_font(30)
        lw = draw.textlength("TAKEAWAY", font=label_font)
        draw.text(
            ((W - lw) / 2, int(H * 0.32)),
            "TAKEAWAY",
            font=label_font,
            fill=accent,
        )
        font = _load_font(58)
        lines = _wrap(beat.text, font, max_w, draw)[:6]
        _draw_centered(draw, lines, font, y_center=int(H * 0.5))

    return img


def _still_to_clip(png: Path, duration: float, out_mp4: Path) -> Path:
    duration = max(duration, 0.35)
    fade = min(0.12, duration * 0.2)
    fade_out_st = max(duration - fade, 0.0)
    vf = (
        f"fps={FPS},format=yuv420p,setsar=1,"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fade=t=out:st={fade_out_st:.3f}:d={fade:.3f}"
    )
    run_ffmpeg(
        [
            "-loop",
            "1",
            "-i",
            str(png),
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
            str(CRF),
            "-an",
            str(out_mp4),
        ]
    )
    return out_mp4


def render_kinetic_video(
    plan: OwnedScenePlan,
    vo_path: Path,
    output_path: Path,
    *,
    max_seconds: float = 58.0,
) -> float:
    """Compose beat cards timed to plan.start/end, mux VO. Returns duration."""
    if not plan.beats:
        raise ValueError("OwnedScenePlan has no beats")
    vo_dur = probe_duration(vo_path)
    target = min(vo_dur, max_seconds)
    if target < 1.0:
        raise RuntimeError(f"VO too short: {vo_dur:.2f}s")

    # Ensure times cover target
    for beat in plan.beats:
        if beat.start_sec is None or beat.end_sec is None:
            raise ValueError("Beats must have start_sec/end_sec before render")
    plan.beats[-1].end_sec = target

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kinetic_") as tmp:
        tmp_path = Path(tmp)
        clip_paths: list[Path] = []
        for i, beat in enumerate(plan.beats):
            start = float(beat.start_sec or 0.0)
            end = float(beat.end_sec or start + 1.0)
            if i + 1 < len(plan.beats):
                next_start = float(plan.beats[i + 1].start_sec or end)
                end = min(end, next_start)
            dur = max(end - start, 0.35)
            if i == len(plan.beats) - 1:
                dur = max(target - start, 0.35)
            png = tmp_path / f"beat_{i:02d}.png"
            mp4 = tmp_path / f"beat_{i:02d}.mp4"
            render_beat_card(beat, brand=plan.brand).save(png)
            _still_to_clip(png, dur, mp4)
            clip_paths.append(mp4)

        # concat demuxer
        list_file = tmp_path / "list.txt"
        list_file.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in clip_paths),
            encoding="utf-8",
        )
        silent_video = tmp_path / "silent.mp4"
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                str(CRF),
                "-r",
                str(FPS),
                "-an",
                str(silent_video),
            ]
        )

        # Trim/pad video to VO length, mux audio
        vid_dur = probe_duration(silent_video)
        pad = max(0.0, target - vid_dur)
        vf = f"trim=duration={min(vid_dur, target):.3f},setpts=PTS-STARTPTS,fps={FPS},setsar=1"
        if pad > 0.05:
            vf += f",tpad=stop_mode=clone:stop_duration={pad:.3f}"
        af = (
            f"atrim=duration={target:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(target - 0.12, 0):.3f}:d=0.12"
        )
        logger.info(
            "Kinetic render beats=%d vo=%.1fs -> %s",
            len(plan.beats),
            target,
            output_path.name,
        )
        run_ffmpeg(
            [
                "-i",
                str(silent_video),
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
                str(CRF),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
    return target
