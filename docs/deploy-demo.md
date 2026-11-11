# Deploying the public demo (task 1.13b)

The read-only demo API (decision 035) with its own PostgreSQL, run with Docker Compose profile
`demo` on macserver (Ubuntu x86_64) and exposed through the Cloudflare Tunnel that already runs
on the host. Design: decision 036.

> **Warning.** On the server, never run plain `docker compose up`, `docker compose down` or
> `docker compose --profile demo up` without service names. That would also start the default
> `postgres` service (and anything else without a profile). Always use the make targets:
> `make demo-init`, `make demo-up`, `make demo-down`, `make demo-logs`.

What runs:

| Service | Role | Network exposure |
|---|---|---|
| `demo-postgres` | Demo database (volume `demo_postgres_data`) | none: compose network only |
| `demo-init` | One-shot: roles → migrations → synthetic data → one pipeline run | none; exits when done |
| `demo-api` | Read-only API as `demo_ro` | `127.0.0.1:8200` only |

`demo-api` receives only `DEMO_RO_DB_PASSWORD`. The owner and the other role passwords go to
`demo-init` only. Generated files, including the answer key, exist only on `demo-init`'s tmpfs
and are gone when it exits.

## 1. Prerequisites

Docker Engine with the Compose v2 plugin (BuildKit is the default builder), `git`, `make`,
`openssl`, `curl`. Check that port 8200 is free on 127.0.0.1:

```sh
docker compose version
ss -ltn | grep ':8200 ' || echo "8200 is free"
```

## 2. Clone

```sh
git clone https://github.com/tkestas92/matchbook.git ~/matchbook-demo
cd ~/matchbook-demo
git checkout main
```

## 3. Create `.env` with random passwords

`COMPOSE_PROJECT_NAME` keeps the demo's containers, network and volume names separate from
everything else on the server.

```sh
cd ~/matchbook-demo
umask 077
cat > .env <<EOF
COMPOSE_PROJECT_NAME=matchbook-demo
POSTGRES_USER=matchbook
POSTGRES_PASSWORD=$(openssl rand -hex 32)
POSTGRES_DB=matchbook
AGENCY_TIMEZONE=Europe/Vilnius
ETL_DB_PASSWORD=$(openssl rand -hex 32)
APP_DB_PASSWORD=$(openssl rand -hex 32)
AGENT_RO_DB_PASSWORD=$(openssl rand -hex 32)
EVAL_RUNNER_DB_PASSWORD=$(openssl rand -hex 32)
DEMO_RO_DB_PASSWORD=$(openssl rand -hex 32)
DEMO_API_PORT=8200
INGEST_PERIOD_START=2026-05-01
INGEST_PERIOD_END=2026-10-31
EOF
chmod 600 .env
```

Hex passwords keep the database URL valid without escaping. Do not print `.env` or run
`docker compose config` in shared terminals: both show the passwords.

## 4. Seed the database (build on the server)

```sh
make demo-init
```

Builds both images, starts `demo-postgres`, waits for it to be healthy, then runs the seeding job
in the foreground. It ends with the pipeline summary and `==> demo data seeded`. A non-zero exit
means seeding failed; read the output, fix, and run it again (every step is idempotent).

## 5. Start the API

```sh
make demo-up
```

Starts `demo-api` and waits until its healthcheck passes.

## 6. Verify on the server

```sh
curl -fsS http://127.0.0.1:8200/health
curl -fsS http://127.0.0.1:8200/api/status
curl -fsS http://127.0.0.1:8200/api/summary
curl -fsS 'http://127.0.0.1:8200/api/issues?limit=2'
curl -fsS http://127.0.0.1:8200/api/eval
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8200/api/issues   # expect 405
docker ps --filter name=matchbook-demo --format '{{.Names}}\t{{.Ports}}'
```

`docker ps` must show `127.0.0.1:8200->8200/tcp` for `demo-api` and no published port for
`demo-postgres`. From another machine on the LAN, `curl http://<server-ip>:8200/health` must fail.

## 7. Cloudflare Tunnel public hostname

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels →** the existing tunnel →
**Public Hostname → Add a public hostname**:

- Subdomain `demo-matchbook`, domain `kantrybes.lt`
- Service type `HTTP`, URL `localhost:8200` (i.e. `http://localhost:8200`)

Save, then check from outside: `curl -fsS https://demo-matchbook.kantrybes.lt/health`.

If the tunnel is managed by a local `config.yml` instead of the dashboard, add the equivalent
ingress rule above the catch-all and restart `cloudflared`:

```yaml
- hostname: demo-matchbook.kantrybes.lt
  service: http://localhost:8200
```

**Rate limiting must be set in Cloudflare.** The API has no auth and no rate limiting of its own
(decision 035). Add a WAF rate limiting rule for `demo-matchbook.kantrybes.lt` (for example, block
an IP for 10 minutes after 60 requests per minute). Keep the 5-connection DB pool in mind: bursts
above it get 503s.

## Update, refresh, stop

```sh
cd ~/matchbook-demo
git pull
make demo-up            # rebuild and restart the API
make demo-init          # optional: re-seed (adds a new pipeline run)
make demo-logs          # follow API logs (Ctrl-C to stop)
make demo-down          # stop and remove the demo containers; the database volume stays
```

To delete the demo data too: `make demo-down`, then
`docker volume rm matchbook-demo_demo_postgres_data`.
