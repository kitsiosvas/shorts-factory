from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
import yt_dlp
import os
import logging
from instagrapi import Client
import random
from config import settings

logger = logging.getLogger(__name__)

router_download = APIRouter()

class VideoURL(BaseModel):
    url: str

@router_download.post("/download-video/")
async def download_video(video: VideoURL):
    try:
        logger.info(f"Starting download for URL: {video.url}")
        download_path = f'{settings.VIDS_DIR}/%(title)s.%(ext)s'
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'mp4',
        }

        os.makedirs(settings.VIDS_DIR, exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video.url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        logger.info(f"Video downloaded successfully: {downloaded_file}")
        return {"message": "Video downloaded successfully"}
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")

@router_download.post("/download-random-reel/")
async def download_random_reel():
    try:
        logger.info("Fetching random Reel from similar accounts")
        ig_client = Client()
        ig_client.login(settings.IG_USERNAME, settings.IG_PASSWORD)
        logger.info("Instagram login successful")

        account = random.choice(settings.SIMILAR_ACCOUNTS)
        logger.info(f"Selected account: {account}")

        medias = ig_client.user_medias(ig_client.user_id_from_username(account), amount=5)
        reels = [media for media in medias if media.media_type == 2 and media.video_url]
        if not reels:
            logger.warning(f"No Reels found for account: {account}")
            raise HTTPException(status_code=404, detail=f"No Reels found for account: {account}")

        chosen_reel = random.choice(reels)
        video_url = chosen_reel.video_url.encoded_string()
        logger.info(f"Selected Random URL")

        download_path = f'{settings.VIDS_DIR}/%(title)s.%(ext)s'
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'mp4',
        }

        os.makedirs(settings.VIDS_DIR, exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        logger.info(f"Random Reel downloaded successfully: {downloaded_file}")
        ig_client.logout()
        return {"message": "Random Reel downloaded successfully", "file_path": downloaded_file}
    except Exception as e:
        logger.error(f"Error downloading random Reel: {str(e)}")
        if 'ig_client' in locals():
            ig_client.logout()
        raise HTTPException(status_code=500, detail=f"Error downloading random Reel: {str(e)}")