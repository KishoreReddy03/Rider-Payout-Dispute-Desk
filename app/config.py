"""Application configuration settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application environment settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 8000
    host: str = "0.0.0.0"
    env: str = "development"

    # PaySwift integration URL
    payswift_base_url: str = "http://localhost:8081"

    # Database configuration
    database_url: str = "postgresql://postgres:postgres@localhost:5432/rider_dispute_db"

    # LLM configuration
    llm_provider: str = "gemini"
    gemini_api_key: str = ""


settings = Settings()
