# Shorts Factory

Topic-driven **YouTube Shorts** pipeline: discover → download → LLM highlight pick → sentence snap → quality gate → **original VO narration** → karaoke captions → 9:16 render → upload.

Requires **Python 3.13**, **ffmpeg** on `PATH`, **Deno** (YouTube JS challenges for yt-dlp), **faster-whisper** (sentence snap + VO captions), and **edge-tts** (narration audio).

The old Instagram bot lives under [`legacy/instagram/`](legacy/instagram/) and is unused by this app.

## Pipeline

1. Read niches from [`topics.yaml`](topics.yaml) (`enabled: false` = kill-switch).
2. Discover captioned YouTube videos (medium explainers or long theme/podcast-style sources).
3. Download once with `yt-dlp` (retries on flaky CDN cuts).
4. Pull YouTube captions → **Gemini** (or Ollama) picks self-contained clip windows.
   - Long sources: transcript is **chunked** (~15 min); up to **10 non-overlapping** Shorts per video.
5. Per candidate: faster-whisper **sentence snap** → Gemini **verifier** → cut → **visual mode**:
   - **Talking head:** rejected (`reject_talking_heads: true`).
   - **B-roll / animation / diagrams:** Gemini rewrite → **edge-tts VO**, captions follow VO,
     optional **brand bumper** in/out (`enable_brand_bumper`).
6. Store parent `sources` + child `renders` in SQLite (`data/factory.db`). Publish one render at a time.

Silence-based random cutting is **disabled** — every Short comes from transcript + LLM.

Discovery queries favor animation / whiteboard / podcast-explainer sources. Set `reject_talking_heads: false` only for experiments.

**Copyright / monetization risk:** footage is still third-party B-roll. Claims remain possible. Captions/blur alone are not transformative; the VO rewrite is the originality layer. Multi-channel GCP scaling is a separate project.

## Owned templates (parallel A/B path)

Pivot v1+: discovery/transcript is **research only**; visuals are animated kinetic
cards (Pillow frames + Ken Burns / word reveal), not third-party frames.
Remotion can consume the same scene JSON later.

1. Same discover/download (captions as research).
2. `POST /process-owned-next` → Gemini research brief → owned VO script → edge-tts → animated beat cards timed to VO words → optional karaoke (`owned_burn_karaoke`) + bumpers → `media/ready/owned_{video_id}.mp4`.
3. Scene JSON is written to `media/raw/owned_{id}_scenes.json` (stable schema for a future Remotion compositor).
4. Clip factory stays on `/process-next`. Same `source_id` can produce both `renders.kind=clip` and `kind=owned` for A/B.

```powershell
# Prefer a specific source for side-by-side QC
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8010/process-owned-next `
  -ContentType application/json -Body '{"source_id": 1}'
```

Knobs: `owned_max_words`, `owned_enable_bumper`, `owned_min_script_novelty`, `owned_burn_karaoke` (default false — cards reveal VO text).

## Topics

Configured in [`topics.yaml`](topics.yaml):

| Topic | Typical source length |
|-------|------------------------|
| `medicine` | 3–20 min explainers |
| `sleep_science` | 20 min–2 h |
| `brain_performance` | 20 min–2 h |
| `psychology_explained` | 20 min–2 h |

Long topics use YouTube `videoDuration=long`. Knobs: `max_clips_per_source` (default 10), `picker_chunk_sec`, `clip_min_gap_sec`, `min_clip_score`, `verifier_soft_floor`.

## Smart clips (Gemini)

1. Get a key: [Google AI Studio → API key](https://aistudio.google.com/apikey)
2. Copy `.env.example` → `.env` and set `GEMINI_API_KEY=...`
3. `topics.yaml` → `llm_provider: gemini`
4. Restart the server, then `POST /discover` / `POST /process-next`

### Cost (rough)

Gemini Flash-class paid list prices (~**$0.30 / 1M in**, **$2.50 / 1M out**). Free tier is often **$0** within quotas. faster-whisper is local (**$0**).

| Work | Paid estimate |
|------|----------------|
| Short explainer (1 clip) | ~**$0.002 – $0.01** |
| + narration script rewrite | ~**$0.001 – $0.005** extra |
| Long source (chunked pick + several verifiers) | higher; still cents-scale typically |
| edge-tts VO | **$0** |
| App LLM unit | **1 per processed source** (not per chunk; narration calls bundled) |

### YouTube Data API quota

Default free project: **~10,000 units/day**. Upload ≈ **1,600** units → about **6 uploads/day** hard limit. App budget (`youtube_daily_quota_budget: 8000`) caps at **5 uploads/day**. Quota is **per GCP project**, not per YouTube channel.

## Setup (Windows)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3.13 -m pip install -r requirements.txt

winget install Gyan.FFmpeg
winget install DenoLand.Deno
# restart the terminal so ffmpeg + deno are on PATH
Copy-Item .env.example .env
```

### YouTube OAuth

1. Google Cloud Console → project → enable **YouTube Data API v3**.
2. OAuth client (Desktop) → `secrets/client_secrets.json`.
3. First upload/discover opens a browser; token → `secrets/token.json`.

Default privacy is `private` (`PRIVACY_STATUS` in `.env`).

### YouTube download bot-check

YouTube often blocks anonymous yt-dlp downloads. **Cookie file** is the reliable fix on Windows (Chrome locks its DB while open):

```powershell
# Quit Chrome completely first (system tray too)
python -m src.ingest.export_cookies
# or: python -m src.ingest.export_cookies --browser firefox
```

Then in `.env`:

```
YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt
```

Restart the server and `POST /process-next`. Re-export cookies if downloads start failing again (sessions expire).

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn src.main:app --host 127.0.0.1 --port 8010
```

Port **8010** by default (`APP_PORT` to override).

### Manual endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Queue + source/render/quota snapshot |
| POST | `/discover` | Search & enqueue (`{"topic_id":"sleep_science"}` optional) |
| POST | `/process-next` | Clip factory: download + B-roll/VO Shorts (may create **multiple** `renders`) |
| POST | `/process-owned-next` | Owned kinetic path (`source_id` / `topic_id` optional); no foreign footage |
| POST | `/publish-next` | Upload next rendered Short (one `render` at a time) |
| POST | `/upload-test` | Prove OAuth with a local MP4 |

Scheduler intervals come from `topics.yaml` → `pipeline`. Set `ENABLE_SCHEDULER=false` for manual-only.

Optional full-video Whisper fallback (heavy):

```powershell
py -3.13 -m pip install setuptools openai-whisper
```

Then `use_whisper: true` in `topics.yaml`. Sentence snapping already uses **faster-whisper** separately.

## X (stub)

[`src/publish/x.py`](src/publish/x.py) — stubs only. No X API spend yet.

## Legacy Instagram

See [`legacy/instagram/README.md`](legacy/instagram/README.md).
