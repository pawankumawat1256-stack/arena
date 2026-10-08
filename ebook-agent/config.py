"""Application settings. Every value comes from environment variables or the .env file."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    app_env: str = "development"
    log_level: str = "INFO"
    app_secret_key: str = ""  # signs unsubscribe links; use 32+ random characters
    api_key: str = ""  # sent as X-API-Key on protected endpoints
    public_base_url: str = "http://localhost:8000"  # public HTTPS address in production
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = "sqlite:///./data/ebook_agent.db"
    dashboard_password: str = ""
    dashboard_api_url: str = "http://localhost:8000"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_temperature: float = 0.4

    # Notion
    notion_api_key: str = ""
    notion_parent_page_id: str = ""

    # Google: Gmail and Drive share one OAuth client and one token file
    google_client_secrets_file: Path = PROJECT_ROOT / "secrets" / "google_client_secret.json"
    google_token_file: Path = PROJECT_ROOT / "secrets" / "google_token.json"
    google_drive_folder_id: str = ""
    business_postal_address: str = ""  # marketing email is blocked until this is set

    # Gumroad
    gumroad_access_token: str = ""  # OAuth access token with the view_sales scope
    gumroad_webhook_token: str = ""  # appended to the webhook URL as ?token=

    # Instagram Graph API (Facebook Login path)
    instagram_access_token: str = ""
    instagram_user_id: str = ""
    instagram_graph_base_url: str = "https://graph.facebook.com"
    graph_api_version: str = "v21.0"
    instagram_daily_post_limit: int = 100  # used only if the live quota check cannot be read

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Business rules
    kdp_list_price_usd: float = 9.99
    kdp_file_size_mb: float = 3.0
    toolkit_price_usd: float = 19.00
    bundle_price_usd: float = 24.00
    amazon_in_withholding_rate: float = 0.10
    buyer_link_ttl_hours: int = 72
    buyer_max_downloads: int = 5
    marketing_posts_per_week: int = 3
    email_batch_limit: int = 50

    # Scheduler
    scheduler_timezone: str = "Asia/Kolkata"
    analytics_cron_hour: int = 7
    marketing_cron_weekday: str = "mon"
    marketing_cron_hour: int = 9

    def ensure_directories(self) -> None:
        for name in ("ebooks", "covers", "manuscripts"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)


def utcnow() -> datetime:
    """Naive UTC timestamp. Every datetime stored in the database is naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


settings = Settings()
