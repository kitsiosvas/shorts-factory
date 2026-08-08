from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.config import get_settings
from src.publish.base import DraftPost, PublishResult

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_credentials() -> Credentials:
    settings = get_settings()
    token_path = settings.youtube_token_path
    secrets_path = settings.youtube_client_secrets
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:  # noqa: BLE001 — invalid_grant etc. → re-auth
                logger.warning("YouTube token refresh failed (%s); starting OAuth again", exc)
                creds = None
                try:
                    token_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if not refreshed:
            if not secrets_path.exists():
                raise FileNotFoundError(
                    f"Missing YouTube OAuth client secrets at {secrets_path}. "
                    "Create a Desktop OAuth client in Google Cloud Console, enable "
                    "YouTube Data API v3, and save client_secrets.json there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_youtube_service(*, api_key: str | None = None):
    if api_key:
        return build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    creds = get_credentials()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


class YouTubePublisher:
    platform = "youtube"

    def publish_video(
        self,
        *,
        video_path: Path,
        title: str,
        description: str,
    ) -> PublishResult:
        settings = get_settings()
        if not video_path.exists():
            return PublishResult(
                platform=self.platform,
                platform_post_id=None,
                success=False,
                error=f"Video not found: {video_path}",
            )
        try:
            youtube = get_youtube_service()
            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description,
                    "categoryId": "22",
                    "tags": ["Shorts"],
                },
                "status": {
                    "privacyStatus": settings.privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            }
            media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
            logger.info("Uploading Short title=%r privacy=%s", title, settings.privacy_status)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info("Upload progress %.0f%%", status.progress() * 100)
            video_id = response.get("id")
            return PublishResult(
                platform=self.platform,
                platform_post_id=video_id,
                success=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("YouTube upload failed")
            return PublishResult(
                platform=self.platform,
                platform_post_id=None,
                success=False,
                error=str(exc),
            )

    def publish_draft(self, draft: DraftPost) -> PublishResult:
        if not draft.media_path:
            return PublishResult(
                platform=self.platform,
                platform_post_id=None,
                success=False,
                error="YouTube publish_draft requires media_path",
            )
        meta = draft.metadata or {}
        return self.publish_video(
            video_path=draft.media_path,
            title=meta.get("title") or draft.text[:100],
            description=meta.get("description") or draft.text,
        )


def upload_test_short(video_path: Path, title: str = "Test Short #Shorts") -> PublishResult:
    """Manual proof helper for OAuth + videos.insert."""
    return YouTubePublisher().publish_video(
        video_path=video_path,
        title=title,
        description="OAuth upload proof from Shorts Factory\n#Shorts",
    )
