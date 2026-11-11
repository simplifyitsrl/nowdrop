"""Application settings, read from environment variables (and `.env` if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    base_url: str = ""
    default_currency: str = "USD"
    trigger_words: list[str] = field(default_factory=lambda: ["now", "ahora"])
    database_path: str = "./data/nowdrop.db"

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def upload_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.database_path)), "uploads")


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        paypal_client_id=env("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=env("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_base_url=env("PAYPAL_BASE_URL", "").strip() or "https://api-m.sandbox.paypal.com",
        anthropic_api_key=env("ANTHROPIC_API_KEY", "").strip(),
        llm_model=env("LLM_MODEL", "").strip() or "claude-haiku-5-5",
        base_url=env("BASE_URL", "").strip().rstrip("/"),
        default_currency=(env("DEFAULT_CURRENCY", "").strip() or "USD").upper(),
        trigger_words=[
            w.strip().lower() for w in (env("TRIGGER_WORDS", "") or "now,ahora").split(",") if w.strip()
        ],
        database_path=env("DATABASE_PATH", "").strip() or "./data/nowdrop.db",
    )
