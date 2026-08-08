from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.intelligence.narration import NarrationScript
from src.intelligence.owned_brief import ResearchBrief
from src.intelligence.words import Word

logger = logging.getLogger(__name__)

VisualType = Literal["title_card", "big_stat", "bullets", "mechanism", "closer"]

SCHEMA_VERSION = 1


@dataclass
class OwnedBeat:
    text: str
    visual_type: VisualType
    emphasis: str | None = None
    start_sec: float | None = None
    end_sec: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "visual_type": self.visual_type,
            "emphasis": self.emphasis,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
        }


@dataclass
class OwnedScenePlan:
    """Stable scene JSON for Python kinetic render; Remotion can consume later."""

    schema_version: int = SCHEMA_VERSION
    brand: str = "Shorts"
    topic_focus: str = ""
    beats: list[OwnedBeat] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "brand": self.brand,
            "topic_focus": self.topic_focus,
            "beats": [b.to_dict() for b in self.beats],
        }

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OwnedScenePlan:
        beats = [
            OwnedBeat(
                text=str(b.get("text") or "").strip(),
                visual_type=str(b.get("visual_type") or "bullets"),  # type: ignore[arg-type]
                emphasis=(str(b["emphasis"]) if b.get("emphasis") else None),
                start_sec=b.get("start_sec"),
                end_sec=b.get("end_sec"),
            )
            for b in (data.get("beats") or [])
            if str(b.get("text") or "").strip()
        ]
        return cls(
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            brand=str(data.get("brand") or "Shorts"),
            topic_focus=str(data.get("topic_focus") or ""),
            beats=beats,
        )


def _looks_like_stat(text: str) -> bool:
    return bool(re.search(r"\d", text))


def _body_visual(line: str, index: int, brief: ResearchBrief | None) -> VisualType:
    if _looks_like_stat(line):
        return "big_stat"
    if brief and brief.mechanism and index == 0 and len(line) > 40:
        return "mechanism"
    if index % 2 == 1:
        return "bullets"
    return "mechanism" if len(line) > 55 else "big_stat"


def build_scene_plan(
    script: NarrationScript,
    *,
    brand: str,
    brief: ResearchBrief | None = None,
) -> OwnedScenePlan:
    """Deterministic beat → visual_type mapping (Remotion-stable names)."""
    beats: list[OwnedBeat] = [
        OwnedBeat(
            text=script.hook_line.strip(),
            visual_type="title_card",
            emphasis=brief.topic_focus if brief else None,
        )
    ]
    for i, line in enumerate(script.body_lines):
        line = line.strip()
        if not line:
            continue
        beats.append(
            OwnedBeat(
                text=line,
                visual_type=_body_visual(line, i, brief),
                emphasis=None,
            )
        )
    if script.closer.strip():
        beats.append(
            OwnedBeat(text=script.closer.strip(), visual_type="closer")
        )
    if len(beats) < 2:
        raise ValueError("Scene plan needs at least hook + one more beat")
    focus = (brief.topic_focus if brief else "") or script.hook_line[:80]
    plan = OwnedScenePlan(brand=brand.strip() or "Shorts", topic_focus=focus, beats=beats)
    logger.info("Scene plan beats=%d focus=%s", len(plan.beats), focus[:50])
    return plan


def _normalize_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def assign_beat_times_from_words(
    plan: OwnedScenePlan,
    words: list[Word],
    *,
    total_duration: float,
) -> OwnedScenePlan:
    """Map beats onto VO word timestamps sequentially; fill gaps if needed."""
    if not plan.beats:
        return plan
    if not words or total_duration <= 0:
        n = len(plan.beats)
        slot = total_duration / n if n else 0.0
        for i, beat in enumerate(plan.beats):
            beat.start_sec = round(i * slot, 3)
            beat.end_sec = round((i + 1) * slot if i < n - 1 else total_duration, 3)
        return plan

    word_tokens = [(w, _normalize_tokens(w.text)) for w in words]
    flat: list[tuple[Word, str]] = []
    for w, toks in word_tokens:
        for t in toks:
            flat.append((w, t))

    cursor = 0
    for bi, beat in enumerate(plan.beats):
        beat_toks = _normalize_tokens(beat.text)
        if not beat_toks:
            continue
        # Greedy: find start at cursor, consume roughly len(beat_toks) words
        start_i = cursor
        # Advance start_i until first beat token appears nearby
        target0 = beat_toks[0]
        found = False
        for j in range(cursor, len(flat)):
            if flat[j][1] == target0:
                start_i = j
                found = True
                break
        if not found:
            start_i = cursor
        end_i = min(start_i + max(len(beat_toks), 1) - 1, len(flat) - 1)
        # Prefer ending near last beat token within a window
        last_tok = beat_toks[-1]
        window_end = min(start_i + len(beat_toks) + 8, len(flat))
        for j in range(start_i, window_end):
            if flat[j][1] == last_tok:
                end_i = j
        beat.start_sec = round(float(flat[start_i][0].start), 3)
        beat.end_sec = round(float(flat[end_i][0].end), 3)
        cursor = end_i + 1

    # Monotonic + cover full duration
    for i, beat in enumerate(plan.beats):
        if beat.start_sec is None:
            beat.start_sec = 0.0
        if beat.end_sec is None:
            beat.end_sec = beat.start_sec + 1.0
        if i > 0:
            prev = plan.beats[i - 1]
            assert prev.end_sec is not None and prev.start_sec is not None
            if beat.start_sec < prev.end_sec:
                beat.start_sec = prev.end_sec
            if beat.end_sec <= beat.start_sec:
                beat.end_sec = beat.start_sec + 0.4
    plan.beats[0].start_sec = 0.0
    plan.beats[-1].end_sec = round(total_duration, 3)
    # Stretch last gap if VO longer than last assigned end
    for i in range(len(plan.beats) - 1):
        a, b = plan.beats[i], plan.beats[i + 1]
        if a.end_sec is not None and b.start_sec is not None and a.end_sec > b.start_sec:
            a.end_sec = b.start_sec
    return plan


def assign_beat_times_equal(
    plan: OwnedScenePlan,
    *,
    total_duration: float,
) -> OwnedScenePlan:
    return assign_beat_times_from_words(plan, [], total_duration=total_duration)
