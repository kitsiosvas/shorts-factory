# Legacy Instagram Reel bot

Archived from the original project. **Not used by the active Shorts factory.**

## What it does

Scrapes Reels from configured similar accounts via `instagrapi`, downloads with `yt-dlp`, and posts them back to Instagram on a schedule.

## How to run standalone

```bash
cd legacy/instagram
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in this folder (or the repo root) with:

```
ig_username=...
ig_password=...
vids_dir=vids
similar_accounts=["account1","account2"]
```

From `legacy/instagram` (so imports resolve):

```bash
cd src
uvicorn main:app --port 8000
```

Session state is stored in `settings.json` in the working directory. Keep credentials out of git.
