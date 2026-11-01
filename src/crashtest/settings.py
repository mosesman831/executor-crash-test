from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: SecretStr
    openai_executor_api_key: SecretStr
    crashtest_agent_model: str = "gpt-6-astra"
    crashtest_root: Path = Path("/srv/crashtest")
    crashtest_codex_bin: str = "codex"
    crashtest_decisions: Literal["on", "off"] = "on"
    crashtest_mode: Literal["live", "replay", "tamper"] = "live"
    crashtest_decisions_model: str = "gpt-6-luna"


def load_settings() -> Settings:
    return Settings()
