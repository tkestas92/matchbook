"""Least-privilege role grants, curated agent views and row-level security.

Login roles and their passwords are managed by `make roles` (decision 015); this
migration only creates them NOLOGIN when missing so it can run in any order.
RLS design: decision 016.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `agent_views` never logs in: it owns the agent views so base-table RLS is applied
# as a non-superuser, non-owner role while `agent_ro` gets no base-table access.
ROLES = ("etl", "app", "agent_ro", "eval_runner", "agent_views")
ALL_SCHEMAS = ("raw", "core", "app", "eval")
WRITE = "SELECT, INSERT, UPDATE, DELETE"
AGENT_STATEMENT_TIMEOUT = "5s"
# Roles allowed to call set_config(); `agent_ro` must not widen its own scope.
SET_CONFIG_ROLES = "etl, app, eval_runner"
SET_CONFIG = "pg_catalog.set_config(text, text, boolean)"

SCOPE_FUNCTION = "core.agent_client_ids()"
IN_SCOPE = f"= ANY ({SCOPE_FUNCTION})"

# Every table an agent view reads, with the predicate that limits it to scoped clients.
RLS_POLICIES = {
    "core.clients": f"id {IN_SCOPE}",
    "core.ad_accounts": f"client_id {IN_SCOPE}",
    "core.insertion_orders": f"client_id {IN_SCOPE}",
    "core.invoices": f"client_id {IN_SCOPE}",
    "app.issues": f"client_id {IN_SCOPE}",
    "core.campaigns": (
        "EXISTS (SELECT 1 FROM core.ad_accounts a "
        f"WHERE a.id = ad_account_id AND a.client_id {IN_SCOPE})"
    ),
    "core.daily_delivery": (
        "EXISTS (SELECT 1 FROM core.campaigns c "
        "JOIN core.ad_accounts a ON a.id = c.ad_account_id "
        f"WHERE c.id = campaign_id AND a.client_id {IN_SCOPE})"
    ),
}

AGENT_VIEWS = {
    "core.v_spend_daily": """
        SELECT d.date, a.client_id, c.platform_id, d.campaign_id,
               c.name AS campaign_name, d.spend, d.impressions, d.clicks,
               d.conversions
        FROM core.daily_delivery d
        JOIN core.campaigns c ON c.id = d.campaign_id
        JOIN core.ad_accounts a ON a.id = c.ad_account_id
    """,
    "core.v_pacing_base": """
        SELECT io.id AS io_id, io.client_id, io.platform_id, io.campaign_ref,
               io.start_date, io.end_date, io.budget,
               COALESCE(SUM(d.spend), 0)::NUMERIC(12, 2) AS spend_to_date,
               MAX(d.date) AS last_delivery_date
        FROM core.insertion_orders io
        LEFT JOIN core.campaigns c ON c.io_id = io.id
        LEFT JOIN core.daily_delivery d
               ON d.campaign_id = c.id
              AND d.date BETWEEN io.start_date AND io.end_date
        GROUP BY io.id
    """,
    "core.v_invoices": """
        SELECT id, kind, vendor, number, client_id, platform_id, issue_date,
               period_start, period_end, subtotal, vat, total, status
        FROM core.invoices
    """,
    "core.v_issues": """
        SELECT id, type, severity, client_id, platform_id, period, entity_type,
               entity_id, amount, explanation, proposed_action, status,
               created_at
        FROM app.issues
    """,
}


def upgrade() -> None:
    for role in ROLES:
        create_role_if_missing(role)
    grant_role_privileges()
    create_scope_function()
    create_agent_views()
    enable_row_level_security()
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SET_CONFIG} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SET_CONFIG} TO {SET_CONFIG_ROLES}")
    on_current_database("REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC")
    on_current_database("GRANT TEMPORARY ON DATABASE %I TO etl, app")
    on_current_database(
        f"ALTER ROLE agent_ro IN DATABASE %I SET statement_timeout = '{AGENT_STATEMENT_TIMEOUT}'"
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


def grant_on_schemas(
    schemas: tuple[str, ...], role: str, privileges: str, *, sequences: bool
) -> None:
    for schema in schemas:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        op.execute(f"GRANT {privileges} ON ALL TABLES IN SCHEMA {schema} TO {role}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT {privileges} ON TABLES TO {role}"
        )
        if sequences:
            op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
            op.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
            )


def grant_role_privileges() -> None:
    grant_on_schemas(("raw", "core"), "etl", WRITE, sequences=True)
    grant_on_schemas(("core",), "app", "SELECT", sequences=False)
    grant_on_schemas(("app",), "app", WRITE, sequences=True)
    grant_on_schemas(ALL_SCHEMAS, "eval_runner", "SELECT", sequences=False)

    op.execute("GRANT USAGE ON SCHEMA core, app TO agent_views")
    view_sources = set(RLS_POLICIES) | {"app.user_client_scope"}
    for table in sorted(view_sources):
        op.execute(f"GRANT SELECT ON {table} TO agent_views")


def create_scope_function() -> None:
    """Clients visible to the session's `app.user_id`; unset or unknown → none."""
    op.execute(
        f"""
        CREATE FUNCTION {SCOPE_FUNCTION} RETURNS BIGINT[]
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
            SELECT COALESCE(array_agg(s.client_id), '{{}}')
            FROM app.user_client_scope s
            WHERE s.user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
        $$
        """
    )
    op.execute(f"ALTER FUNCTION {SCOPE_FUNCTION} OWNER TO agent_views")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SCOPE_FUNCTION} FROM PUBLIC")
    # Function EXECUTE is checked as the session user even inside a view.
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCOPE_FUNCTION} TO agent_views, agent_ro")


def create_agent_views() -> None:
    op.execute("GRANT USAGE ON SCHEMA core TO agent_ro")
    for view, query in AGENT_VIEWS.items():
        op.execute(f"CREATE VIEW {view} WITH (security_barrier = true) AS {query}")
        op.execute(f"ALTER VIEW {view} OWNER TO agent_views")
        op.execute(f"GRANT SELECT ON {view} TO agent_ro")


def enable_row_level_security() -> None:
    for table, predicate in RLS_POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY agent_scope ON {table} FOR SELECT TO agent_views USING ({predicate})"
        )
        op.execute(
            f"CREATE POLICY full_access ON {table} TO etl, app, eval_runner "
            "USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    on_current_database("ALTER ROLE agent_ro IN DATABASE %I RESET statement_timeout")
    on_current_database("REVOKE TEMPORARY ON DATABASE %I FROM etl, app")
    on_current_database("GRANT TEMPORARY ON DATABASE %I TO PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SET_CONFIG} FROM {SET_CONFIG_ROLES}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SET_CONFIG} TO PUBLIC")

    for table in RLS_POLICIES:
        op.execute(f"DROP POLICY full_access ON {table}")
        op.execute(f"DROP POLICY agent_scope ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for view in reversed(AGENT_VIEWS):
        op.execute(f"DROP VIEW {view}")
    op.execute(f"DROP FUNCTION {SCOPE_FUNCTION}")

    for role in ROLES:
        for schema in ALL_SCHEMAS:
            op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
            op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}")
            op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {role}")
            op.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {role}"
            )
            op.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON SEQUENCES FROM {role}"
            )
