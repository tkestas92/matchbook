"""Read-only public demo: schema `demo`, two curated views and the `demo_ro` role.

The views are owned by the migration owner, so they see every (synthetic) client; `demo_ro`
reads only these views. The login and password come from `make roles` (decision 015).
Design: decision 035.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEMO_ROLE = "demo_ro"
DEMO_STATEMENT_TIMEOUT = "3s"

# With FORCE ROW LEVEL SECURITY the owner would be filtered too and the views would be empty.
VIEW_SOURCES = ("app.issues", "app.pipeline_runs", "core.clients", "core.platforms")

DEMO_VIEWS = {
    "demo.v_issues": """
        SELECT i.id, i.type, i.severity, i.amount, i.explanation, i.proposed_action,
               i.period, c.code AS client_code, p.name AS platform
        FROM app.issues i
        LEFT JOIN core.clients c ON c.id = i.client_id
        LEFT JOIN core.platforms p ON p.id = i.platform_id
        WHERE i.status = 'open'
    """,
    # Only step name, status and numeric counts leave `details`: never data_dir, errors,
    # durations or lists of rejected documents.
    "demo.v_pipeline_runs": """
        SELECT r.status, r.started_at, r.finished_at,
               COALESCE((
                   SELECT jsonb_agg(
                              jsonb_build_object(
                                  'name', s.step ->> 'name',
                                  'status', s.step ->> 'status',
                                  'counts', COALESCE((
                                      SELECT jsonb_object_agg(e.key, e.value)
                                      FROM jsonb_each(
                                          CASE WHEN jsonb_typeof(s.step -> 'counts') = 'object'
                                               THEN s.step -> 'counts'
                                               ELSE '{}'::jsonb END
                                      ) AS e(key, value)
                                      WHERE jsonb_typeof(e.value) = 'number'
                                  ), '{}'::jsonb)
                              )
                              ORDER BY s.ord
                          )
                   FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof(r.details -> 'steps') = 'array'
                                 THEN r.details -> 'steps'
                                 ELSE '[]'::jsonb END
                        ) WITH ORDINALITY AS s(step, ord)
               ), '[]'::jsonb) AS steps
        FROM app.pipeline_runs r
    """,
}


def upgrade() -> None:
    create_role_if_missing(DEMO_ROLE)
    refuse_forced_row_security()
    op.execute("CREATE SCHEMA demo")
    op.execute("REVOKE ALL ON SCHEMA demo FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA demo TO {DEMO_ROLE}")
    for view, query in DEMO_VIEWS.items():
        op.execute(f"CREATE VIEW {view} WITH (security_barrier = true) AS {query}")
        op.execute(f"REVOKE ALL ON {view} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON {view} TO {DEMO_ROLE}")
    on_current_database(
        f"ALTER ROLE {DEMO_ROLE} IN DATABASE %I SET default_transaction_read_only = on"
    )
    on_current_database(
        f"ALTER ROLE {DEMO_ROLE} IN DATABASE %I SET statement_timeout = '{DEMO_STATEMENT_TIMEOUT}'"
    )


def create_role_if_missing(role: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN;
            END IF;
        END $$
        """
    )


def on_current_database(template: str) -> None:
    """Run a statement whose `%I` is the current database name."""
    op.execute(f"DO $$ BEGIN EXECUTE format($q${template}$q$, current_database()); END $$")


def refuse_forced_row_security() -> None:
    tables = ", ".join(f"'{table}'::regclass" for table in VIEW_SOURCES)
    op.execute(
        f"""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM pg_class WHERE oid IN ({tables}) AND relforcerowsecurity) THEN
                RAISE EXCEPTION 'FORCE ROW LEVEL SECURITY on a demo view source would hide every row from the owner-rights demo views (decision 035)';
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    on_current_database(f"ALTER ROLE {DEMO_ROLE} IN DATABASE %I RESET statement_timeout")
    on_current_database(
        f"ALTER ROLE {DEMO_ROLE} IN DATABASE %I RESET default_transaction_read_only"
    )
    for view in reversed(DEMO_VIEWS):
        op.execute(f"REVOKE ALL ON {view} FROM {DEMO_ROLE}")
        op.execute(f"DROP VIEW {view}")
    op.execute(f"REVOKE ALL ON SCHEMA demo FROM {DEMO_ROLE}")
    op.execute("DROP SCHEMA demo")
