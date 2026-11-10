.DEFAULT_GOAL := help
.PHONY: help up down roles migrate test lint format generate mock-apis pipeline eval eval-load-key eval-stage1 \
	airflow-up airflow-down airflow-password airflow-check

help: ## List available targets
	@grep -E '^[a-z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up: ## Start local services
	docker compose up -d --wait

down: ## Stop local services
	docker compose down

roles: ## Create/update DB login roles from .env (idempotent)
	PYTHONPATH=backend uv run python -m app.db.roles

migrate: ## Apply Alembic migrations
	uv run alembic upgrade head

test: ## Run pytest
	uv run pytest

lint: ## ruff check + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and fix lint issues
	uv run ruff check --fix .
	uv run ruff format .

generate: ## Generate synthetic data with planted errors into data/generated/
	uv run python -m generator

mock-apis: ## Serve mock Meta/Google/TikTok APIs on MOCK_PLATFORMS_PORT
	uv run python -m mock_platforms

pipeline: ## Run the nightly pipeline once (needs `make mock-apis` running)
	PYTHONPATH=backend uv run python -m pipelines.run

eval-load-key: ## Owner: load data/generated/answer_key.json into eval.answer_key
	PYTHONPATH=backend uv run python -m evals.load_answer_key

eval-stage1: ## Compare app.issues with eval.answer_key; fails on a missed error
	PYTHONPATH=backend uv run python -m evals.stage1

eval: ## Run evals (stage 2)
	@echo "not yet implemented (stage 2)"

AIRFLOW_ENV_VARS := AIRFLOW_DB_PASSWORD AIRFLOW_JWT_SECRET AIRFLOW_API_SECRET_KEY
AIRFLOW_SERVICES := airflow airflow-mock-apis airflow-postgres

airflow-up: ## Start Airflow (own compose profile) with the nightly DAG; UI on 127.0.0.1:AIRFLOW_PORT
	@for var in $(AIRFLOW_ENV_VARS); do \
		grep -Eq "^$$var=.+" .env || { echo "Set $$var in .env (see .env.example)"; exit 1; }; \
	done
	docker compose --profile airflow up -d --build --wait

airflow-down: ## Stop and remove the Airflow containers (matchbook Postgres keeps running)
	docker compose --profile airflow rm --stop --force $(AIRFLOW_SERVICES)

airflow-password: ## Print the generated Airflow UI password for user `admin`
	docker compose --profile airflow exec airflow cat /opt/airflow/simple_auth_manager_passwords.json.generated

airflow-check: ## Optional: DAG import errors in the running Airflow (needs airflow-up; not in CI)
	docker compose --profile airflow exec airflow airflow dags list-import-errors
