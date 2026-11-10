"""The schema contract shared by the model and migration tests."""

# `demo` holds only the read-only demo views (migration 0005, decision 035).
EXPECTED_SCHEMAS = {"raw", "core", "app", "eval", "demo"}

EXPECTED_TABLES = {
    "raw.meta_responses",
    "raw.google_responses",
    "raw.tiktok_responses",
    "core.clients",
    "core.platforms",
    "core.ad_accounts",
    "core.insertion_orders",
    "core.campaigns",
    "core.campaign_aliases",
    "core.daily_delivery",
    "core.invoices",
    "core.invoice_lines",
    "core.credit_notes",
    "core.reconciliation_results",
    "app.users",
    "app.user_client_scope",
    "app.issues",
    "app.audit_log",
    "app.pipeline_runs",
    "eval.answer_key",
}

# Written by the pipeline, so every row carries the run that wrote it.
RUN_ID_TABLES = {
    "raw.meta_responses",
    "raw.google_responses",
    "raw.tiktok_responses",
    "core.ad_accounts",
    "core.insertion_orders",
    "core.campaigns",
    "core.campaign_aliases",
    "core.daily_delivery",
    "core.invoices",
    "core.invoice_lines",
    "core.credit_notes",
    "core.reconciliation_results",
    "app.issues",
}

# Upserted by the pipeline, so they need updated_at.
UPDATED_AT_TABLES = {
    "core.clients",
    "core.platforms",
    "core.ad_accounts",
    "core.insertion_orders",
    "core.campaigns",
    "core.campaign_aliases",
    "core.daily_delivery",
    "core.invoices",
    "core.invoice_lines",
    "core.credit_notes",
    "core.reconciliation_results",
    "app.issues",
}

# Every NUMERIC column is money (12,2) except these.
NON_MONEY_NUMERIC_COLUMNS = {
    "core.clients.markup_pct",
    "core.campaign_aliases.confidence",
    "core.invoice_lines.extracted_confidence",
    "app.pipeline_runs.cost_eur",
}

FINANCIAL_TABLES = {
    "core.invoices",
    "core.invoice_lines",
    "core.credit_notes",
    "core.daily_delivery",
}
