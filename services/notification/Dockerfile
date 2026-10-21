# BOOK-64 step 5b: Notification's own image -- only its own code and its own
# dependencies (no FastAPI, Celery, Redis... those are Booking's).
#
# Built from the REPO ROOT as context, not this folder, because the workspace
# lockfile (uv.lock) lives at the root:
#
#     docker build -f services/notification/Dockerfile .

FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /code

# dependency layer first, same caching trick as Booking's Dockerfile: it only
# rebuilds when the lockfile or a pyproject changes, not on every code edit.
# uv reads the root pyproject for the workspace, but --package installs only
# Notification's own dependencies from it.
COPY pyproject.toml uv.lock ./
COPY services/notification/pyproject.toml services/notification/pyproject.toml
RUN uv sync --package notification --no-dev --frozen --no-install-workspace

COPY services/notification services/notification
RUN uv sync --package notification --no-dev --frozen

ENV PATH="/code/.venv/bin:$PATH"

# the consumer; docker-compose's notification-migrate overrides this with
# `python -m notification.migrate`
CMD ["python", "-m", "notification"]
