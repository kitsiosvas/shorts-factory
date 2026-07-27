from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class PublishResult:
    platform: str
    platform_post_id: str | None
    success: bool
    error: str | None = None


@dataclass
class DraftPost:
    text: str
    media_path: Path | None = None
    reply_to_id: str | None = None
    metadata: dict | None = None


class Publisher(Protocol):
    platform: str

    def publish_video(
        self,
        *,
        video_path: Path,
        title: str,
        description: str,
    ) -> PublishResult: ...

    def publish_draft(self, draft: DraftPost) -> PublishResult: ...
