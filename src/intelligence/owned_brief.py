from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.intelligence.highlights import _gemini_generate_json
from src.intelligence.models import Transcript
from src.intelligence.narration import (
    NarrationScript,
    _parse_script,
    script_novelty_ratio,
)

logger = logging.getLogger(__name__)

BRIEF_PROMPT = """You extract a research brief for an ORIGINAL educational YouTube
Short ({topic_name}). The source transcript is RESEARCH ONLY — we will NOT use
any of its footage. Pull the idea and supported facts; ignore ads, intros,
CTAs, and tangents.

Return ONLY valid JSON (no markdown):
{{
  "topic_focus": "one short phrase naming the idea",
  "key_facts": ["fact 1", "fact 2", "fact 3"],
  "myth_or_misconception": "common wrong belief, or empty string",
  "mechanism": "how/why it works in plain language",
  "why_it_matters": "one sentence stake for the viewer",
  "numbers_or_stats": ["only numbers clearly supported by the source"]
}}

Rules:
- 3–5 key_facts max; concrete, not vague.
- Do not invent clinical claims, studies, or numbers absent from the source.
- For medicine topics: no diagnosis or treatment advice.
- Prefer physiology / psychology mechanisms over celebrity anecdotes.

Source title: {source_title}

Transcript (research only):
{transcript_text}
"""

OWNED_SCRIPT_PROMPT = """You write ORIGINAL host voiceover for educational YouTube
Shorts ({topic_name}). There is NO source footage — visuals are our own kinetic
cards. The research brief below is the only factual base.

Rules:
- Punchy Shorts pacing: short sentences, one clear idea.
- hook_line (first ~2s): cold open that stops the scroll (myth-bust, bold claim,
  surprising mechanism, or sharp question). Ban soft openers: "In this clip",
  "Let's talk about", "Did you know that", "Today we're going to".
- body: 2–4 short beats that TEACH from the brief. Add one concrete analogy or
  mental model. Do not dump the brief as a list.
- closer: one-line takeaway (no subscribe CTA).
- Total spoken words (hook + body + closer) <= {max_words}.
- Stay within the brief; for medicine, no diagnosis/treatment advice.
- Do not invent numbers. Prefer different phrasing than the brief's wording.

Return ONLY valid JSON (no markdown):
{{
  "hook_line": "...",
  "body": ["...", "..."],
  "closer": "..."
}}

Research brief:
{brief_text}
"""

RETRY_OWNED_NOVELTY = """
IMPORTANT: Too close to the research brief wording. Rewrite with a stronger
cold open and different sentence shapes. Do not reuse 4+ word phrases.
"""


@dataclass
class ResearchBrief:
    topic_focus: str
    key_facts: list[str] = field(default_factory=list)
    myth_or_misconception: str = ""
    mechanism: str = ""
    why_it_matters: str = ""
    numbers_or_stats: list[str] = field(default_factory=list)

    def as_prompt_text(self) -> str:
        facts = "\n".join(f"- {f}" for f in self.key_facts) or "- (none)"
        nums = "\n".join(f"- {n}" for n in self.numbers_or_stats) or "- (none)"
        return (
            f"Focus: {self.topic_focus}\n"
            f"Key facts:\n{facts}\n"
            f"Myth: {self.myth_or_misconception or '(none)'}\n"
            f"Mechanism: {self.mechanism or '(none)'}\n"
            f"Why it matters: {self.why_it_matters or '(none)'}\n"
            f"Numbers:\n{nums}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_focus": self.topic_focus,
            "key_facts": self.key_facts,
            "myth_or_misconception": self.myth_or_misconception,
            "mechanism": self.mechanism,
            "why_it_matters": self.why_it_matters,
            "numbers_or_stats": self.numbers_or_stats,
        }


def _parse_brief(parsed: dict[str, Any]) -> ResearchBrief:
    focus = str(parsed.get("topic_focus") or "").strip()
    if not focus:
        raise ValueError("Research brief missing topic_focus")
    facts_raw = parsed.get("key_facts") or []
    if isinstance(facts_raw, str):
        facts = [facts_raw.strip()] if facts_raw.strip() else []
    else:
        facts = [str(x).strip() for x in facts_raw if str(x).strip()]
    nums_raw = parsed.get("numbers_or_stats") or []
    if isinstance(nums_raw, str):
        nums = [nums_raw.strip()] if nums_raw.strip() else []
    else:
        nums = [str(x).strip() for x in nums_raw if str(x).strip()]
    return ResearchBrief(
        topic_focus=focus,
        key_facts=facts[:5],
        myth_or_misconception=str(parsed.get("myth_or_misconception") or "").strip(),
        mechanism=str(parsed.get("mechanism") or "").strip(),
        why_it_matters=str(parsed.get("why_it_matters") or "").strip(),
        numbers_or_stats=nums[:5],
    )


def extract_research_brief(
    transcript: Transcript,
    *,
    topic_name: str,
    source_title: str,
    api_key: str,
    model: str,
    max_chars: int = 14_000,
) -> tuple[ResearchBrief, dict[str, Any]]:
    """Gemini: transcript → research brief (no clip timestamps)."""
    text = (transcript.text or "").strip()
    if len(text) < 80:
        raise ValueError("Transcript too short for research brief")
    prompt = BRIEF_PROMPT.format(
        topic_name=topic_name,
        source_title=(source_title or "").strip() or "(untitled)",
        transcript_text=text[:max_chars],
    )
    parsed, meta = _gemini_generate_json(
        prompt, api_key=api_key, model=model, temperature=0.35
    )
    brief = _parse_brief(parsed)
    logger.info(
        "Research brief focus=%s facts=%d",
        brief.topic_focus[:60],
        len(brief.key_facts),
    )
    return brief, meta


def write_owned_narration_script(
    brief: ResearchBrief,
    *,
    topic_name: str,
    api_key: str,
    model: str,
    max_words: int = 110,
    min_novelty: float = 0.40,
) -> tuple[NarrationScript, dict[str, Any]]:
    """Gemini: research brief → host VO script (no B-roll assumption)."""
    brief_text = brief.as_prompt_text()
    base_prompt = OWNED_SCRIPT_PROMPT.format(
        topic_name=topic_name,
        max_words=max_words,
        brief_text=brief_text,
    )
    last_script: NarrationScript | None = None
    last_meta: dict[str, Any] = {}
    prompt = base_prompt

    for attempt in range(2):
        parsed, meta = _gemini_generate_json(
            prompt,
            api_key=api_key,
            model=model,
            temperature=0.55 if attempt == 0 else 0.75,
        )
        script = _parse_script(parsed)
        if script.word_count > max_words + 25:
            kept: list[str] = [script.hook_line]
            budget = max_words - len(re.findall(r"\b\w+\b", script.closer))
            used = len(re.findall(r"\b\w+\b", script.hook_line))
            for line in script.body_lines:
                n = len(re.findall(r"\b\w+\b", line))
                if used + n > budget:
                    break
                kept.append(line)
                used += n
            script = NarrationScript(
                hook_line=kept[0],
                body_lines=kept[1:],
                closer=script.closer,
            )

        novelty = script_novelty_ratio(script.full_text, brief_text)
        meta = {
            **meta,
            "novelty": novelty,
            "word_count": script.word_count,
            "attempt": attempt + 1,
        }
        last_script, last_meta = script, meta
        logger.info(
            "Owned narration attempt=%d words=%d novelty=%.2f",
            attempt + 1,
            script.word_count,
            novelty,
        )
        if novelty >= min_novelty and script.word_count >= 25:
            return script, meta
        prompt = base_prompt + RETRY_OWNED_NOVELTY

    assert last_script is not None
    novelty = float(last_meta.get("novelty") or 0.0)
    if novelty < min_novelty:
        raise RuntimeError(
            f"Owned narration too close to brief (novelty={novelty:.2f} < {min_novelty})"
        )
    if last_script.word_count < 25:
        raise RuntimeError("Owned narration script too short")
    return last_script, last_meta
