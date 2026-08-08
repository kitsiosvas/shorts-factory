from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx

from src.intelligence.highlights import (
    GEMINI_MODEL_FALLBACKS,
    _extract_json,
    estimate_gemini_cost_usd,
    rough_token_estimate,
)
from src.render.ffmpeg_utils import require_ffmpeg

logger = logging.getLogger(__name__)

VisualMode = Literal["talking_head", "broll"]

CLASSIFY_PROMPT = """You classify a YouTube Shorts source clip for editing.

Look at the frame and the spoken transcript excerpt.

Return talking_head if a real person's face/upper body is the PRIMARY subject
(doctor/host talking to camera, interview close-up, selfie-style explainer,
split-screen where a large face dominates).

Return broll if the frame is mostly useful WITHOUT relying on a talking face:
animations, motion graphics, whiteboard / illustrated explainers, diagrams,
slides, stock footage, screen recordings, text-on-screen lessons, or a small
face inset while graphics dominate.

When unsure, return talking_head (we will reject those).

Return ONLY JSON:
{{"mode": "talking_head" or "broll", "reason": "one short sentence"}}

Transcript excerpt:
{clip_text}
"""


def extract_frame_jpeg(video_path: Path, at_sec: float, output_path: Path) -> Path:
    ffmpeg = require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    at_sec = max(0.0, float(at_sec))
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{at_sec:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"frame extract failed: {result.stderr[-800:]}")
    return output_path


def _gemini_json_with_image(
    prompt: str,
    image_path: Path,
    *,
    api_key: str,
    model: str,
    temperature: float = 0.1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
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
                    "Gemini vision model %s returned %s; trying fallback",
                    candidate,
                    response.status_code,
                )
                continue
            if response.is_error:
                raise RuntimeError(
                    f"Gemini vision API {response.status_code} for model={candidate}: "
                    f"{response.text[:500]}"
                )
            data = response.json()
            used_model = candidate
            break

    if data is None:
        raise RuntimeError(f"Gemini vision failed on all models. Last: {last_error}")

    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    if not text:
        raise RuntimeError(f"Gemini vision empty content: {str(data)[:500]}")

    usage = data.get("usageMetadata") or {}
    input_tokens = int(usage.get("promptTokenCount") or rough_token_estimate(prompt))
    output_tokens = int(usage.get("candidatesTokenCount") or rough_token_estimate(text))
    meta = {
        "provider": "gemini",
        "model": used_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(
            estimate_gemini_cost_usd(
                input_tokens=input_tokens, output_tokens=output_tokens
            ),
            6,
        ),
    }
    return _extract_json(text), meta


def classify_visual_mode(
    video_path: Path,
    *,
    start_sec: float,
    end_sec: float,
    clip_text: str,
    api_key: str,
    model: str,
) -> tuple[VisualMode, dict[str, Any]]:
    """Return talking_head | broll. On failure, default talking_head."""
    mid = (float(start_sec) + float(end_sec)) / 2.0
    try:
        prompt = CLASSIFY_PROMPT.format(
            clip_text=(clip_text or "")[:2500] or "(empty)"
        )
        with tempfile.TemporaryDirectory(prefix="visual_mode_") as tmp:
            frame = Path(tmp) / "frame.jpg"
            extract_frame_jpeg(video_path, mid, frame)
            parsed, meta = _gemini_json_with_image(
                prompt, frame, api_key=api_key, model=model
            )
        mode_raw = str(parsed.get("mode") or "").strip().lower().replace("-", "_")
        if mode_raw in {"talking_head", "talkinghead", "face", "host"}:
            mode: VisualMode = "talking_head"
        elif mode_raw in {"broll", "b_roll", "animation", "diagram", "visuals"}:
            mode = "broll"
        else:
            mode = "talking_head"
        meta = {
            **meta,
            "mode": mode,
            "reason": str(parsed.get("reason") or "").strip(),
        }
        logger.info("Visual mode=%s reason=%s", mode, meta.get("reason"))
        return mode, meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("Visual mode classify failed (%s); default talking_head", exc)
        return "talking_head", {"mode": "talking_head", "error": str(exc)}
