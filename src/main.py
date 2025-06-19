import logging
from fastapi import FastAPI
import uvicorn
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.on_event("startup")
async def startup_event():
    pass

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