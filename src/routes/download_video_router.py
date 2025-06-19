from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
import yt_dlp
import os
import json
import logging
import random
import hashlib
import time
from config import settings, ig_client

logger = logging.getLogger(__name__)

router_download = APIRouter()

# JSON file for tracking downloaded URLs
def init_json_file():
    if not os.path.exists("downloaded_urls.json"):
        with open("downloaded_urls.json", "w") as f:
            json.dump({"urls": []}, f, indent=2)

def is_downloaded(url):
    with open("downloaded_urls.json", "r") as f:
        data = json.load(f)
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        return url_hash in data["urls"]

def log_download(url):
    url_hash = hashlib.sha256(url.encode()).hexdigest()
    with open("downloaded_urls.json", "r+") as f:
        data = json.load(f)
        if url_hash not in data["urls"]:
            data["urls"].append(url_hash)
            f.seek(0)
            json.dump(data, f, indent=2)
            f.truncate()

# Initialize JSON file
init_json_file()

class VideoURL(BaseModel):
    url: str

@router_download.post("/download-video/")
async def download_video(video: VideoURL):
    try:
        logger.info(f"Starting download for URL: {video.url}")
        if is_downloaded(video.url):
            logger.warning(f"Video already downloaded: {video.url}")
            raise HTTPException(status_code=400, detail="Video already downloaded")

        download_path = f'{settings.vids_dir}/%(title)s.%(ext)s'
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'mp4',
        }

        os.makedirs(settings.vids_dir, exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video.url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        log_download(video.url)
        logger.info(f"Video downloaded successfully: {downloaded_file}")
        return {"message": "Video downloaded successfully"}
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")

@router_download.post("/download-random-reel/")
async def download_random_reel():
    try:
        logger.info("Fetching random Reel from similar accounts")
        time.sleep(random.uniform(5, 15))
        if not ig_client:
            logger.error("Instagram client not initialized")
            raise HTTPException(status_code=500, detail="Instagram client not initialized")

        account = random.choice(settings.similar_accounts)
        logger.info(f"Selected account: {account}")

        medias = ig_client.user_medias(ig_client.user_id_from_username(account), amount=2)
        reels = [media for media in medias if media.media_type == 2 and media.video_url]
        if not reels:
            logger.warning(f"No Reels found for account: {account}")
            raise HTTPException(status_code=404, detail=f"No Reels found for account: {account}")

        available_reels = [reel for reel in reels if not is_downloaded(reel.video_url.encoded_string())]
        if not available_reels:
            logger.warning(f"All available Reels for {account} already downloaded")
            raise HTTPException(status_code=404, detail="No new Reels available for this account")

        chosen_reel = random.choice(available_reels)
        video_url = chosen_reel.video_url.encoded_string()
        logger.info(f"Selected Reel URL: {video_url}")

        download_path = f'{settings.vids_dir}/%(title)s.%(ext)s'
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'mp4',
        }

        os.makedirs(settings.vids_dir, exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        log_download(video_url)
        logger.info(f"Random Reel downloaded successfully: {downloaded_file}")
        return {"message": "Random Reel downloaded successfully", "file_path": downloaded_file}
    except Exception as e:
        logger.error(f"Error downloading random Reel: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading random Reel: {str(e)}")