FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /code

COPY pyproject.toml uv.lock ./
# BOOK-64: the root is a uv workspace (services/notification is a member), and
# uv reads every member's pyproject.toml even when it installs none of them --
# Notification is dev-only here, never part of Booking's image
COPY services/notification/pyproject.toml services/notification/pyproject.toml
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
