from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from src.config import get_settings
from src.db import Database
from src.routes.pipeline_router import router as pipeline_router
from src.workers.pipeline import Pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(get_settings().log_path),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _jitter_sleep() -> None:
    pipeline = get_settings().pipeline()
    delay = random.uniform(pipeline.jitter_seconds_min, pipeline.jitter_seconds_max)
    await asyncio.sleep(delay)


async def scheduled_discover() -> None:
    await _jitter_sleep()
    logger.info("Scheduled discover")
    result = await asyncio.to_thread(Pipeline().discover)
    logger.info("Discover result: %s", result)


async def scheduled_process() -> None:
    await _jitter_sleep()
    logger.info("Scheduled process-next")
    result = await asyncio.to_thread(Pipeline().process_next)
    logger.info("Process result: %s", result)


async def scheduled_publish() -> None:
    await _jitter_sleep()
    logger.info("Scheduled publish-next")
    result = await asyncio.to_thread(Pipeline().publish_next)
    logger.info("Publish result: %s", result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Database(settings.database_path)  # ensure schema
    if settings.enable_scheduler:
        pipeline = settings.pipeline()
        scheduler.add_job(
            scheduled_discover,
            "interval",
            minutes=pipeline.discover_interval_minutes,
            id="discover",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_process,
            "interval",
            minutes=pipeline.process_interval_minutes,
            id="process",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_publish,
            "interval",
            minutes=pipeline.publish_interval_minutes,
            id="publish",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "Scheduler started discover=%sm process=%sm publish=%sm",
            pipeline.discover_interval_minutes,
            pipeline.process_interval_minutes,
            pipeline.publish_interval_minutes,
        )
    else:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false)")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(title="Shorts Factory", version="0.1.0", lifespan=lifespan)
app.include_router(pipeline_router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "python": "3.13"}


if __name__ == "__main__":
    import uvicorn

    from src.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )
