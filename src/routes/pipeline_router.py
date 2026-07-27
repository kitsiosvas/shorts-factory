from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.workers.pipeline import Pipeline

router = APIRouter()


class DiscoverRequest(BaseModel):
    topic_id: str | None = Field(default=None, description="Optional single topic id")


class UploadTestRequest(BaseModel):
    video_path: str
    title: str = "Test Short #Shorts"


def _pipeline() -> Pipeline:
    return Pipeline()


@router.post("/discover")
def discover(body: DiscoverRequest | None = None) -> dict:
    body = body or DiscoverRequest()
    result = _pipeline().discover(body.topic_id)
    if result.get("error") and result.get("inserted", 0) == 0:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/process-next")
def process_next() -> dict:
    return _pipeline().process_next()


@router.post("/publish-next")
def publish_next() -> dict:
    return _pipeline().publish_next()


@router.get("/status")
def status() -> dict:
    return _pipeline().status()


@router.post("/upload-test")
def upload_test(body: UploadTestRequest) -> dict:
    """Prove YouTube OAuth + videos.insert with a local vertical MP4."""
    from pathlib import Path

    from src.publish.youtube import upload_test_short

    path = Path(body.video_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    result = upload_test_short(path, title=body.title)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "Upload failed")
    return {
        "youtube_id": result.platform_post_id,
        "url": f"https://youtube.com/shorts/{result.platform_post_id}",
    }
