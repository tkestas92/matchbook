#!/bin/sh
# One-shot seeding for the demo database (decision 036); run by `make demo-init`.
# Generated files, including the answer key, live only on this container's tmpfs /app/data.
set -eu

MOCK_PID=""
cleanup() {
    if [ -n "$MOCK_PID" ]; then kill "$MOCK_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

echo "==> roles"
python -m app.db.roles

echo "==> migrations"
alembic upgrade head

echo "==> synthetic data"
python -m generator

echo "==> mock platform APIs"
# Token for this container only: the mock server and the pipeline share it, nothing else sees it.
MOCK_PLATFORMS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MOCK_PLATFORMS_TOKEN
python -m mock_platforms &
MOCK_PID=$!
python - <<'PY'
import os, socket, time

port = int(os.environ.get("MOCK_PLATFORMS_PORT", "8100"))
for _ in range(60):
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("mock platform APIs did not start")
PY

echo "==> pipeline"
python -m pipelines.run
echo "==> demo data seeded"
