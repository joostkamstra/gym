from pydantic_settings import BaseSettings
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
    TELEGRAM_BOT_TOKEN: str = ""  # for /api/nutrition/check-reminders
    TELEGRAM_CHAT_ID_JOOST: str = ""  # Joost's chat-id (hardcoded for MVP)
    REMINDER_SECRET: str = "change-me"  # shared secret for cron endpoint

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
