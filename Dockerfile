FROM node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY --from=frontend /app/static ./app/static
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
RUN chmod +x /app/scripts/*.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin healthmirror \
    && chown -R healthmirror:healthmirror /app

USER healthmirror

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host ${APP_HOST:-127.0.0.1} --port ${APP_PORT:-8000} --no-access-log"]
