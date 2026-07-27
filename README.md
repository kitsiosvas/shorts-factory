# Shorts Factory

Topic-driven **YouTube Shorts** pipeline (discover → download → clip → caption → upload), with shared publisher interfaces and an **X engagement stub** for later.

Requires **Python 3.13** and **ffmpeg** on `PATH`.

The old Instagram bot is preserved under [`legacy/instagram/`](legacy/instagram/) and is not used by this app.

## Pipeline

1. Read niches from [`topics.yaml`](topics.yaml) (`enabled: false` = kill-switch).
2. Discover popular YouTube videos via YouTube Data API.
3. Download with `yt-dlp`.
4. **Highlight selection:** pull free YouTube captions → optional **Gemini / Ollama** picks a self-contained clip (falls back to silence-based cut if `llm_provider: none`).
5. Cut with fades, blur-pad to 9:16, burn hook text, upload as Short.
6. Track sources/posts/quota in SQLite (`data/factory.db`).

**Copyright risk:** this reuses others’ footage. Claims/strikes/monetization blocks are likely. Source IDs are stored for audit/pause.

## Smart clips (Gemini / Ollama) — required

Silence-based random cutting is **disabled**. Every Short must come from transcript + LLM.

1. Get a key: [Google AI Studio → API key](https://aistudio.google.com/apikey)
2. Create `.env` in the repo root (copy from `.env.example`) and add:
   ```
   GEMINI_API_KEY=your_key_here
   ```
3. `topics.yaml` already has `llm_provider: gemini` and `max_llm_highlights_per_day: 5`
4. Restart the server, then `POST /process-next`

### Gemini cost per video (estimate)

Using **Gemini 2.5 Flash** paid list prices (~**$0.30 / 1M input**, **$2.50 / 1M output**). Free tier is often **$0** within quotas.

| Source length | Typical tokens (in+out) | Paid cost |
|---------------|-------------------------|-----------|
| ~10 min | ~4k–8k | **~$0.002 – $0.005** |
| ~15–20 min | ~8k–15k | **~$0.005 – $0.01** |
| 3 Shorts/day | — | **~$0.02 – $0.03/day** |

Transcripts prefer **YouTube auto-captions** (free). If a video has no captions, processing fails unless you set `use_whisper: true`.

Each `/process-next` response includes `highlight.estimated_cost_usd` when Gemini runs.

## Setup (Windows)

```powershell
# Use Python 3.13 explicitly (py launcher)
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3.13 -m pip install -r requirements.txt

# ffmpeg (required for clipping)
winget install Gyan.FFmpeg
# then restart the terminal so ffmpeg is on PATH
```

Copy env template:

```powershell
Copy-Item .env.example .env
```

### YouTube OAuth

1. Google Cloud Console → create a project → enable **YouTube Data API v3**.
2. Create OAuth client (Desktop app) → download JSON → save as `secrets/client_secrets.json`.
3. First upload/discover will open a browser; token is saved to `secrets/token.json`.

Default upload privacy is `private` (`PRIVACY_STATUS` in `.env`) so you can review before going public.

## Run

From the repo root:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn src.main:app --host 127.0.0.1 --port 8010
```

Default port is **8010** (avoids clashing with other local apps on 8000). Override with `APP_PORT` in `.env` or `--port`.

### Manual endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Queue + quota snapshot |
| POST | `/discover` | Search & enqueue sources (`{"topic_id":"medicine"}` optional) |
| POST | `/process-next` | Download + render one Short |
| POST | `/publish-next` | Upload next rendered Short |
| POST | `/upload-test` | Prove OAuth with a local MP4 (`{"video_path":"...","title":"... #Shorts"}`) |

Scheduler intervals come from `topics.yaml` → `pipeline`. Set `ENABLE_SCHEDULER=false` to run manual-only.

Optional Whisper captions (heavy; pulls torch):

```powershell
py -3.13 -m pip install setuptools openai-whisper
```

Then set `use_whisper: true` under `pipeline` in `topics.yaml`.

## X (stub)

[`src/publish/x.py`](src/publish/x.py) defines `TrendsProvider`, `EngagementAgent`, and `XPublisher` stubs. No X API spend until Phase 3.

## Legacy Instagram

See [`legacy/instagram/README.md`](legacy/instagram/README.md).
