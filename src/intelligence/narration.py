from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from src.intelligence.highlights import _gemini_generate_json

logger = logging.getLogger(__name__)

NARRATION_PROMPT = """You write ORIGINAL host voiceover for educational YouTube
Shorts ({topic_name}). Viewers hear YOUR voice only — the source is silent
B-roll under your script. You are not a summarizer of the transcript.

Goal: a Short that feels authored by a host, not a paraphrase of someone else's
lines. Steal the IDEA and the FACTS that the source supports; rebuild the
structure, wording, and framing from scratch.

Rules:
- Punchy Shorts pacing: short sentences, one clear idea.
- hook_line (first ~2s): a cold open that STOPS the scroll. Prefer myth-bust,
  bold claim, surprising mechanism, or sharp question. It must NOT restate the
  source's first sentence or the picker hook. Ban soft openers: "In this clip",
  "Let's talk about", "Did you know that", "Today we're going to".
- body: 2–4 short beats that TEACH. Reorder freely. Add one concrete analogy,
  mental model, or "why it matters" beat the raw transcript does not spell out.
  Do not walk the transcript beat-by-beat.
- closer: one-line takeaway the viewer can repeat (no subscribe CTA).
- Total spoken words (hook + body + closer) <= {max_words}.
- Stay within what the source supports; for medicine, no diagnosis/treatment
  advice. Do not invent numbers or study claims.
- Never copy 4+ word phrases from the source. Prefer different verbs and
  sentence shapes even when terms like "dopamine" must stay.

Return ONLY valid JSON (no markdown):
{{
  "hook_line": "...",
  "body": ["...", "..."],
  "closer": "..."
}}

Optional picker hook (use as inspiration only — do not echo it): {hook_title}

Source clip transcript (extract the idea; do NOT quote or paraphrase closely):
{clip_text}
"""

RETRY_NOVELTY_EXTRA = """
IMPORTANT: Your previous draft was too close to the source. Rewrite as a NEW
script: different hook angle, different sentence order, different phrasing.
Do not reuse 4+ word phrases from the source. Lead with a stronger cold open.
"""


@dataclass
class NarrationScript:
    hook_line: str
    body_lines: list[str]
    closer: str

    @property
    def full_text(self) -> str:
        parts = [self.hook_line.strip(), *[b.strip() for b in self.body_lines if b.strip()]]
        if self.closer.strip():
            parts.append(self.closer.strip())
        return " ".join(p for p in parts if p)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b\w+\b", self.full_text))


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) > 3}


def _ngrams(text: str, n: int = 4) -> set[str]:
    toks = re.findall(r"[a-z0-9']+", text.lower())
    if len(toks) < n:
        return set()
    return {" ".join(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def script_novelty_ratio(script_text: str, source_text: str) -> float:
    """1.0 = no shared 4-grams with source; 0.0 = every 4-gram appears in source.

    Unigram overlap is too harsh for educational niches (shared terms like
    'sleep', 'dopamine'). Phrase copying is the real failure mode.
    """
    script_grams = _ngrams(script_text, 4)
    if not script_grams:
        script_toks = _tokenize(script_text)
        source_toks = _tokenize(source_text)
        if not script_toks:
            return 0.0
        return 1.0 - (len(script_toks & source_toks) / len(script_toks))
    source_grams = _ngrams(source_text, 4)
    shared = script_grams & source_grams
    return 1.0 - (len(shared) / len(script_grams))


def _parse_script(parsed: dict[str, Any]) -> NarrationScript:
    hook = str(parsed.get("hook_line") or "").strip()
    body_raw = parsed.get("body") or []
    if isinstance(body_raw, str):
        body = [body_raw.strip()] if body_raw.strip() else []
    else:
        body = [str(x).strip() for x in body_raw if str(x).strip()]
    closer = str(parsed.get("closer") or "").strip()
    if not hook or not body:
        raise ValueError("Narration JSON missing hook_line or body")
    return NarrationScript(hook_line=hook, body_lines=body, closer=closer)


def write_narration_script(
    clip_text: str,
    *,
    topic_name: str,
    hook_title: str | None,
    api_key: str,
    model: str,
    max_words: int = 110,
    min_novelty: float = 0.45,
) -> tuple[NarrationScript, dict[str, Any]]:
    """Gemini rewrite → NarrationScript. Regenerates once if novelty is low."""
    clip_text = (clip_text or "").strip()
    if len(clip_text) < 40:
        raise ValueError("Clip text too short for narration")

    base_prompt = NARRATION_PROMPT.format(
        topic_name=topic_name,
        max_words=max_words,
        hook_title=(hook_title or "").strip() or "(none)",
        clip_text=clip_text[:6000],
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
            # Soft trim: keep hook + as many body lines as fit + closer
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

        novelty = script_novelty_ratio(script.full_text, clip_text)
        meta = {
            **meta,
            "novelty": novelty,
            "word_count": script.word_count,
            "attempt": attempt + 1,
        }
        last_script, last_meta = script, meta
        logger.info(
            "Narration attempt=%d words=%d novelty=%.2f",
            attempt + 1,
            script.word_count,
            novelty,
        )
        if novelty >= min_novelty and script.word_count >= 25:
            return script, meta
        prompt = base_prompt + RETRY_NOVELTY_EXTRA

    assert last_script is not None
    novelty = float(last_meta.get("novelty") or 0.0)
    if novelty < min_novelty:
        raise RuntimeError(
            f"Narration too close to source (novelty={novelty:.2f} < {min_novelty})"
        )
    if last_script.word_count < 25:
        raise RuntimeError("Narration script too short")
    return last_script, last_meta
