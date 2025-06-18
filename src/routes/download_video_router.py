from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
import yt_dlp
import os
import logging

logger = logging.getLogger(__name__)

router_download = APIRouter()

class VideoURL(BaseModel):
    url: str


@router_download.post("/download-video/")
async def download_video(video: VideoURL):
    try:
        logger.info(f"Starting download for URL: {video.url}")
        download_path = 'C:/Users/User/Desktop/insta_vid_downloader/vids/%(title)s.%(ext)s'
        ydl_opts = {
            'outtmpl': download_path,
            'format': 'mp4',
        }

        os.makedirs('C:/Users/User/Desktop/insta_vid_downloader/vids', exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video.url, download=True)
            downloaded_file = ydl.prepare_filename(info)

        logger.info(f"Video downloaded successfully: {downloaded_file}")
        return {"message": "Video downloaded successfully"}
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")