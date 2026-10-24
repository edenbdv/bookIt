# BOOK-65: Payment's own image -- only its own code and its own dependencies
# (no Celery, Redis, bcrypt... those are Booking's). Same shape as Notification's.
#
# Built from the REPO ROOT as context, not this folder, because the workspace
# lockfile (uv.lock) lives at the root:
#
#     docker build -f services/payment/Dockerfile .

FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /code

# dependency layer first: only rebuilds when the lockfile or a pyproject changes.
# uv reads the root pyproject for the workspace, but --package installs only
# Payment's own dependencies from it.
COPY pyproject.toml uv.lock ./
COPY services/payment/pyproject.toml services/payment/pyproject.toml
RUN uv sync --package payment --no-dev --frozen --no-install-workspace

COPY services/payment services/payment
RUN uv sync --package payment --no-dev --frozen

ENV PATH="/code/.venv/bin:$PATH"

# the HTTP app; docker-compose's payment-migrate / -scheduler / -consumer override
# this with their own `python -m payment.<module>`
CMD ["uvicorn", "payment.main:app", "--host", "0.0.0.0", "--port", "8001"]
