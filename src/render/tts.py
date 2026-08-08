from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "en-US-AndrewNeural"


def synthesize_speech(
    text: str,
    output_path: Path,
    *,
    voice: str = DEFAULT_VOICE,
) -> Path:
    """Generate narration audio via edge-tts (free neural voices)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("TTS text is empty")

    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts is not installed. Run: pip install edge-tts"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Prefer mp3; ffmpeg handles it fine when muxing.
    if output_path.suffix.lower() not in {".mp3", ".wav", ".m4a"}:
        output_path = output_path.with_suffix(".mp3")

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output_path))

    logger.info("TTS voice=%s chars=%d -> %s", voice, len(text), output_path.name)
    asyncio.run(_run())
    if not output_path.exists() or output_path.stat().st_size < 100:
        raise RuntimeError(f"edge-tts produced empty file: {output_path}")
    return output_path
