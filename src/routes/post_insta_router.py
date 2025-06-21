import gc
from fastapi import HTTPException, APIRouter
from pydantic import BaseModel
import os
import random
import logging
import time
from datetime import datetime
from config import settings

logger = logging.getLogger(__name__)

router_upload = APIRouter()

def force_delete_file(file_path, retries=5, delay=1):
    gc.collect()
    for attempt in range(retries):
        try:
            os.remove(file_path)
            logger.info(f"Deleted file: {file_path}")
            return
        except PermissionError as e:
            logger.warning(f"Attempt {attempt + 1}: File locked, retrying in {delay}s")
            time.sleep(delay)
            gc.collect()
    logger.error(f"Failed to delete file: {file_path}")
    raise HTTPException(status_code=500, detail=f"Failed to delete file: {file_path}")

class VideoPost(BaseModel):
    caption: str = "First account ever to reach 1M likes without followers!!!"

@router_upload.post("/post-video/")
async def post_random_video(video: VideoPost):
    try:
        if not settings.ig_client:
            logger.error("Instagram client not initialized")
            raise HTTPException(status_code=500, detail="Instagram client not initialized")
        
        logger.info(f"Fetching random video from directory: {settings.vids_dir}")
        time.sleep(random.uniform(5, 15))

        video_files = [f for f in os.listdir(settings.vids_dir) if f.lower().endswith('.mp4')]
        if not video_files:
            logger.warning("No video files found in directory")
            raise HTTPException(status_code=404, detail="No video files available to post")

        chosen_video = random.choice(video_files)
        full_path = os.path.join(settings.vids_dir, chosen_video)
        logger.info(f"Selected video for upload: {full_path}")

        if not os.path.exists(full_path):
            logger.error(f"Selected video does not exist: {full_path}")
            raise HTTPException(status_code=404, detail="Selected video file not found")

        logger.info(f"Uploading Reel with caption: '{video.caption}'")
        start_time = datetime.now()
        settings.ig_client.clip_upload(
            path=full_path,
            caption=video.caption,
            extra_data={"is_reel": True}
        )
        upload_time = datetime.now() - start_time
        logger.info(f"Video posted successfully in {upload_time.total_seconds():.2f} seconds")

        logger.info(f"Attempting to delete video: {full_path}")
        force_delete_file(full_path)

        return {"message": f"Video '{chosen_video}' posted and deleted successfully"}
    except Exception as e:
        logger.error(f"Error posting video: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error posting video: {str(e)}")