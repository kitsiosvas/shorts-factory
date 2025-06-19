import logging
from pydantic_settings import BaseSettings
from typing import List
import os
from dotenv import load_dotenv
import os
import json
from instagrapi import Client
logger = logging.getLogger(__name__)

load_dotenv(override=True)

class Settings(BaseSettings):
    ig_username: str
    ig_password: str
    vids_dir: str = None
    similar_accounts: List[str] = None


settings = Settings()


ig_client = None

def init_client():
    global ig_client
    try:
        ig_client = Client()
        if os.path.exists("settings.json"):
            with open("settings.json", "r") as f:
                old_session = json.load(f)
            ig_client.set_settings({})
            ig_client.set_uuids(old_session["uuids"])
        ig_client.login(settings.ig_username, settings.ig_password)
        with open("settings.json", "w") as f:
            json.dump(ig_client.get_settings(), f, indent=2)
        logger.info("Instagram client initialized")
    except Exception as e:
        logger.error(f"Client initialization failed: {str(e)}")
        raise