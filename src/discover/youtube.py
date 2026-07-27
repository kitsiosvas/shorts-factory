from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.config import TopicConfig
from src.publish.youtube import get_youtube_service

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredVideo:
    youtube_video_id: str
    title: str
    channel_title: str
    url: str
    duration_sec: int
    view_count: int


def _parse_duration(iso: str) -> int:
    """Parse ISO-8601 duration like PT1H2M3S into seconds."""
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        iso or "",
    )
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def discover_for_topic(topic: TopicConfig, *, api_key: str | None = None) -> list[DiscoveredVideo]:
    """Search YouTube for topic candidates. Uses OAuth client if no API key."""
    youtube = get_youtube_service(api_key=api_key)
    results: list[DiscoveredVideo] = []
    seen: set[str] = set()

    for query in topic.search_queries:
        logger.info("Discover query=%r topic=%s", query, topic.id)
        search = (
            youtube.search()
            .list(
                q=query,
                part="id,snippet",
                type="video",
                order="viewCount",
                maxResults=topic.max_results_per_query,
                videoDuration="medium",
                relevanceLanguage="en",
            )
            .execute()
        )
        video_ids = [
            item["id"]["videoId"]
            for item in search.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        if not video_ids:
            continue

        details = (
            youtube.videos()
            .list(part="contentDetails,statistics,snippet", id=",".join(video_ids))
            .execute()
        )
        for item in details.get("items", []):
            video_id = item["id"]
            if video_id in seen:
                continue
            title = item["snippet"]["title"]
            lowered = title.lower()
            if any(ex.lower() in lowered for ex in topic.exclude_keywords):
                continue
            duration = _parse_duration(item["contentDetails"]["duration"])
            views = int(item.get("statistics", {}).get("viewCount", 0))
            if duration < topic.min_duration_sec or duration > topic.max_duration_sec:
                continue
            if views < topic.min_view_count:
                continue
            seen.add(video_id)
            results.append(
                DiscoveredVideo(
                    youtube_video_id=video_id,
                    title=title,
                    channel_title=item["snippet"].get("channelTitle", ""),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    duration_sec=duration,
                    view_count=views,
                )
            )

    results.sort(key=lambda v: v.view_count, reverse=True)
    logger.info("Discovered %s candidates for topic=%s", len(results), topic.id)
    return results
