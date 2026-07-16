import logging
from pydantic_settings import BaseSettings
from typing import List, Union
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
    ig_client: Union[Client, None] = None
    default_caption: str = "Type of memes my unemployed friend sends me..."

    def init_client(self):
        try:
            self.ig_client = Client()
            if os.path.exists("settings.json"):
                with open("settings.json", "r") as f:
                    old_session = json.load(f)
                self.ig_client.set_settings({})
                self.ig_client.set_uuids(old_session["uuids"])
            self.ig_client.login(self.ig_username, self.ig_password)
            with open("settings.json", "w") as f:
                json.dump(self.ig_client.get_settings(), f, indent=2)
            logger.info("Instagram client initialized")
        except Exception as e:
            logger.error(f"Client initialization failed: {str(e)}")
            raise


settings = Settings()
settings.init_client()