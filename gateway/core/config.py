"""Application configuration via Pydantic Settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://hcc:hcc@localhost:5432/hcc"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False

    model_config = {"env_prefix": "HCC_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
