"""BOOK-64 step 5a: Notification's own settings -- only what it actually uses.

Not the monolith's app.core.config.Settings, which carries Booking's DB URL,
JWT secrets, rate limits... none of Notification's business. Same env var
names as before (and as docker-compose sets), so nothing changes in how it's
configured -- just who reads them.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # its own database -- never Booking's (step 4). Local default is the
    # `notifications` database on the dev Postgres server.
    notification_database_url: str = (
        "postgresql+asyncpg://bookit:bookit@localhost:5432/notifications"
    )
    # Kafka's host-published listener; containers override to kafka:9092
    kafka_bootstrap_servers: str = "localhost:9094"
    # MailHog, a local SMTP test double (worker-decisions.md §8)
    smtp_host: str = "localhost"
    smtp_port: int = 1025


@lru_cache
def get_notification_settings() -> NotificationSettings:
    return NotificationSettings()
