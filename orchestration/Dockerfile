# Airflow plus an isolated project venv for the tasks (decision 034).
# Airflow keeps its own dependency pins; the tasks run in /opt/matchbook-venv with the project's
# locked dependencies. Project code is mounted at /opt/matchbook by docker compose, not copied.
FROM apache/airflow:3.3.2-python3.12

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

USER root
ENV UV_PROJECT_ENVIRONMENT=/opt/matchbook-venv \
    UV_PYTHON=/usr/python/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /opt/matchbook
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache \
    && printf '/opt/matchbook/backend\n/opt/matchbook\n' \
       > /opt/matchbook-venv/lib/python3.12/site-packages/matchbook.pth \
    && mkdir -p /opt/airflow/run-state \
    && chown airflow:root /opt/airflow/run-state

USER airflow
WORKDIR /opt/airflow
