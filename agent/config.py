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

    # --- Email ---
    # Which service delivers outbound email.
    email_provider: Literal["resend", "sendgrid", "smtp"] = "resend"
    # Sender, e.g. "Sam at Acme <sam@acme.example>". Falls back to RESEND_FROM_EMAIL.
    email_from: str = ""

    # SendGrid
    sendgrid_api_key: str = ""

    # SMTP (Amazon SES, Gmail/Workspace, Postmark, Mailgun, your own server)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # Implicit TLS (usually port 465). Otherwise STARTTLS is used unless
    # SMTP_STARTTLS=false (only for local test servers such as Mailpit).
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True

    # Resend (also receives replies and bounces via webhook)
    resend_api_key: str = ""
    resend_from_email: str = "outbound@yourdomain.com"
    # Svix signing secret (whsec_...) from Resend -> Webhooks.
    resend_webhook_secret: str = ""

    # --- SMS ---
    # Which service sends and receives SMS.
    sms_provider: Literal["africastalking", "twilio"] = "africastalking"

    # Twilio: account SID + auth token (the token also verifies inbound
    # webhooks), and a sender: a phone number or a Messaging Service.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_messaging_service_sid: str = ""

    # Africa's Talking
    at_username: str = "sandbox"
    # Africa's Talking does not sign callbacks; put this token in the callback
    # URL instead: /webhooks/sms/inbound?token=<AT_WEBHOOK_TOKEN>
    at_webhook_token: str = ""
    at_api_key: str = ""
    at_shortcode: str = ""
    at_environment: str = "sandbox"

    # --- CRM ---
    # Which CRM to write contacts, notes and lead status to.
    crm_provider: Literal["hubspot", "pipedrive", "salesforce", "attio"] = "hubspot"

    # Pipedrive: API token + the subdomain of <domain>.pipedrive.com.
    pipedrive_api_token: str = ""
    pipedrive_company_domain: str = ""
    # Optional: key of a text custom field on persons that holds the lead status.
    pipedrive_status_field: str = ""

    # Salesforce: client-credentials OAuth against your My Domain URL.
    salesforce_domain: str = ""  # e.g. https://acme.my.salesforce.com
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_api_version: str = "v62.0"
    # Optional JSON overriding the Lead Status mapping, e.g. {"QUALIFIED": "Working"}.
    salesforce_status_map: str = ""

    # Attio: workspace access token.
    attio_api_key: str = ""
    # Optional: slug of a text attribute on People that holds the lead status.
    attio_status_attribute: str = ""

    # --- CRM: HubSpot ---
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

    # --- Calendar ---
    # Which service books discovery calls.
    calendar_provider: Literal["calcom", "calendly"] = "calcom"

    # Calendly (booking via API needs a paid plan)
    calendly_api_token: str = ""
    calendly_event_type_uri: str = ""  # https://api.calendly.com/event_types/<uuid>
    calendly_scheduling_url: str = ""  # public link, e.g. https://calendly.com/acme/discovery
    # The event type's location kind (zoom_conference, google_conference, ...);
    # empty when the event type has no location. CALENDLY_LOCATION holds the
    # value for kinds that need one (e.g. a phone number).
    calendly_location_kind: str = ""
    calendly_location: str = ""

    # Cal.com
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

    # --- Sending limits (live email only) ---
    daily_send_limit: int = 50
    # ISO date the sending domain started sending (e.g. 2026-11-01). While
    # set, the daily limit ramps 10/day in week one, doubling weekly.
    email_warmup_start: str = ""
    # Pause automatically when the 7-day bounce or spam-complaint rate exceeds these.
    bounce_pause_rate: float = 0.05
    complaint_pause_rate: float = 0.003

    # --- Follow-ups ---
    # Days of silence before each follow-up, counted from the previous email.
    # "3,7" = first follow-up after 3 days, second 7 days later, then stop.
    # Empty disables follow-ups.
    follow_up_days: str = "3,7"
    # How often the app checks for due follow-ups. 0 = never (use
    # POST /api/follow-ups/run from a cron instead).
    follow_up_interval_minutes: int = 60

    @property
    def follow_up_schedule(self) -> list[int]:
        return [int(d) for d in self.follow_up_days.split(",") if d.strip()]

    # --- Review ---
    # When true, every LLM-drafted email waits in the review queue
    # (/review, /api/review) until a person approves it.
    require_approval: bool = True

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
    def sender_email(self) -> str:
        return self.email_from or self.resend_from_email

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def active_model(self) -> str:
        """Return the appropriate model based on environment."""
        return self.dev_model if self.is_dev else self.eval_model


# Singleton settings instance
settings = Settings()
