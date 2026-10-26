"""BOOK-65: Payment's own settings -- only what it actually uses. Not the monolith's
app.core.config.Settings, which carries Booking's database, rate limits, Redis...
Same env var names as before, so docker-compose and .env need no change -- only who
reads them. Same idea as Notification's config.py (BOOK-64 step 5a).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class PaymentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # its own database (§18) -- containers override the host to `db`
    payment_database_url: str = "postgresql+asyncpg://bookit:bookit@localhost:5432/payments"
    # Kafka's host-published listener; containers override to kafka:9092
    kafka_bootstrap_servers: str = "localhost:9094"
    # where Booking's /internal routes are (BOOK-66); containers override to http://app:8000
    booking_base_url: str = "http://localhost:8000"
    # signs Payment's service tokens to Booking (docs/auth/service-token-basics.md)
    service_token_secret: str = "dev-service-secret-change-me"
    # signs the service token (no secret_key: the gateway checks guest JWTs, BOOK-67b)
    jwt_algorithm: str = "HS256"
    # verifies the provider's webhook signature (BOOK-55)
    payment_webhook_secret: str = "dev-webhook-secret-change-me"


@lru_cache
def get_payment_settings() -> PaymentSettings:
    return PaymentSettings()
