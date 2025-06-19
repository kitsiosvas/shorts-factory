import logging
from fastapi import FastAPI
import uvicorn
from config import settings
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx
import asyncio
import random

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = FastAPI()

async def download_task():
    logger.info("Triggering random Reel download")
    try:
        logger.info("Running scheduled download task")
        await asyncio.sleep(random.uniform(30, 300))  # Random delay
        async with httpx.AsyncClient() as client:
            response = await client.post("http://localhost:8000/download-random-reel/")
            logger.info(f"Download response: {response.json()}")
    except Exception as e:
        logger.error(f"Download task failed: {str(e)}")

@app.on_event("startup")
async def startup_event():
    settings.init_client()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(download_task, "interval", minutes=60)
    scheduler.start()
    logger.info("Download scheduler started")

@app.on_event("shutdown")
async def shutdown_event():
    if settings.ig_client:
        settings.ig_client.logout()
        logger.info("Instagram client logged out")

from routes.download_video_router import router_download
from routes.post_insta_router import router_upload

app.include_router(router_download)
app.include_router(router_upload)

if __name__ == "__main__":
    uvicorn.run(app, port=8000)