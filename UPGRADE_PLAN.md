# Shorts Polish Upgrade Plan

Goal: make generated Shorts feel complete and self-contained — stricter Gemini
brief, sentence-accurate cuts, burned karaoke captions, rejection of weak picks,
better source filtering. Approved scope: all four workstreams + faster-whisper
as a new dependency.

## Why clips feel abrupt today (research findings)

1. **Gemini can only cut on caption-cue boundaries.** The transcript sent to the
   LLM is cue-level YouTube VTT (phrase fragments), so its start/end timestamps
   land mid-sentence. Nothing snaps them afterward; `extract_clip` uses raw
   floats and hides the seam with a 0.45s fade.
2. **No quality gate.** `_parse_clips` sorts by self-reported score; there is no
   minimum threshold and no second-pass check for self-containment.
3. **No burned dialogue captions.** 60–85% of Shorts viewers start muted;
   burned phrase-by-phrase captions lift sound-off retention ~25–40%. Highest
   leverage retention fix available.
4. **Prompt was a one-shot ask** — one candidate, no boundary quotes, no
   structure checklist, no permission to return zero clips.

### Monetization / originality layer (MVP shipped)

YouTube Shorts revenue sharing excludes non-original reuploads; captions/blur-pad
do not count. MVP transformation (flag `enable_narration` in `topics.yaml`):

- Gemini rewrites clip transcript → host VO script (novelty gate)
- `edge-tts` synthesizes audio; source dialogue is muted
- Karaoke captions follow the VO (faster-whisper on the TTS file)
- Description includes source link + disclosure

Still not legal advice: Content ID claims on footage remain possible. Multi-channel
GCP scaling stays out of scope.

---

## Status

- [x] 1. Stricter Gemini brief (3 candidates, verbatim first/last sentence
      quotes, allow empty result, harsh scoring) — `src/intelligence/highlights.py`
- [x] 2. Verifier pass `verify_clip_gemini()` + `VERIFIER_PROMPT` + refactored
      `_gemini_generate_json()` helper — `src/intelligence/highlights.py`
- [x] `HighlightClip.first_sentence` / `.last_sentence` fields —
      `src/intelligence/models.py`
- [x] 3. Word-level timestamps + sentence snapping (`src/intelligence/words.py`)
- [x] 4. ASS karaoke captions (`src/render/subtitles.py`) burned in vertical pass
- [x] 5. Discovery filter `videoCaption=closedCaption`
- [x] 6. Config knobs + pipeline integration + requirements
- [x] 7. End-to-end test on one source video

---

## 3. `src/intelligence/words.py` (new module)

Purpose: word-level timestamps around a candidate window via faster-whisper,
sentence building, and boundary snapping.

```python
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
    words: list[Word]
```

Functions:

- `_get_model(model_name)` — `@lru_cache(maxsize=1)`, returns
  `faster_whisper.WhisperModel(model_name, device="cpu", compute_type="int8")`.
- `transcribe_window_words(video_path, window_start, window_end, *, model_name="base", pad_sec=20.0) -> list[Word]`
  - Extract mono 16kHz wav of `[window_start - pad_sec, window_end + pad_sec]`
    with ffmpeg (`-ss OFFSET -t LENGTH -i src -vn -ac 1 -ar 16000 out.wav`,
    use `require_ffmpeg()` from `src/render/ffmpeg_utils.py`, tempdir).
  - `model.transcribe(wav, word_timestamps=True, language="en", vad_filter=True)`
  - Offset every word timestamp by the extraction offset so results are in
    absolute source-video seconds. Strip empty word texts.
  - Raise RuntimeError with an install hint if faster-whisper is not importable.
- `build_sentences(words) -> list[Sentence]` — split after a word whose text
  ends with `.`, `!`, `?`, or `…`, OR when the gap to the next word > 1.0s.
- `snap_clip_to_sentences(words, start, end, *, min_sec, max_sec) -> tuple[float, float, list[Word]]`
  - Build sentences; if empty, return `(start, end, [])`.
  - Snapped start = start of the sentence containing `start` (if `start` falls
    in a gap, the next sentence's start).
  - Snapped end = end of the sentence containing `end` (if in a gap, the
    previous sentence's end).
  - Enforce duration: if > `max_sec`, drop trailing sentences while duration
    stays >= `min_sec`; last resort trim end to `start + max_sec`. If <
    `min_sec`, append following sentences while <= `max_sec`.
  - Padding: start -= 0.15 but not before the previous word's end;
    end += 0.35 but not past the next word's start. Clamp start >= 0.
  - Return snapped bounds + the words inside them (used for captions and the
    verifier text).

## 4. `src/render/subtitles.py` (new module)

- `words_to_ass(words, *, clip_start, output_path, max_words_per_line=3) -> Path`
  - Timestamps relative to `clip_start` (captions are burned on the already-cut
    clip). ASS header: `PlayResX: 1080`, `PlayResY: 1920`,
    `ScaledBorderAndShadow: yes`, `WrapStyle: 2`.
  - Style (karaoke: spoken words fill PrimaryColour, upcoming show
    SecondaryColour):
    `Style: Caption,Arial,96,&H0000FFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,0,2,60,60,560,1`
    (Primary yellow, Secondary white, thick black outline 7, Alignment 2 bottom
    center, MarginV 560 = lower-middle safe zone above the Shorts UI).
  - Group words into lines of <= `max_words_per_line`, breaking early at
    sentence-ending punctuation. One `Dialogue:` per line group; per-word
    `{\k<centiseconds>}` tags where each word's k-duration includes the gap to
    the next word in the line. Time format `H:MM:SS.CC`.
- `segments_to_ass(segments, *, clip_start, clip_end, output_path) -> Path` —
  fallback when whisper words are unavailable: cue-level lines from
  `TranscriptSegment`s overlapping the clip, same style, no `\k` tags, white
  PrimaryColour.
- `ass_filter_arg(path) -> str` — Windows-safe filter escaping:
  `str(path).replace("\\", "/").replace(":", "\\:")` wrapped as `ass='...'`.

## 5. `src/render/vertical.py`

Add optional `ass_path: Path | None = None` to `to_vertical_916`. When set,
append `,{ass_filter_arg(ass_path)}` to the end of the existing filter_complex
chain (after `setsar=1`). No other changes.

## 6. `src/render/clipper.py`

Lower default `fade_seconds` from 0.45 to 0.20 — with sentence-snapped bounds a
long fade eats the first spoken word of the hook.

## 7. `src/discover/youtube.py`

In the `search().list(...)` call add:
- `videoCaption="closedCaption"` when `topic.require_captions` (new config,
  default True) — avoids wasted downloads on caption-less videos.
- `videoLicense="creativeCommon"` when `topic.creative_commons_only` (default
  False).

## 8. Config knobs

`src/config.py`:
- `TopicConfig`: `require_captions: bool = True`,
  `creative_commons_only: bool = False`
- `PipelineConfig`: `min_clip_score: int = 70`, `enable_verifier: bool = True`,
  `burn_captions: bool = True`, `snap_to_sentences: bool = True`,
  `snap_whisper_model: str = "base"`

`topics.yaml` pipeline section — add the same five keys with those values.

`requirements.txt` — add `faster-whisper` (latest; pulls ctranslate2, CPU-only
fine).

## 9. Pipeline integration (`src/workers/pipeline.py`, `process_next`)

Replace the single `best = clips[0]` block with candidate iteration:

```python
selected = None
rejections = []
for cand in clips[:3]:
    if cand.score < pipeline.min_clip_score:
        rejections.append(f"{cand.start_sec:.0f}-{cand.end_sec:.0f}s: picker score {cand.score:.0f} < floor")
        continue
    start, end = cand.start_sec, cand.end_sec
    clip_words = []
    if pipeline.snap_to_sentences:
        try:
            words = transcribe_window_words(raw_path, start, end, model_name=pipeline.snap_whisper_model)
            start, end, clip_words = snap_clip_to_sentences(words, start, end,
                min_sec=float(pipeline.clip_min_seconds), max_sec=float(pipeline.clip_max_seconds))
        except Exception:
            logger.exception("Sentence snapping failed; using raw LLM bounds")
    clip_text = (" ".join(w.text for w in clip_words) if clip_words
                 else transcript.text_between(start, end))
    if pipeline.enable_verifier and pipeline.llm_provider == "gemini":
        v = verify_clip_gemini(clip_text, api_key=self.settings.gemini_api_key, model=pipeline.gemini_model)
        if v["verdict"] != "pass" or (0 <= v["score"] < pipeline.min_clip_score):
            rejections.append(f"{start:.0f}-{end:.0f}s: verifier {v['verdict']} score={v['score']:.0f} ({v['issues']})")
            continue
    selected = (cand, start, end, clip_words)
    break
if selected is None:
    raise RuntimeError(f"All candidate clips rejected: {rejections}")
```

Then:
- `extract_clip(...)` with snapped bounds (unchanged signature).
- If `pipeline.burn_captions`: build ASS next to the clip
  (`{video_id}_subs.ass` in `media_raw_dir`) via `words_to_ass(clip_words,
  clip_start=start, ...)`, falling back to
  `segments_to_ass(transcript.segments, clip_start=start, clip_end=end, ...)`
  when `clip_words` is empty. Pass `ass_path` to `to_vertical_916`. Delete the
  .ass with the other temp files.
- Keep `burn_hook_text` as the final pass (top title) unchanged.
- Log the rejection list and the verifier result for observability.
- LLM daily quota: keep counting 1 per processed video (verifier calls are
  bundled into the same unit).

Imports: `from src.intelligence.words import snap_clip_to_sentences,
transcribe_window_words`, `from src.intelligence.highlights import
verify_clip_gemini`, `from src.render.subtitles import segments_to_ass,
words_to_ass`.

## 10. Test procedure

1. `pip install faster-whisper` (and add to requirements.txt).
2. Restart the uvicorn server (it runs on 127.0.0.1:8010; a session may already
   be live in a terminal — kill/restart it so new code loads).
3. `POST http://127.0.0.1:8010/discover` (topic `medicine`), then
   `POST /process-next`. Watch `app.log` for: candidate scores, snap deltas
   (raw vs snapped bounds), verifier verdicts.
4. Inspect `media/ready/{video_id}.mp4`: cut must start/end on sentence
   boundaries, karaoke captions visible in lower-middle, hook title on top.
5. Failure modes to check: source with no qualifying clip → status `failed`
   with "All candidate clips rejected"; whisper import missing → snapping
   skipped with a logged warning, pipeline still renders.

## Acceptance criteria

- No mid-word/mid-sentence cut on rendered output.
- Weak picks (picker score or verifier score < 70, or verifier fail) never
  render; source marked failed with the rejection reasons.
- Burned word-timed captions present when `burn_captions: true`.
- Discovery only returns captioned videos when `require_captions: true`.
