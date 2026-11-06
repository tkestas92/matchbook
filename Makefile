.DEFAULT_GOAL := help
.PHONY: help up down roles migrate test lint format generate pipeline eval

help: ## List available targets
	@grep -E '^[a-z_]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

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
	uv run ruff format .
	uv run ruff check --fix .

generate: ## Generate synthetic data (stage 1)
	@echo "not yet implemented (stage 1)"

pipeline: ## Run the nightly pipeline without Airflow (stage 1)
	@echo "not yet implemented (stage 1)"

eval: ## Run evals (stage 2)
	@echo "not yet implemented (stage 2)"
