# Public demo (decision 036). Build context is the repo root; deploy/demo/Dockerfile.dockerignore
# decides what enters the context, the COPY lines below what enters each image.
#   demo-api:  read-only API (backend/app + docs/eval-results.md only), logs in as demo_ro.
#   demo-init: one-shot seeding job (roles, migrations, generator, mock APIs, one pipeline run).
FROM python:3.12-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/backend:/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache \
    && useradd --system --uid 10001 --user-group --no-create-home matchbook

FROM base AS demo-api
# Code stays owned by root: the runtime user can read it, not change it.
COPY backend/app backend/app
COPY docs/eval-results.md docs/eval-results.md
USER matchbook
EXPOSE 8200
CMD ["uvicorn", "app.demo.app:create_demo_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8200"]

FROM base AS demo-init
COPY alembic.ini alembic.ini
COPY backend/app backend/app
COPY backend/migrations backend/migrations
COPY pipelines pipelines
COPY generator generator
COPY mock_platforms mock_platforms
COPY deploy/demo/init.sh deploy/demo/init.sh
USER matchbook
CMD ["sh", "deploy/demo/init.sh"]
