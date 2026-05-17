from pydantic_settings import BaseSettings
from pydantic import model_validator
from functools import lru_cache


class Settings(BaseSettings):
    APP_ENV: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://gym:gym@localhost:5432/gymtracker"
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_DAYS: int = 90
    CORS_ORIGINS: str = "http://localhost:8001,https://gym.cloudwijk.nl"
    FORMSUBMIT_EMAIL: str = "kamstra@gmail.com"
    ANTHROPIC_API_KEY: str = ""  # required for /api/nutrition/parse
    NUTRITION_MODEL: str = "claude-sonnet-4-5-20250929"  # Item #12: override via env
    TELEGRAM_BOT_TOKEN: str = ""  # for /api/nutrition/check-reminders
    TELEGRAM_CHAT_ID_JOOST: str = ""  # Joost's chat-id (hardcoded for MVP)
    REMINDER_SECRET: str = "change-me"  # shared secret for cron endpoint

    @model_validator(mode="after")
    def _prod_secret_guard(self):
        """Item #4: refuse to boot in production with default secrets."""
        if self.APP_ENV == "production":
            if self.SECRET_KEY.startswith("dev-") or "change-in-production" in self.SECRET_KEY:
                raise ValueError("SECRET_KEY must be set in production (default detected)")
            if self.REMINDER_SECRET == "change-me" or not self.REMINDER_SECRET:
                raise ValueError("REMINDER_SECRET must be set in production")
        return self

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
