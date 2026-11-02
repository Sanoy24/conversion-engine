"""
Centralized configuration for the Conversion Engine.
All settings are loaded from environment variables via pydantic-settings.
"""

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
    # Svix signing secret (whsec_...) from Resend -> Webhooks.
    resend_webhook_secret: str = ""

    # --- SMS (Africa's Talking) ---
    at_username: str = "sandbox"
    # Africa's Talking does not sign callbacks; put this token in the callback
    # URL instead: /webhooks/sms/inbound?token=<AT_WEBHOOK_TOKEN>
    at_webhook_token: str = ""
    at_api_key: str = ""
    at_shortcode: str = ""
    at_environment: str = "sandbox"

    # --- CRM (HubSpot) ---
    hubspot_access_token: str = ""
    hubspot_portal_id: str = ""
    # When True, route CRM calls through the official HubSpot MCP server
    # (@hubspot/mcp-server, launched Feb 2026) instead of the direct REST API.
    # Requires Node.js / npx on the host. If MCP setup fails at runtime,
    # the factory falls back to the direct-API client with a warning.
    use_hubspot_mcp: bool = False
    # npx command to launch the HubSpot MCP server. Override if you've pinned
    # a specific version or installed it globally.
    hubspot_mcp_command: str = "npx"
    hubspot_mcp_args: str = "-y,@hubspot/mcp-server"

    # --- Calendar (Cal.com) ---
    calcom_api_key: str = ""
    # Secret set on the Cal.com webhook; verifies X-Cal-Signature-256.
    calcom_webhook_secret: str = ""
    calcom_base_url: str = "http://localhost:3000"
    calcom_event_type_id: int = 1
    # SDR who co-hosts every discovery call — added as a guest on the Cal.com
    # booking so both the prospect and the SDR receive a calendar invite.
    # Set via SDR_EMAIL in .env. If empty, the booking is created with only
    # the prospect as attendee (functional but fails the "both attendees" check).
    sdr_email: str = ""

    # --- Observability (Langfuse) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # .env may use LANGFUSE_BASE_URL instead of LANGFUSE_HOST — support both
    langfuse_base_url: str = ""

    @property
    def langfuse_endpoint(self) -> str:
        return self.langfuse_base_url or self.langfuse_host

    # --- Application ---
    app_env: Literal["development", "production"] = "development"
    # Required on /api/* as `X-API-Key` or `Authorization: Bearer`. When unset,
    # the API is open in development and closed in production.
    api_key: str = ""
    app_port: int = 8000
    app_host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- Conversation storage ---
    # SQLite file that keeps conversation threads and the suppression list
    # across restarts. Set to an empty string to keep both in memory only.
    conversation_db_path: str = "./data/conversations.db"

    # --- Unsubscribe ---
    # Public URL this app is reachable at; unsubscribe links point here.
    # Both values are required before live email is allowed.
    public_base_url: str = ""
    # Secret used to sign unsubscribe links. Keep it stable: changing it
    # invalidates every link already sent.
    unsubscribe_secret: str = ""

    # --- Kill Switch ---
    live_outbound_enabled: bool = False

    # --- SCAP: Signal-Confidence-Aware Phrasing ---
    # Master toggle. When False, the drafter behavior is identical to the
    # plain (un-transformed) baseline; when True, the SCAP pre-prompt transform is applied
    # before every draft_email call. Sub-flags pick individual rules for
    # ablation studies.
    enable_scap: bool = True
    scap_strip_low: bool = True
    scap_filter_gap_low: bool = True
    scap_soften_medium: bool = True

    # --- Playbook ---
    # Directory containing playbook.yaml: the company identity, voice, ICP
    # segments, and knowledge files the agent sells with. See playbooks/example/.
    playbook_dir: str = "./playbooks/example"

    # --- Public Data Paths (populate with `python -m scripts.fetch_data`) ---
    crunchbase_data_path: str = "./data/crunchbase_odm_sample.json"
    layoffs_data_path: str = "./data/layoffs.csv"
    job_posts_snapshot_path: str = "./data/job_posts_snapshot.json"
    allow_synthetic_job_posts_snapshot: bool = False
    # Max distinct companies live-crawled for job posts per process (politeness cap).
    live_crawl_cap: int = 200

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def active_model(self) -> str:
        """Return the appropriate model based on environment."""
        return self.dev_model if self.is_dev else self.eval_model


# Singleton settings instance
settings = Settings()
