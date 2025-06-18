import logging
from fastapi import FastAPI
import uvicorn
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)

app = FastAPI()

from routes.download_video_router import router_download
from routes.post_insta_router import router_upload

app.include_router(router_download)
app.include_router(router_upload)

if __name__ == "__main__":
    uvicorn.run(app, port=8000)