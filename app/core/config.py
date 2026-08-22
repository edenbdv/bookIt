from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads from .env automatically; defaults below let the app boot even without one.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BookIt"
    environment: str = "development"

    database_url: str = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"
    redis_url: str = "redis://localhost:6379/0"

    # dev-only default -- every real deployment must override this via .env,
    # since anyone who knows it can forge a valid access token for any user
    secret_key: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7


# lru_cache on a zero-argument function = singleton: .env is parsed once,
# every caller gets the same cached Settings instance. See core/README.md.
@lru_cache
def get_settings() -> Settings:
    return Settings()
