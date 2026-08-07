FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /code

COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
