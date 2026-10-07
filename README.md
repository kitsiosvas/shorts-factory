# Shorts Factory

LLM video pipeline (FastAPI, Gemini/Ollama, faster-whisper, edge-tts) that picks self-contained clips from long YouTube transcripts, checks each with a second LLM pass, and renders narrated 9:16 Shorts with captions.

Needs **Python 3.13**, **ffmpeg**, and **Deno** (yt-dlp's YouTube JS challenges).

## Pipeline

1. Niches live in [`topics.yaml`](topics.yaml). `enabled: false` turns one off.
2. Discover captioned YouTube explainers, or longer podcast-style sources.
3. Download once with `yt-dlp`.
4. Gemini (or Ollama) picks self-contained windows from the transcript. Long sources are chunked (~15 min), up to 10 non-overlapping Shorts per video.
5. faster-whisper snaps each window to sentence boundaries. A second Gemini pass verifies it.
6. Talking heads are dropped. B-roll, animation, and diagrams get a rewritten script, edge-tts narration, karaoke captions, and an optional brand bumper.
7. Parents land in `sources`, children in `renders` (`data/factory.db`). Publish one render at a time.

Every Short comes from a transcript plus the LLM.

`POST /process-owned-next` is a second path: the transcript is research only, and the render is original kinetic cards. The same source can produce both a clip and an owned render.

Clip footage is still third-party B-roll, so copyright claims stay possible. The VO rewrite is the originality layer.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3.13 -m pip install -r requirements.txt

winget install Gyan.FFmpeg
winget install DenoLand.Deno
Copy-Item .env.example .env
```

Restart the terminal so `ffmpeg` and `deno` are on `PATH`. Set `GEMINI_API_KEY` in `.env` and `llm_provider: gemini` in `topics.yaml`. Key: [Google AI Studio](https://aistudio.google.com/apikey).

YouTube upload and discover need a Desktop OAuth client at `secrets/client_secrets.json` (YouTube Data API v3). The first call opens a browser and writes `secrets/token.json`. Uploads default to `private`.

If yt-dlp is blocked, quit Chrome and run `python -m src.ingest.export_cookies`, then set `YTDLP_COOKIES_FILE=secrets/youtube_cookies.txt`.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn src.main:app --host 127.0.0.1 --port 8010
```

| Method | Path | What it does |
|--------|------|----------------|
| GET | `/health` | Liveness |
| GET | `/status` | Queue, sources, renders, quota |
| POST | `/discover` | Search and enqueue |
| POST | `/process-next` | Download and render B-roll/VO Shorts |
| POST | `/process-owned-next` | Render owned kinetic Shorts |
| POST | `/publish-next` | Upload the next render |
| POST | `/upload-test` | Prove OAuth with a local MP4 |

The scheduler reads `topics.yaml` → `pipeline`. Set `ENABLE_SCHEDULER=false` to drive it by hand.

The old Instagram bot is unused, under [`legacy/instagram/`](legacy/instagram/).
