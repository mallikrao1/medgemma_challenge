from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="MedGemma Discharge Copilot", alias="APP_NAME")
    app_version: str = Field(default="1.0.0", alias="APP_VERSION")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8010, alias="API_PORT")

    model_backend: str = Field(default="mock", alias="MODEL_BACKEND")
    medgemma_model_id: str = Field(default="google/medgemma-4b-it", alias="MEDGEMMA_MODEL_ID")
    max_input_chars: int = Field(default=12000, alias="MAX_INPUT_CHARS")
    max_new_tokens: int = Field(default=700, alias="MAX_NEW_TOKENS")
    temperature: float = Field(default=0.1, alias="TEMPERATURE")
    timeout_seconds: int = Field(default=120, alias="TIMEOUT_SECONDS")

    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="", alias="OPENAI_MODEL")

    default_language: str = Field(default="english", alias="DEFAULT_LANGUAGE")


settings = Settings()

