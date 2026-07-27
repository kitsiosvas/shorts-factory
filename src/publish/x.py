from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from src.publish.base import DraftPost, PublishResult

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    source: str
    topic_id: str
    text: str
    score: float = 0.0
    external_id: str | None = None


class TrendsProvider(Protocol):
    def get_niche_signals(self, topic_id: str) -> list[Signal]: ...


class EngagementAgent(Protocol):
    def draft_posts(self, signals: list[Signal]) -> list[DraftPost]: ...


class StubTrendsProvider:
    """Placeholder until X API is wired (Phase 3)."""

    def get_niche_signals(self, topic_id: str) -> list[Signal]:
        logger.info("X trends stub called for topic=%s (no API spend)", topic_id)
        return []


class StubEngagementAgent:
    def draft_posts(self, signals: list[Signal]) -> list[DraftPost]:
        drafts: list[DraftPost] = []
        for signal in signals:
            drafts.append(
                DraftPost(
                    text=f"Thoughts on {signal.topic_id}: {signal.text[:200]}",
                    metadata={"signal_id": signal.external_id},
                )
            )
        return drafts


class XPublisher:
    platform = "x"

    def publish_video(
        self,
        *,
        video_path,
        title: str,
        description: str,
    ) -> PublishResult:
        logger.warning("X video publish not implemented (engagement-first design)")
        return PublishResult(
            platform=self.platform,
            platform_post_id=None,
            success=False,
            error="X video publish not implemented",
        )

    def publish_draft(self, draft: DraftPost) -> PublishResult:
        logger.info("X publish stub (no-op): %s", draft.text[:120])
        return PublishResult(
            platform=self.platform,
            platform_post_id=None,
            success=False,
            error="X API not wired yet — stub only",
        )
