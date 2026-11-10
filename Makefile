.DEFAULT_GOAL := help
.PHONY: help up down roles migrate test lint format generate mock-apis pipeline eval eval-load-key eval-stage1

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
