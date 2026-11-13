from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # Storage
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'rentagent.db'}"
    checkpoint_url: str = f"sqlite:///{BASE_DIR / 'data' / 'checkpoints.db'}"
    redis_url: str | None = None  # enables Celery + cross-process event fan-out

    # LLM
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_enable_fallbacks: bool = True  # server-side refusal fallbacks

    # Discovery
    enable_web_search_discovery: bool = True
    discovery_interval_minutes: int = 120
    http_user_agent: str = "RentAgentBot/0.1 (+tenant's personal rental assistant)"
    # Domains whose pages may be fetched (robots.txt is checked in addition).
    # Add a domain only after reviewing its terms of use.
    fetch_allowed_domains: str = ""
    playwright_enabled: bool = False

    # Email (SMTP send + IMAP receive) — the user's own or a dedicated mailbox
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: str | None = None
    imap_alert_folder: str = "INBOX"
    imap_reply_folder: str = "INBOX"

    # WhatsApp Cloud API (official)
    whatsapp_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: str = "rentagent-verify"
    whatsapp_first_contact_template: str | None = None  # approved template name

    # The tenant (used in message signatures + notifications)
    user_name: str = "the tenant"
    user_email: str | None = None
    user_whatsapp: str | None = None  # user's own number, for notifications (opted in)

    # Safety rails
    daily_outreach_cap: int = 10
    contact_cooldown_hours: int = 48
    min_score_to_contact: float = 60.0
    auto_followups: bool = True
    auto_visit_slots: bool = True

    cors_origins: str = "http://localhost:3000"

    @property
    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.fetch_allowed_domains.split(",") if d.strip()}


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    (BASE_DIR / "data").mkdir(exist_ok=True)
    return s
