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

    # BOOK-77 ranking tunable: roughly how many reviews a property needs
    # before its own average outweighs the global mean in the "recommended"
    # sort. Raise it to make ranking more conservative (harder for a listing
    # with few reviews to climb), lower it to make it more responsive.
    # Environment-overridable so it can be tuned without a redeploy -- but
    # note that overriding it per-environment means staging and production
    # will order listings differently.
    rating_smoothing_m: int = 5

    # BOOK-40: TTL for the cached global mean rating (get_global_mean_rating).
    # A ranking-smoothing constant that changes slowly -- a few minutes of
    # staleness is a non-issue, per cache-aside-basics.md#1.
    global_mean_rating_cache_ttl_seconds: int = 300


# lru_cache on a zero-argument function = singleton: .env is parsed once,
# every caller gets the same cached Settings instance. See core/README.md.
@lru_cache
def get_settings() -> Settings:
    return Settings()
