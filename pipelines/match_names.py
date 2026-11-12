"""Pipeline step after load_plans: campaign renames, rule first and AI second (decision 047).

    raw snapshots -> the records normalize keeps (its own functions, read-only)
      -> find_renames: tagged campaign, later untagged name not confirmed by a person or model
      -> key already in app.issues: no model call; only its run_id moves to this run
      -> NAME_JUDGE=llm: judge_rename proposes an action; otherwise the cautious action
      -> one issue per campaign-month, upserted as `app`

Nothing here writes `core.campaign_aliases`: a person confirms a rename later. On a conflict
only `run_id` is updated, so the text, amount, severity, status and decision columns of an
existing issue never change. The cost cap or a provider error stops judging; the remaining
renames are still written by the rule and the run is partial.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.db.models.app import Issue
from app.db.models.core import AdAccount, Campaign, CampaignAlias, Client, Platform
from app.llm.client import LLMClient
from app.llm.cost import CostCapExceeded
from app.llm.provider import ProviderError
from app.reconciliation.models import IssueDraft, NameObservation, RenameCandidate, RenameVerdict
from app.reconciliation.rename_judge import judge_rename
from app.reconciliation.renames import (
    ISSUE_KEY_PREFIX,
    ConfirmedAlias,
    find_renames,
    issue_key,
    rename_draft,
    rule_verdict,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, Engine, select

from pipelines.ingest import PLATFORMS
from pipelines.load_plans import client_ids, platform_ids
from pipelines.normalize import (
    DeliveryRecord,
    NormalizeResult,
    collect_records,
    load_accounts,
    load_platform_snapshots,
    upsert,
)
from pipelines.reconcile import issue_row

RULE = "rule"
LLM = "llm"
NameJudge = Literal["rule", "llm"]
CONFIRMED_SOURCES = ("ai", "human")
SKIPPED_COST_CAP = "skipped_cost_cap"
SKIPPED_PROVIDER_ERROR = "skipped_provider_error"

AccountKey = tuple[str, str]


class NameSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAME_", env_file=".env", extra="ignore")

    judge: NameJudge = RULE


@dataclass(frozen=True)
class ClientRef:
    code: str
    name: str


@dataclass
class MatchNamesResult:
    run_id: uuid.UUID
    candidates: int = 0
    already_known: int = 0
    judged: int = 0
    issues_written: int = 0
    skipped_cost_cap: int = 0
    skipped_provider_error: int = 0
    llm_cost_eur: Decimal = Decimal(0)
    llm_tokens: int = 0
    llm_calls: int = 0
    drafts: list[IssueDraft] = field(default_factory=list)


def account_clients(conn: Connection) -> dict[AccountKey, ClientRef]:
    query = (
        select(
            Platform.name, AdAccount.external_account_id, Client.code, Client.name.label("client")
        )
        .join(Platform, Platform.id == AdAccount.platform_id)
        .join(Client, Client.id == AdAccount.client_id)
    )
    return {
        (row.name, row.external_account_id): ClientRef(row.code, row.client)
        for row in conn.execute(query)
    }


def delivery_records(conn: Connection) -> list[DeliveryRecord]:
    """The records normalize keeps: newest snapshot per period, one account per campaign."""
    collected = NormalizeResult(run_id=uuid.uuid4())  # Scratch; nothing of it is stored.
    snapshots = [s for api in PLATFORMS for s in load_platform_snapshots(conn, api)]
    collect_records(collected, snapshots, load_accounts(conn))
    return collected.records


def observations(
    records: Sequence[DeliveryRecord], clients: dict[AccountKey, ClientRef]
) -> list[NameObservation]:
    return [
        NameObservation(
            platform=record.platform,
            platform_campaign_id=record.campaign_id,
            client=clients[(record.platform, record.account_id)].code,
            date=record.date,
            name=record.campaign_name,
            spend=record.spend,
        )
        for record in records
    ]


def confirmed_aliases(conn: Connection) -> set[ConfirmedAlias]:
    query = (
        select(Platform.name, Campaign.platform_campaign_id, CampaignAlias.alias)
        .join(Campaign, Campaign.id == CampaignAlias.campaign_id)
        .join(Platform, Platform.id == Campaign.platform_id)
        .where(CampaignAlias.source.in_(CONFIRMED_SOURCES))
    )
    return {(row.name, row.platform_campaign_id, row.alias) for row in conn.execute(query)}


def known_keys(conn: Connection) -> set[str]:
    """Rename issues that exist, whatever their status."""
    query = select(Issue.dedup_key).where(Issue.dedup_key.startswith(ISSUE_KEY_PREFIX))
    return set(conn.scalars(query))


def judge_all(
    result: MatchNamesResult,
    candidates: Sequence[RenameCandidate],
    known: set[str],
    client: LLMClient | None,
    client_names: dict[str, str],
) -> list[RenameVerdict]:
    """A verdict per candidate; the model is asked only about new ones, until it must stop."""
    verdicts: list[RenameVerdict] = []
    stopped: str | None = None
    for candidate in candidates:
        if issue_key(candidate) in known:
            result.already_known += 1
            verdicts.append(rule_verdict())  # Only its run_id is written.
            continue
        if client is None:
            verdicts.append(rule_verdict())
            continue
        if stopped is None:
            try:
                verdicts.append(judge_rename(client, candidate, client_names[candidate.client]))
                result.judged += 1
                continue
            except CostCapExceeded:
                stopped = SKIPPED_COST_CAP
            except ProviderError:
                stopped = SKIPPED_PROVIDER_ERROR
        if stopped == SKIPPED_COST_CAP:
            result.skipped_cost_cap += 1
        else:
            result.skipped_provider_error += 1
        verdicts.append(rule_verdict(f"model:{stopped}"))
    return verdicts


def judge_with_usage(
    result: MatchNamesResult,
    candidates: Sequence[RenameCandidate],
    known: set[str],
    client: LLMClient,
    client_names: dict[str, str],
) -> list[RenameVerdict]:
    """`judge_all`, recording this step's share of the shared run ledger."""
    ledger = client.ledger
    cost_before, tokens_before, calls_before = ledger.total, ledger.tokens, ledger.calls
    try:
        return judge_all(result, candidates, known, client, client_names)
    finally:
        result.llm_cost_eur = ledger.total - cost_before
        result.llm_tokens = ledger.tokens - tokens_before
        result.llm_calls = ledger.calls - calls_before


def write_rename_issues(engine: Engine, run_id: uuid.UUID, drafts: Sequence[IssueDraft]) -> None:
    """Upsert as `app`; an existing issue gets only the new run_id."""
    with engine.begin() as conn:
        clients, platforms = client_ids(conn), platform_ids(conn)
        rows = [issue_row(run_id, draft, clients, platforms) for draft in drafts]
        upsert(conn, Issue, rows, ("dedup_key",), ("run_id",))


def match_names(
    run_id: uuid.UUID, etl: Engine, app: Engine, client: LLMClient | None
) -> MatchNamesResult:
    """Read as etl and app, judge outside any transaction, then write the issues as app."""
    result = MatchNamesResult(run_id=run_id)
    with etl.connect() as conn:
        clients = account_clients(conn)
        seen = observations(delivery_records(conn), clients)
        confirmed = confirmed_aliases(conn)
    with app.connect() as conn:
        known = known_keys(conn)

    candidates = find_renames(seen, confirmed)
    result.candidates = len(candidates)
    client_names = {ref.code: ref.name for ref in clients.values()}
    if client is None:
        verdicts = judge_all(result, candidates, known, None, client_names)
    else:
        verdicts = judge_with_usage(result, candidates, known, client, client_names)

    result.drafts = [rename_draft(c, v) for c, v in zip(candidates, verdicts, strict=True)]
    write_rename_issues(app, run_id, result.drafts)
    result.issues_written = len(result.drafts)
    return result
