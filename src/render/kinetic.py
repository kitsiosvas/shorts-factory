from __future__ import annotations

import logging
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.intelligence.scene_plan import OwnedBeat, OwnedScenePlan
from src.intelligence.words import Word
from src.render.ffmpeg_utils import probe_duration, run_ffmpeg

logger = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
CRF = 19

_ACCENTS: dict[str, tuple[int, int, int]] = {
    "title_card": (90, 200, 180),
    "big_stat": (220, 180, 100),
    "bullets": (120, 170, 220),
    "mechanism": (90, 200, 180),
    "closer": (220, 180, 100),
}


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
    return lines or ([text] if text else [])


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _bg_base() -> Image.Image:
    img = Image.new("RGB", (W, H), (14, 18, 24))
    draw = ImageDraw.Draw(img)
    for i in range(50):
        shade = 8 + i // 3
        draw.rectangle([0, i * 8, W, i * 8 + 8], fill=(shade, shade + 2, shade + 4))
        draw.rectangle(
            [0, H - (i + 1) * 8, W, H - i * 8],
            fill=(shade, shade + 2, shade + 4),
        )
    # Soft accent glow blobs (static; Ken Burns moves them)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse([ -200, 400, 500, 1100], fill=(40, 80, 90, 55))
    od.ellipse([600, 900, 1300, 1700], fill=(70, 50, 30, 40))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    return img


def _ken_burns(bg: Image.Image, progress: float, *, zoom_end: float = 1.08) -> Image.Image:
    """Crop a slowly zooming window from a slightly larger canvas."""
    zoom = _lerp(1.0, zoom_end, progress)
    # Render bg onto oversized canvas then crop center
    scale = zoom
    sw, sh = int(W * scale) + 2, int(H * scale) + 2
    scaled = bg.resize((sw, sh), Image.Resampling.BILINEAR)
    # Drift slightly upward over time
    x = (sw - W) // 2
    y = max(0, int((sh - H) * (0.45 - 0.1 * progress)))
    return scaled.crop((x, y, x + W, y + H))


def _draw_centered_rgba(
    img: Image.Image,
    lines: list[str],
    font: ImageFont.ImageFont,
    *,
    y_center: float,
    fill: tuple[int, int, int] = (245, 247, 250),
    opacity: float = 1.0,
    y_offset: float = 0.0,
) -> None:
    if not lines or opacity <= 0.02:
        return
    alpha = max(0, min(255, int(255 * opacity)))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    line_h = int(font.size * 1.2)
    total_h = len(lines) * line_h
    y = y_center - total_h / 2 + y_offset
    for line in lines:
        tw = ld.textlength(line, font=font)
        x = (W - tw) / 2
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            ld.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, alpha))
        ld.text((x, y), line, font=font, fill=(*fill, alpha))
        y += line_h
    img.alpha_composite(layer)


def _visible_text(
    *,
    beat_text: str,
    beat_start: float,
    beat_end: float,
    t_abs: float,
    words: list[Word],
    chunk_sec: float = 0.4,
) -> str:
    """Reveal spoken tokens; fall back to timed word chunks."""
    local_words = [
        w for w in words if w.end > beat_start - 0.05 and w.start < beat_end + 0.05
    ]
    if local_words:
        revealed = [w.text for w in local_words if w.start <= t_abs + 0.05]
        return " ".join(revealed).strip()
    tokens = beat_text.split()
    if not tokens:
        return ""
    elapsed = max(0.0, t_abs - beat_start)
    # Show at least one token after 0.05s
    n = max(1, int(elapsed / chunk_sec) + 1) if elapsed > 0.05 else 0
    n = min(n, len(tokens))
    return " ".join(tokens[:n])


def _label_for(vtype: str) -> str | None:
    return {
        "bullets": "KEY POINT",
        "mechanism": "HOW IT WORKS",
        "closer": "TAKEAWAY",
    }.get(vtype)


def _font_size_for(vtype: str) -> int:
    return {
        "title_card": 64,
        "big_stat": 70,
        "bullets": 54,
        "mechanism": 50,
        "closer": 56,
    }.get(vtype, 56)


def render_beat_frame(
    beat: OwnedBeat,
    *,
    brand: str,
    t_local: float,
    duration: float,
    t_abs: float,
    words: list[Word] | None = None,
    bg_cache: Image.Image | None = None,
) -> Image.Image:
    """One animated frame for a beat at local time t_local (0..duration)."""
    duration = max(duration, 0.35)
    progress = max(0.0, min(1.0, t_local / duration))
    intro = _ease_out_cubic(min(1.0, t_local / 0.45))
    fade_out = 1.0
    if t_local > duration - 0.18:
        fade_out = max(0.0, (duration - t_local) / 0.18)

    vtype = beat.visual_type
    accent = _ACCENTS.get(vtype, (90, 200, 180))
    base = bg_cache or _bg_base()
    zoom_end = 1.10 if vtype == "closer" else 1.07
    frame = _ken_burns(base, progress, zoom_end=zoom_end).convert("RGBA")
    draw = ImageDraw.Draw(frame)

    # Brand whisper
    brand_font = _load_font(28)
    bw = draw.textlength(brand, font=brand_font)
    brand_alpha = int(200 * fade_out)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text(
        ((W - bw) / 2, int(H * 0.08)),
        brand,
        font=brand_font,
        fill=(120, 130, 140, brand_alpha),
    )
    frame.alpha_composite(layer)

    # Accent bar (grows in)
    bar_w = int(80 * intro)
    bar_y = int(H * 0.14)
    if bar_w > 0:
        bar_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar_layer)
        bd.rectangle(
            [W // 2 - bar_w, bar_y, W // 2 + bar_w, bar_y + 5],
            fill=(*accent, int(255 * fade_out)),
        )
        frame.alpha_composite(bar_layer)

    # Label
    label = _label_for(vtype)
    if label:
        label_font = _load_font(30)
        label_opacity = intro * fade_out
        if vtype == "mechanism":
            label_opacity = _ease_out_cubic(min(1.0, t_local / 0.35)) * fade_out
        # Soft vertical bob for closer "pulse"
        bob = 0.0
        if vtype == "closer":
            bob = -8.0 * math.sin(min(t_local, 0.8) / 0.8 * math.pi)
        ll = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        lld = ImageDraw.Draw(ll)
        lx = (
            (W - lld.textlength(label, font=label_font)) / 2
            if vtype == "closer"
            else W * 0.1
        )
        ly = H * (0.32 if vtype == "closer" else 0.28)
        ly -= (1.0 - intro) * 20
        ly += bob
        alpha = int(255 * label_opacity)
        lld.text((lx, ly), label, font=label_font, fill=(*accent, alpha))
        frame.alpha_composite(ll)

    # Body text — word-synced reveal
    words = words or []
    visible = _visible_text(
        beat_text=beat.text,
        beat_start=float(beat.start_sec or 0.0),
        beat_end=float(beat.end_sec or duration),
        t_abs=t_abs,
        words=words,
    )
    max_w = int(W * 0.84)
    font = _load_font(_font_size_for(vtype))
    # Measure wrap on temp draw
    probe = ImageDraw.Draw(Image.new("RGB", (W, H)))
    lines = _wrap(visible, font, max_w, probe)[:8] if visible else []

    y_center = H * {
        "title_card": 0.45,
        "big_stat": 0.48,
        "bullets": 0.52,
        "mechanism": 0.54,
        "closer": 0.52,
    }.get(vtype, 0.5)

    # Motion: slide-up + fade for title; progressive lines for bullets
    y_off = 0.0
    opacity = fade_out
    fill = (245, 247, 250)
    if vtype == "title_card":
        y_off = (1.0 - intro) * 70
        opacity = intro * fade_out
    elif vtype == "big_stat":
        # Punch scale via opacity pop then settle
        punch = 1.0
        if t_local < 0.35:
            punch = 0.55 + 0.45 * _ease_out_cubic(t_local / 0.35)
        opacity = punch * fade_out
        fill = (245, 235, 200)
        y_off = (1.0 - punch) * -30
    elif vtype == "bullets":
        # Reveal lines progressively even within visible text
        if lines:
            n_show = max(1, int(math.ceil(len(lines) * max(intro, progress))))
            # Prefer word-sync length; still stagger lines early
            if progress < 0.25 and len(lines) > 1:
                n_show = max(1, min(n_show, max(1, int(len(lines) * progress / 0.25))))
            lines = lines[:n_show]
        opacity = fade_out
        y_off = (1.0 - intro) * 40
    elif vtype == "mechanism":
        opacity = _ease_out_cubic(min(1.0, max(0.0, (t_local - 0.15) / 0.4))) * fade_out
        y_off = (1.0 - min(1.0, max(0.0, (t_local - 0.15) / 0.4))) * 35
    else:  # closer
        opacity = intro * fade_out
        y_off = (1.0 - intro) * 40

    _draw_centered_rgba(
        frame,
        lines,
        font,
        y_center=y_center,
        fill=fill,
        opacity=opacity,
        y_offset=y_off,
    )

    # Emphasis under title / big_stat
    if beat.emphasis and vtype in {"title_card", "big_stat"} and intro > 0.5:
        ef = _load_font(32)
        elines = _wrap(beat.emphasis, ef, max_w, probe)[:2]
        emp_op = ((intro - 0.5) / 0.5) * fade_out
        _draw_centered_rgba(
            frame,
            elines,
            ef,
            y_center=H * 0.68,
            fill=(160, 170, 180),
            opacity=emp_op,
            y_offset=(1.0 - emp_op) * 20,
        )

    return frame.convert("RGB")


def render_beat_card(beat: OwnedBeat, *, brand: str) -> Image.Image:
    """Static mid-beat preview card (debug / fallback)."""
    start = float(beat.start_sec or 0.0)
    end = float(beat.end_sec or start + 3.0)
    mid = (start + end) / 2
    dur = max(end - start, 0.35)
    return render_beat_frame(
        beat,
        brand=brand,
        t_local=dur * 0.55,
        duration=dur,
        t_abs=mid,
        words=None,
    )


def _frames_to_clip(frames_dir: Path, duration: float, out_mp4: Path, n_frames: int) -> Path:
    fade = min(0.12, duration * 0.15)
    fade_out_st = max(duration - fade, 0.0)
    vf = (
        f"fps={FPS},format=yuv420p,setsar=1,"
        f"fade=t=in:st=0:d={fade:.3f},"
        f"fade=t=out:st={fade_out_st:.3f}:d={fade:.3f}"
    )
    pattern = str(frames_dir / "f_%05d.jpg")
    run_ffmpeg(
        [
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            pattern,
            "-frames:v",
            str(n_frames),
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


def _render_beat_clip(
    beat: OwnedBeat,
    *,
    brand: str,
    duration: float,
    words: list[Word],
    out_mp4: Path,
    tmp: Path,
    beat_index: int,
    bg_cache: Image.Image,
) -> Path:
    duration = max(duration, 0.35)
    n_frames = max(int(round(duration * FPS)), 2)
    frames_dir = tmp / f"beat_{beat_index:02d}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    beat_start = float(beat.start_sec or 0.0)

    for fi in range(n_frames):
        t_local = fi / FPS
        t_abs = beat_start + t_local
        frame = render_beat_frame(
            beat,
            brand=brand,
            t_local=t_local,
            duration=duration,
            t_abs=t_abs,
            words=words,
            bg_cache=bg_cache,
        )
        frame.save(frames_dir / f"f_{fi:05d}.jpg", quality=88, optimize=True)

    return _frames_to_clip(frames_dir, duration, out_mp4, n_frames)


def render_kinetic_video(
    plan: OwnedScenePlan,
    vo_path: Path,
    output_path: Path,
    *,
    max_seconds: float = 58.0,
    words: list[Word] | None = None,
) -> float:
    """Compose animated beat cards timed to plan + VO words, mux audio."""
    if not plan.beats:
        raise ValueError("OwnedScenePlan has no beats")
    vo_dur = probe_duration(vo_path)
    target = min(vo_dur, max_seconds)
    if target < 1.0:
        raise RuntimeError(f"VO too short: {vo_dur:.2f}s")

    for beat in plan.beats:
        if beat.start_sec is None or beat.end_sec is None:
            raise ValueError("Beats must have start_sec/end_sec before render")
    plan.beats[-1].end_sec = target
    words = words or []

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bg_cache = _bg_base()

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
            # Mutate end for frame reveal bounds
            beat.end_sec = start + dur
            mp4 = tmp_path / f"beat_{i:02d}.mp4"
            _render_beat_clip(
                beat,
                brand=plan.brand,
                duration=dur,
                words=words,
                out_mp4=mp4,
                tmp=tmp_path,
                beat_index=i,
                bg_cache=bg_cache,
            )
            clip_paths.append(mp4)

        list_file = tmp_path / "list.txt"
        list_file.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in clip_paths),
            encoding="utf-8",
        )
        silent_video = tmp_path / "silent.mp4"
        run_ffmpeg(
            [
                "-y",
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
            "Kinetic render beats=%d vo=%.1fs words=%d -> %s",
            len(plan.beats),
            target,
            len(words),
            output_path.name,
        )
        run_ffmpeg(
            [
                "-y",
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
