# Matchbook

Three-way media reconciliation & reporting for agencies

**Status:** work in progress

See the full plan in [docs/plan.md](docs/plan.md).

## Development

```bash
cp .env.example .env   # then edit secrets if needed
make up                # start PostgreSQL
make migrate           # apply Alembic migrations
make test              # run pytest
make lint              # ruff check + format check
```

Install the git pre-commit hook (ruff lint `--fix` + format) once per clone:

```bash
uv run pre-commit install
```
