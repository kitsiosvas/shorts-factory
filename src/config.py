from pydantic_settings import BaseSettings
from typing import List
import os
from dotenv import load_dotenv


load_dotenv(override=True)

class Settings(BaseSettings):
    IG_USERNAME: str
    IG_PASSWORD: str
    VIDS_DIR: str = None
    SIMILAR_ACCOUNTS: List[str] = None


settings = Settings()