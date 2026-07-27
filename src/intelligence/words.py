from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SENTENCE_ENDERS = (".", "!", "?", "…")
_GAP_SPLIT_SEC = 1.0
_PAD_START_SEC = 0.15
_PAD_END_SEC = 0.35


@dataclass
class Word:
    start: float  # absolute seconds in the source video
    end: float
    text: str


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@lru_cache(maxsize=1)
def _get_model(model_name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        ) from exc
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def transcribe_window_words(
    video_path: Path,
    window_start: float,
    window_end: float,
    *,
    model_name: str = "base",
    pad_sec: float = 20.0,
) -> list[Word]:
    """Word-level timestamps around a candidate window via faster-whisper."""
    from src.render.ffmpeg_utils import require_ffmpeg

    offset = max(0.0, window_start - pad_sec)
    length = (window_end + pad_sec) - offset
    if length <= 0:
        return []

    ffmpeg = require_ffmpeg()
    model = _get_model(model_name)

    with tempfile.TemporaryDirectory(prefix="fw_words_") as tmp:
        wav_path = Path(tmp) / "window.wav"
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg wav extract failed: {result.stderr[-1500:]}")

        segments, _info = model.transcribe(
            str(wav_path),
            word_timestamps=True,
            language="en",
            vad_filter=True,
        )

        words: list[Word] = []
        for segment in segments:
            for w in segment.words or []:
                text = (w.word or "").strip()
                if not text:
                    continue
                words.append(
                    Word(
                        start=float(w.start) + offset,
                        end=float(w.end) + offset,
                        text=text,
                    )
                )
        return words


def build_sentences(words: list[Word]) -> list[Sentence]:
    """Split words into sentences on punctuation or long gaps."""
    if not words:
        return []

    sentences: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = " ".join(w.text for w in current).strip()
        sentences.append(
            Sentence(
                start=current[0].start,
                end=current[-1].end,
                text=text,
                words=list(current),
            )
        )
        current = []

    for i, word in enumerate(words):
        current.append(word)
        ends_sentence = word.text.rstrip().endswith(_SENTENCE_ENDERS)
        gap_split = False
        if i + 1 < len(words):
            gap_split = (words[i + 1].start - word.end) > _GAP_SPLIT_SEC
        if ends_sentence or gap_split:
            flush()

    flush()
    return sentences


def snap_clip_to_sentences(
    words: list[Word],
    start: float,
    end: float,
    *,
    min_sec: float,
    max_sec: float,
) -> tuple[float, float, list[Word]]:
    """Snap clip bounds to sentence boundaries and return in-window words."""
    sentences = build_sentences(words)
    if not sentences:
        return start, end, []

    # Snapped start = start of sentence containing `start` (or next if in a gap)
    snap_start = start
    start_idx: int | None = None
    for i, sent in enumerate(sentences):
        if sent.start <= start <= sent.end:
            snap_start = sent.start
            start_idx = i
            break
        if start < sent.start:
            snap_start = sent.start
            start_idx = i
            break
    if start_idx is None:
        # start is after the last sentence
        return start, end, []

    # Snapped end = end of sentence containing `end` (or previous if in a gap)
    snap_end = end
    end_idx: int | None = None
    for i in range(len(sentences) - 1, -1, -1):
        sent = sentences[i]
        if sent.start <= end <= sent.end:
            snap_end = sent.end
            end_idx = i
            break
        if end > sent.end:
            snap_end = sent.end
            end_idx = i
            break
    if end_idx is None or end_idx < start_idx:
        # end is before the first useful sentence
        end_idx = start_idx
        snap_end = sentences[start_idx].end

    # Enforce max duration: prefer dropping lead-in (preserve payoff/end).
    while start_idx < end_idx and (snap_end - snap_start) > max_sec:
        start_idx += 1
        snap_start = sentences[start_idx].start
    # Last resort: trim the end if still over budget.
    while end_idx > start_idx and (snap_end - snap_start) > max_sec:
        end_idx -= 1
        snap_end = sentences[end_idx].end
    if (snap_end - snap_start) > max_sec:
        snap_end = snap_start + max_sec

    # Enforce min duration: append following sentences while <= max_sec
    while end_idx + 1 < len(sentences) and (snap_end - snap_start) < min_sec:
        next_end = sentences[end_idx + 1].end
        if (next_end - snap_start) > max_sec:
            break
        end_idx += 1
        snap_end = next_end

    # Padding: start -= 0.15 but not before previous word's end;
    # end += 0.35 but not past next word's start.
    all_words = words
    prev_end = 0.0
    next_start: float | None = None
    for i, w in enumerate(all_words):
        if w.end <= snap_start + 1e-6:
            prev_end = w.end
        if w.start >= snap_end - 1e-6:
            next_start = w.start
            break

    padded_start = max(0.0, snap_start - _PAD_START_SEC)
    if padded_start < prev_end:
        padded_start = prev_end
    padded_start = max(0.0, padded_start)

    padded_end = snap_end + _PAD_END_SEC
    if next_start is not None and padded_end > next_start:
        padded_end = next_start

    if padded_end <= padded_start:
        padded_start, padded_end = snap_start, snap_end

    in_window = [w for w in all_words if w.end > padded_start and w.start < padded_end]
    return padded_start, padded_end, in_window
