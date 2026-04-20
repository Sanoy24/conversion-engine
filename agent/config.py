"""
Centralized configuration for the Conversion Engine.
All settings are loaded from environment variables via pydantic-settings.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    dev_model: str = "deepseek/deepseek-chat-v3-0324"
    eval_model: str = "anthropic/claude-sonnet-4-20250514"

    # --- Email (Resend) ---
    resend_api_key: str = ""
    resend_from_email: str = "outbound@yourdomain.com"
    resend_webhook_secret: str = ""

    # --- SMS (Africa's Talking) ---
    at_username: str = "sandbox"
    at_api_key: str = ""
    at_shortcode: str = ""
    at_environment: str = "sandbox"

    # --- CRM (HubSpot) ---
    hubspot_access_token: str = ""
    hubspot_portal_id: str = ""

    # --- Calendar (Cal.com) ---
    calcom_api_key: str = ""
    calcom_base_url: str = "http://localhost:3000"
    calcom_event_type_id: int = 1

    # --- Observability (Langfuse) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Application ---
    app_env: Literal["development", "production"] = "development"
    app_port: int = 8000
    app_host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- Kill Switch ---
    live_outbound_enabled: bool = False

    # --- Seed Data Paths ---
    seeds_dir: str = "./tenacious-seeds-placeholder/seeds_placeholder"
    crunchbase_data_path: str = "./data/crunchbase_odm_sample.json"
    layoffs_data_path: str = "./data/layoffs.csv"
    job_posts_snapshot_path: str = "./data/job_posts_snapshot.json"

    @property
    def seeds_path(self) -> Path:
        return Path(self.seeds_dir)

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def active_model(self) -> str:
        """Return the appropriate model based on environment."""
        return self.dev_model if self.is_dev else self.eval_model


# Singleton settings instance
settings = Settings()
