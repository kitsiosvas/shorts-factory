from __future__ import annotations

import logging
from pathlib import Path

from src.intelligence.models import TranscriptSegment
from src.intelligence.words import Word

logger = logging.getLogger(__name__)

_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,96,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,0,2,60,60,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_ASS_HEADER_PLAIN = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,0,2,60,60,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_SENTENCE_ENDERS = (".", "!", "?", "…")


def _fmt_ass_time(seconds: float) -> str:
    """ASS time format H:MM:SS.CC."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    if s >= 60:
        m += 1
        s = 0
    if m >= 60:
        h += 1
        m = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _group_words(words: list[Word], max_words_per_line: int) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        current.append(word)
        ends_sentence = word.text.rstrip().endswith(_SENTENCE_ENDERS)
        if len(current) >= max_words_per_line or ends_sentence:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def words_to_ass(
    words: list[Word],
    *,
    clip_start: float,
    output_path: Path,
    max_words_per_line: int = 3,
) -> Path:
    """Write karaoke ASS captions relative to clip_start."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_ASS_HEADER.rstrip()]

    for group in _group_words(words, max_words_per_line):
        if not group:
            continue
        rel_start = max(0.0, group[0].start - clip_start)
        # End at last word end, relative to clip
        rel_end = max(rel_start + 0.05, group[-1].end - clip_start)

        karaoke_parts: list[str] = []
        for i, word in enumerate(group):
            if i + 1 < len(group):
                # k-duration includes gap to next word in the line
                dur = max(0.01, group[i + 1].start - word.start)
            else:
                dur = max(0.01, word.end - word.start)
            centis = max(1, int(round(dur * 100)))
            karaoke_parts.append(f"{{\\k{centis}}}{word.text}")

        text = " ".join(karaoke_parts)
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(rel_start)},{_fmt_ass_time(rel_end)},"
            f"Caption,,0,0,0,,{text}"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote karaoke ASS %s (%d words)", output_path.name, len(words))
    return output_path


def segments_to_ass(
    segments: list[TranscriptSegment],
    *,
    clip_start: float,
    clip_end: float,
    output_path: Path,
) -> Path:
    """Fallback cue-level ASS (no karaoke) from transcript segments."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_ASS_HEADER_PLAIN.rstrip()]

    for seg in segments:
        if seg.end < clip_start or seg.start > clip_end:
            continue
        text = (seg.text or "").strip()
        if not text:
            continue
        rel_start = max(0.0, seg.start - clip_start)
        rel_end = max(rel_start + 0.05, min(seg.end, clip_end) - clip_start)
        # Escape ASS special chars lightly
        safe = text.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(rel_start)},{_fmt_ass_time(rel_end)},"
            f"Caption,,0,0,0,,{safe}"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote cue ASS %s", output_path.name)
    return output_path


def ass_filter_arg(path: Path) -> str:
    """Windows-safe ass= filter path escaping."""
    escaped = str(path).replace("\\", "/").replace(":", "\\:")
    return f"ass='{escaped}'"
