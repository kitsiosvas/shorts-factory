import gc
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp
import os
import random
from instagrapi import Client
import uvicorn
import logging
from datetime import datetime
from dotenv import load_dotenv
import time

load_dotenv()
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")


def force_delete_file(file_path, retries=5, delay=1):
    gc.collect()
    for attempt in range(retries):
        try:
            os.remove(file_path)
            logger.info(f"Deleted file: {file_path}")
            return
        except PermissionError as e:
            logger.warning(f"Attempt {attempt+1}: File locked, retrying in {delay}s...")
            time.sleep(delay)
            gc.collect()
    logger.error(f"Could not delete file: {file_path}")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

class VideoURL(BaseModel):
    url: str

class VideoPost(BaseModel):
    caption: str = ""

@app.post("/download-video/")
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
        return {"message": "Video downloaded successfully", "file_path": downloaded_file}
    except Exception as e:
        logger.error(f"Error downloading video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")

@app.post("/post-video/")
async def post_random_video(video: VideoPost):
    try:
        vids_dir = 'C:/Users/User/Desktop/insta_vid_downloader/vids'
        logger.info("Fetching random video from vids directory")

        video_files = [f for f in os.listdir(vids_dir) if f.lower().endswith('.mp4')]
        if not video_files:
            logger.warning("No video files found to post")
            raise HTTPException(status_code=404, detail="No video files available to post")

        chosen_video = random.choice(video_files)
        full_path = os.path.join(vids_dir, chosen_video)
        logger.info(f"Selected video for upload: {chosen_video}")

        logger.info("Attempting Instagram login")
        ig_client = Client()
        ig_client.login(os.getenv("IG_USERNAME"), os.getenv("IG_PASSWORD"))
        logger.info("Instagram login successful")

        logger.info(f"Uploading Reel with caption: {video.caption}")
        start_time = datetime.now()
        clip  = ig_client.clip_upload(
            path=full_path,
            caption=video.caption,
            extra_data={"is_reel": True}
        )
        upload_time = datetime.now() - start_time
        logger.info(f"Video posted successfully in {upload_time.total_seconds()} seconds")

        ig_client.logout()
        del ig_client
        gc.collect()
        force_delete_file(full_path)


        return {"message": f"Video '{chosen_video}' posted and deleted successfully"}
    except Exception as e:
        logger.error(f"Error posting video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error posting video: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, port=8000)
