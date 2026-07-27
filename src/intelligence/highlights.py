from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from src.intelligence.models import HighlightClip, Transcript

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert short-form video editor for educational
YouTube Shorts (science, medicine, explainers). Given a timestamped transcript,
pick up to {max_clips:.0f} candidate clips (best first).

A clip is ONLY acceptable if ALL of these hold:
- Self-contained: a viewer with ZERO context understands it fully. No dangling
  references ("as I said earlier", "this" with no antecedent, setups answered
  outside the clip).
- Hook: the FIRST spoken sentence works on its own. Valid hooks include a
  question, bold claim, surprising fact, OR a concrete educational opener
  ("here's what X actually does", a clear named topic with a promise to explain).
  Prefer windows that contain setup + punchline / complete answer inside the
  duration budget.
- Payoff: the clip ends on a completed thought, answer, or punchline — never
  on a trailing setup or an unfinished list. Prefer keeping the answer rather
  than a longer lead-in.
- Boundaries: start_sec is the start of a sentence, end_sec is the end of a
  sentence. Never cut mid-sentence.
- Duration between {min_sec:.0f} and {max_sec:.0f} seconds.
- No channel intros, outros, ads, sponsor reads, subscribe CTAs, or dead air.

If NOTHING in the transcript qualifies, return {{"clips": []}}.
Do NOT force a weak pick — an empty result is better than a mediocre clip.

Return ONLY valid JSON (no markdown) with this shape:
{{
  "clips": [
    {{
      "start_sec": 12.5,
      "end_sec": 48.0,
      "first_sentence": "verbatim first sentence spoken in the clip",
      "last_sentence": "verbatim last sentence spoken in the clip",
      "hook_title": "Short punchy title under 70 chars",
      "reason": "why this works as a standalone Short",
      "score": 0-100
    }}
  ]
}}
Score: 90+ exceptional, 70-89 solid educational Short, below 60 should not
be published. Order clips by score descending. Return at most {max_clips:.0f} clips.
"""

VERIFIER_PROMPT = """You are a quality gate for educational YouTube Shorts
(science, medicine, explainers). Below is the complete spoken text of a
candidate clip — exactly what the viewer will hear, with NO other context.

FAIL the clip ONLY if any of these are true:
- It starts mid-thought or references context that is not in the text.
- It ends without a payoff (trailing setup, unfinished idea, cut-off list).
- It is pure ramble with no clear takeaway, or is mostly ads/CTAs.

PASS educational clips that open with a definition or "X is…" if the rest
delivers a complete, interesting explanation a stranger can follow. A clear
fact-driven opener is enough of a hook for this niche — do NOT fail solely
for sounding textbook-like.

Return ONLY valid JSON (no markdown):
{"score": 0-100, "verdict": "pass" or "fail", "issues": "one short sentence"}

Score: 70+ publishable educational Short, 55-69 borderline but usable,
below 55 fail. Prefer pass when the clip is self-contained and complete.

Clip text:
"""


def estimate_gemini_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_per_mtok: float = 0.30,
    output_per_mtok: float = 2.50,
) -> float:
    """Paid Gemini 2.5 Flash list prices (approx). Free tier = $0."""
    return (input_tokens / 1_000_000) * input_per_mtok + (
        output_tokens / 1_000_000
    ) * output_per_mtok


def rough_token_estimate(text: str) -> int:
    # ~4 chars/token heuristic for English
    return max(1, len(text) // 4)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _parse_clips(payload: dict[str, Any], *, video_duration: float, max_sec: float) -> list[HighlightClip]:
    clips: list[HighlightClip] = []
    for item in payload.get("clips") or []:
        start = float(item["start_sec"])
        end = float(item["end_sec"])
        if end <= start:
            continue
        start = max(0.0, start)
        end = min(video_duration, end)
        if end - start > max_sec:
            end = start + max_sec
        if end - start < 8:
            continue
        clips.append(
            HighlightClip(
                start_sec=start,
                end_sec=end,
                hook_title=str(item.get("hook_title") or "Interesting moment").strip(),
                reason=str(item.get("reason") or "").strip(),
                score=float(item.get("score") or 0),
                first_sentence=str(item.get("first_sentence") or "").strip(),
                last_sentence=str(item.get("last_sentence") or "").strip(),
            )
        )
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips


GEMINI_MODEL_FALLBACKS = (
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


def _gemini_generate_json(
    user_prompt: str,
    *,
    api_key: str,
    model: str,
    temperature: float = 0.3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call Gemini generateContent (with model fallbacks) and parse JSON output."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    # Prefer header so the key is not printed in httpx URL logs.
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    candidates = [model, *[m for m in GEMINI_MODEL_FALLBACKS if m != model]]
    last_error = ""
    data: dict[str, Any] | None = None
    used_model = model

    with httpx.Client(timeout=90.0) as client:
        for candidate in candidates:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{candidate}:generateContent"
            )
            response = client.post(url, headers=headers, json=payload)
            if response.status_code in {404, 429}:
                last_error = f"{candidate}: {response.status_code} {response.text[:400]}"
                logger.warning(
                    "Gemini model %s returned %s; trying fallback",
                    candidate,
                    response.status_code,
                )
                continue
            if response.is_error:
                raise RuntimeError(
                    f"Gemini API {response.status_code} for model={candidate}: "
                    f"{response.text[:500]}"
                )
            data = response.json()
            used_model = candidate
            break

    if data is None:
        raise RuntimeError(
            "No Gemini model accepted generateContent (404/429 on all tried). "
            f"Last error: {last_error}. "
            "Free tier is often limit:0 for some Flash models — enable billing "
            "at https://ai.google.dev/gemini-api/docs/rate-limits or try "
            "gemini-2.5-flash-lite / a new AI Studio project with free quota."
        )

    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    if not text:
        raise RuntimeError(f"Gemini returned empty content: {str(data)[:500]}")

    usage = data.get("usageMetadata") or {}
    input_tokens = int(
        usage.get("promptTokenCount") or rough_token_estimate(user_prompt)
    )
    output_tokens = int(
        usage.get("candidatesTokenCount") or rough_token_estimate(text)
    )
    meta = {
        "provider": "gemini",
        "model": used_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(
            estimate_gemini_cost_usd(input_tokens=input_tokens, output_tokens=output_tokens),
            6,
        ),
    }
    return _extract_json(text), meta


def pick_highlights_gemini(
    transcript: Transcript,
    *,
    api_key: str,
    model: str,
    video_duration: float,
    min_sec: float,
    max_sec: float,
    topic_name: str,
    max_clips: int = 3,
) -> tuple[list[HighlightClip], dict[str, Any]]:
    body_transcript = transcript.as_numbered_lines()
    system = SYSTEM_PROMPT.format(
        min_sec=min_sec, max_sec=max_sec, max_clips=float(max_clips)
    )
    user_prompt = (
        f"{system}\n\n"
        f"Topic niche: {topic_name}\n"
        f"Video duration: {video_duration:.1f}s\n\n"
        f"Transcript:\n{body_transcript}"
    )
    parsed, meta = _gemini_generate_json(user_prompt, api_key=api_key, model=model)
    clips = _parse_clips(parsed, video_duration=video_duration, max_sec=max_sec)
    return clips[:max_clips], meta


def verify_clip_gemini(
    clip_text: str,
    *,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Second-pass quality gate: judge the clip's spoken text in isolation.

    Returns {"score": float, "verdict": "pass"|"fail", "issues": str, "meta": dict}.
    On API/parse failure the clip is passed through (gate fails open) so a flaky
    verifier cannot stall the whole pipeline.
    """
    try:
        parsed, meta = _gemini_generate_json(
            VERIFIER_PROMPT + clip_text.strip(),
            api_key=api_key,
            model=model,
            temperature=0.0,
        )
        return {
            "score": float(parsed.get("score") or 0),
            "verdict": str(parsed.get("verdict") or "fail").lower().strip(),
            "issues": str(parsed.get("issues") or "").strip(),
            "meta": meta,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Clip verifier failed (%s); passing clip through", exc)
        return {"score": -1.0, "verdict": "pass", "issues": f"verifier error: {exc}", "meta": {}}


def pick_highlights_ollama(
    transcript: Transcript,
    *,
    base_url: str,
    model: str,
    video_duration: float,
    min_sec: float,
    max_sec: float,
    topic_name: str,
    max_clips: int = 3,
) -> tuple[list[HighlightClip], dict[str, Any]]:
    body_transcript = transcript.as_numbered_lines()
    system = SYSTEM_PROMPT.format(
        min_sec=min_sec, max_sec=max_sec, max_clips=float(max_clips)
    )
    user_prompt = (
        f"Topic niche: {topic_name}\n"
        f"Video duration: {video_duration:.1f}s\n\n"
        f"Transcript:\n{body_transcript}"
    )
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
    }
    with httpx.Client(timeout=180.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    text = (data.get("message") or {}).get("content") or ""
    meta = {
        "provider": "ollama",
        "model": model,
        "input_tokens": rough_token_estimate(system + user_prompt),
        "output_tokens": rough_token_estimate(text),
        "estimated_cost_usd": 0.0,
    }
    parsed = _extract_json(text)
    clips = _parse_clips(parsed, video_duration=video_duration, max_sec=max_sec)
    return clips[:max_clips], meta


def _clips_overlap(a: HighlightClip, b: HighlightClip, *, min_gap: float) -> bool:
    return not (a.end_sec + min_gap <= b.start_sec or b.end_sec + min_gap <= a.start_sec)


def select_non_overlapping(
    clips: list[HighlightClip],
    *,
    max_clips: int,
    min_gap_sec: float,
) -> list[HighlightClip]:
    """Greedy keep highest-score clips that do not overlap (with gap)."""
    selected: list[HighlightClip] = []
    for cand in sorted(clips, key=lambda c: c.score, reverse=True):
        if any(_clips_overlap(cand, kept, min_gap=min_gap_sec) for kept in selected):
            continue
        selected.append(cand)
        if len(selected) >= max_clips:
            break
    selected.sort(key=lambda c: c.start_sec)
    return selected


def _iter_time_chunks(
    transcript: Transcript,
    *,
    video_duration: float,
    chunk_sec: float,
) -> list[tuple[Transcript, float, float]]:
    if chunk_sec <= 0:
        chunk_sec = video_duration or 1.0
    chunks: list[tuple[Transcript, float, float]] = []
    t = 0.0
    while t < video_duration - 1e-6:
        end = min(t + chunk_sec, video_duration)
        sliced = transcript.slice_between(t, end)
        if sliced.segments:
            chunks.append((sliced, t, end))
        t = end
    if not chunks and transcript.segments:
        chunks.append((transcript, 0.0, video_duration))
    return chunks


def pick_highlights(
    transcript: Transcript,
    *,
    provider: str,
    video_duration: float,
    min_sec: float,
    max_sec: float,
    topic_name: str,
    gemini_api_key: str | None,
    gemini_model: str,
    ollama_base_url: str,
    ollama_model: str,
    max_clips_per_source: int = 10,
    picker_chunk_sec: float = 900.0,
    clip_min_gap_sec: float = 30.0,
) -> tuple[list[HighlightClip], dict[str, Any]]:
    provider = (provider or "none").lower().strip()
    if provider in {"none", "off", "disabled"}:
        return [], {"provider": "none", "estimated_cost_usd": 0.0}
    if not transcript.segments:
        return [], {"provider": provider, "error": "empty transcript", "estimated_cost_usd": 0.0}

    max_clips_per_source = max(1, int(max_clips_per_source))
    chunks = _iter_time_chunks(
        transcript,
        video_duration=video_duration,
        chunk_sec=float(picker_chunk_sec),
    )
    # Short sources: one call asking for up to 3 (or max_clips if smaller).
    # Long sources: up to 2 per chunk, then merge/dedupe.
    per_chunk_cap = (
        min(3, max_clips_per_source)
        if len(chunks) <= 1
        else min(2, max_clips_per_source)
    )

    all_clips: list[HighlightClip] = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    used_model = gemini_model if provider == "gemini" else ollama_model
    chunk_calls = 0

    for chunk_transcript, chunk_start, chunk_end in chunks:
        chunk_calls += 1
        logger.info(
            "Picker chunk %.0f-%.0fs (%d segs) max_clips=%d",
            chunk_start,
            chunk_end,
            len(chunk_transcript.segments),
            per_chunk_cap,
        )
        if provider == "gemini":
            if not gemini_api_key:
                raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is missing")
            clips, meta = pick_highlights_gemini(
                chunk_transcript,
                api_key=gemini_api_key,
                model=gemini_model,
                video_duration=video_duration,
                min_sec=min_sec,
                max_sec=max_sec,
                topic_name=topic_name,
                max_clips=per_chunk_cap,
            )
        elif provider == "ollama":
            clips, meta = pick_highlights_ollama(
                chunk_transcript,
                base_url=ollama_base_url,
                model=ollama_model,
                video_duration=video_duration,
                min_sec=min_sec,
                max_sec=max_sec,
                topic_name=topic_name,
                max_clips=per_chunk_cap,
            )
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r} (use gemini|ollama|none)")

        all_clips.extend(clips)
        total_in += int(meta.get("input_tokens") or 0)
        total_out += int(meta.get("output_tokens") or 0)
        total_cost += float(meta.get("estimated_cost_usd") or 0)
        used_model = str(meta.get("model") or used_model)

    selected = select_non_overlapping(
        all_clips,
        max_clips=max_clips_per_source,
        min_gap_sec=float(clip_min_gap_sec),
    )
    meta = {
        "provider": provider,
        "model": used_model,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "estimated_cost_usd": round(total_cost, 6),
        "chunk_calls": chunk_calls,
        "raw_candidates": len(all_clips),
        "selected_candidates": len(selected),
    }
    return selected, meta
