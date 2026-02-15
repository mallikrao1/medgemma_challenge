from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
load_dotenv(ENV_PATH)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="Enterprise AI Care Platform", alias="ENTERPRISE_APP_NAME")
    app_version: str = Field(default="0.1.0", alias="ENTERPRISE_APP_VERSION")
    api_prefix: str = Field(default="/v1", alias="ENTERPRISE_API_PREFIX")

    database_url: str = Field(
        default=f"sqlite:///{ROOT_DIR / 'data' / 'enterprise.db'}",
        alias="ENTERPRISE_DATABASE_URL",
    )

    jwt_secret_key: str = Field(
        default="replace-this-enterprise-secret-key-in-prod",
        alias="ENTERPRISE_JWT_SECRET_KEY",
    )
    jwt_algorithm: str = Field(default="HS256", alias="ENTERPRISE_JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=60, alias="ENTERPRISE_ACCESS_TOKEN_EXPIRE_MINUTES")

    bootstrap_tenant_name: str = Field(default="Default Tenant", alias="ENTERPRISE_BOOTSTRAP_TENANT_NAME")
    bootstrap_tenant_slug: str = Field(default="default", alias="ENTERPRISE_BOOTSTRAP_TENANT_SLUG")
    bootstrap_admin_email: str = Field(default="admin@enterprise.local", alias="ENTERPRISE_BOOTSTRAP_ADMIN_EMAIL")
    bootstrap_admin_password: str = Field(default="ChangeMe123!", alias="ENTERPRISE_BOOTSTRAP_ADMIN_PASSWORD")
    bootstrap_admin_full_name: str = Field(default="Platform Admin", alias="ENTERPRISE_BOOTSTRAP_ADMIN_FULL_NAME")

    gateway_client_header: str = Field(default="X-Client-ID", alias="ENTERPRISE_GATEWAY_CLIENT_HEADER")
    gateway_rate_limit_per_minute: int = Field(default=120, alias="ENTERPRISE_GATEWAY_RATE_LIMIT_PER_MINUTE")

    worker_poll_seconds: float = Field(default=2.0, alias="ENTERPRISE_WORKER_POLL_SECONDS")
    worker_max_jobs_per_cycle: int = Field(default=10, alias="ENTERPRISE_WORKER_MAX_JOBS_PER_CYCLE")


settings = Settings()
