from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads from .env automatically; defaults below let the app boot even without one.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BookIt"
    environment: str = "development"

    database_url: str = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"
    # (BOOK-65: Payment's database URL, webhook secret and Booking's address live in
    # Payment's own settings, app/payment/config.py -- Booking doesn't need them)
    # (BOOK-64: Notification's database URL lives in its own settings,
    # services/notification -- Booking doesn't need to know it)
    redis_url: str = "redis://localhost:6379/0"

    # BOOK-44: Celery's broker. RabbitMQ, not Redis -- see
    # docs/jobs/worker-decisions.md section 2 for why this project uses Celery
    # on RabbitMQ instead of arq on Redis.
    celery_broker_url: str = "amqp://guest:guest@localhost:5672//"

    # BOOK-45: MailHog, a local SMTP test double, not a real mail server --
    # see docs/jobs/email-basics.md and docs/jobs/worker-decisions.md section
    # 8. Only `worker` ever reads this (see docker-compose.yml's override to
    # the `mailhog` hostname there) -- `app` never sends mail directly.
    smtp_host: str = "localhost"
    smtp_port: int = 1025

    # BOOK-50: the outbox relay's Kafka producer. `localhost:9094` is
    # Kafka's host-published listener (docker-compose.yml) -- the worker
    # container overrides this to `kafka:9092`, same split as smtp_host above.
    kafka_bootstrap_servers: str = "localhost:9094"

    # dev-only default -- every real deployment must override this via .env,
    # since anyone who knows it can forge a valid access token for any user
    secret_key: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # BOOK-66: signs/verifies service tokens (Payment -> Booking's /internal
    # routes) -- its own secret, never secret_key (docs/auth/service-token-basics.md §4).
    # dev-only default, same as secret_key.
    service_token_secret: str = "dev-service-secret-change-me"

    # BOOK-78: how long a just-spent refresh token is still accepted, answering
    # with the token that replaced it instead of counting as reuse. This is the
    # two-open-tabs case -- each tab has its own interceptor and neither knows
    # the other is refreshing, which no amount of client-side single-flight
    # fixes. Keep it seconds: it's also the window a genuinely stolen token
    # stays replayable in. See docs/auth/refresh-token-rotation-basics.md.
    refresh_reuse_grace_seconds: int = 10

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

    # BOOK-42: rate limiting. (limit, window_seconds) pairs -- see
    # docs/cache/rate-limiting-basics.md for the algorithm/key/fail-mode
    # decision behind each group. Do not loosen these per-environment: doing
    # so means staging never exercises the scenario the limiter exists to
    # catch, same warning as rating_smoothing_m above.

    # login -- sliding window log, checked per-IP AND per-email
    rate_limit_login_ip_limit: int = 10
    rate_limit_login_ip_window_seconds: int = 60
    rate_limit_login_email_limit: int = 5
    rate_limit_login_email_window_seconds: int = 60

    # signup, refresh -- fixed window, per-IP
    rate_limit_auth_ip_limit: int = 20
    rate_limit_auth_ip_window_seconds: int = 60

    # public property reads -- fixed window, per-IP, loose
    rate_limit_properties_ip_limit: int = 100
    rate_limit_properties_ip_window_seconds: int = 60

    # authenticated routes -- fixed window, per-user primary + per-IP floor
    rate_limit_user_limit: int = 100
    rate_limit_user_window_seconds: int = 60
    rate_limit_user_ip_floor_limit: int = 300
    rate_limit_user_ip_floor_window_seconds: int = 60


# lru_cache on a zero-argument function = singleton: .env is parsed once,
# every caller gets the same cached Settings instance. See core/README.md.
@lru_cache
def get_settings() -> Settings:
    return Settings()
