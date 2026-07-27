from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: list[TranscriptSegment] = field(default_factory=list)
    source: str = "unknown"  # youtube_captions | whisper | empty

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip()).strip()

    def as_numbered_lines(self, *, max_chars: int = 24_000) -> str:
        """Compact timestamped transcript for LLM prompts."""
        lines: list[str] = []
        size = 0
        for seg in self.segments:
            line = f"[{seg.start:.1f}-{seg.end:.1f}] {seg.text.strip()}"
            if size + len(line) + 1 > max_chars:
                lines.append("...[transcript truncated]...")
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)

    def text_between(self, start: float, end: float) -> str:
        parts = [
            s.text.strip()
            for s in self.segments
            if s.end >= start and s.start <= end and s.text.strip()
        ]
        return " ".join(parts)


@dataclass
class HighlightClip:
    start_sec: float
    end_sec: float
    hook_title: str
    reason: str = ""
    score: float = 0.0

    @property
    def duration(self) -> float:
        return max(self.end_sec - self.start_sec, 0.0)
