from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class TopicConfig(BaseModel):
    id: str
    enabled: bool = True
    display_name: str
    search_queries: list[str]
    exclude_keywords: list[str] = Field(default_factory=list)
    min_duration_sec: int = 180
    max_duration_sec: int = 1200
    min_view_count: int = 50_000
    max_results_per_query: int = 8
    require_captions: bool = True
    creative_commons_only: bool = False


class PipelineConfig(BaseModel):
    discover_interval_minutes: int = 720
    process_interval_minutes: int = 60
    publish_interval_minutes: int = 240
    max_publishes_per_day: int = 3
    clip_max_seconds: int = 45
    clip_min_seconds: int = 18
    jitter_seconds_min: int = 30
    jitter_seconds_max: int = 180
    youtube_daily_quota_budget: int = 8000
    upload_quota_cost: int = 1600
    whisper_model: str = "tiny"
    use_whisper: bool = False
    # Highlight intelligence
    llm_provider: Literal["none", "gemini", "ollama"] = "gemini"
    gemini_model: str = "gemini-flash-lite-latest"
    ollama_model: str = "llama3.2"
    max_llm_highlights_per_day: int = 5
    use_youtube_captions: bool = True
    min_clip_score: int = 70
    enable_verifier: bool = True
    verifier_soft_floor: int = 55
    burn_captions: bool = True
    snap_to_sentences: bool = True
    snap_whisper_model: str = "base"
    max_clips_per_source: int = 10
    clip_min_gap_sec: int = 30
    picker_chunk_sec: int = 900


class TopicsFile(BaseModel):
    topics: list[TopicConfig]
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    topics_path: Path = ROOT / "topics.yaml"
    database_path: Path = ROOT / "data" / "factory.db"
    media_raw_dir: Path = ROOT / "media" / "raw"
    media_ready_dir: Path = ROOT / "media" / "ready"
    media_archive_dir: Path = ROOT / "media" / "archive"
    captions_dir: Path = ROOT / "media" / "captions"
    youtube_client_secrets: Path = ROOT / "secrets" / "client_secrets.json"
    youtube_token_path: Path = ROOT / "secrets" / "token.json"
    enable_scheduler: bool = True
    privacy_status: str = "private"
    app_host: str = "127.0.0.1"
    app_port: int = 8010
    log_path: Path = ROOT / "app.log"

    # LLM keys / endpoints (choose provider in topics.yaml)
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434"

    def ensure_dirs(self) -> None:
        for path in (
            self.media_raw_dir,
            self.media_ready_dir,
            self.media_archive_dir,
            self.captions_dir,
            self.database_path.parent,
            self.youtube_token_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def load_topics_file(self) -> TopicsFile:
        raw: dict[str, Any] = yaml.safe_load(self.topics_path.read_text(encoding="utf-8"))
        return TopicsFile.model_validate(raw)

    def enabled_topics(self) -> list[TopicConfig]:
        return [t for t in self.load_topics_file().topics if t.enabled]

    def pipeline(self) -> PipelineConfig:
        return self.load_topics_file().pipeline


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
