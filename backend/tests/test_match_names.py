"""The match_names step: renames from raw snapshots into `app.issues` (decision 047).

Only FakeProvider answers; no real model and no network. Tests that take `seeded` need the
local Postgres (`make up`) and run on a throwaway database like the other pipeline tests.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.db.models.raw import MetaResponse
from app.llm.client import LLMClient, LLMLimits
from app.llm.cost import CostLedger
from app.llm.fake import FakeProvider, Scripted
from app.reconciliation import templates
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import DBAPIError
from tests.conftest import PipelineDb
from tests.extraction_samples import MODEL, PRICE

from pipelines import run as runner
from pipelines.match_names import LLM, RULE, NameSettings, match_names
from pipelines.normalize import run_normalize
from pipelines.reference import load_reference
from pipelines.run import PARTIAL, RunContext, run_status

TZ = "Europe/Vilnius"
TAGGED = "Acme | Meta | 2026-06 [ACME-META-2606]"
RENAMED = "Acme Meta – Promo June"
TAGGED_2 = "Acme | Meta | 2026-06 [ACME-META-2606b]"
RENAMED_2 = "Acme Meta – Black Friday"
KEY = "reconciliation:campaign_renamed:meta:c1:2026-06"
KEY_2 = "reconciliation:campaign_renamed:meta:c2:2026-06"
REFERENCE = {
    "clients": [
        {
            "code": "ACME",
            "name": "UAB Acme",
            "industry": "retail",
            "billing_model": "markup",
            "markup_pct": "10.00",
            "mgmt_fee": None,
        }
    ],
    "platforms": [{"name": "meta", "timezone": TZ}],
    "ad_accounts": [
        {"client_code": "ACME", "platform": "meta", "external_account_id": "1001", "timezone": TZ}
    ],
}
ISSUE_COLUMNS = (
    "dedup_key, type, severity, client_id, platform_id, period, entity_type, entity_id, amount, "
    "explanation, proposed_action, status, decided_by, decided_at, decision_note, run_id"
)


def day_row(day: int, name: str, campaign: str = "c1", spend: str = "10.00") -> dict[str, Any]:
    return {
        "campaign_id": campaign,
        "campaign_name": name,
        "date": date(2026, 6, day).isoformat(),
        "spend": spend,
        "impressions": 1000,
        "clicks": 20,
        "conversions": 3,
    }


def store_raw(db: PipelineDb, rows: list[dict[str, Any]]) -> None:
    with db.etl.begin() as conn:
        conn.execute(
            insert(MetaResponse).values(
                run_id=uuid.uuid4(),
                account_id="1001",
                period_start=date(2026, 6, 1),
                period_end=date(2026, 6, 30),
                page=1,
                fetched_at=datetime(2026, 7, 1, 3, tzinfo=UTC),
                payload={"data": rows, "paging": {"cursors": {}}},
                body=b"{}",
            )
        )


@pytest.fixture
def seeded(pipeline_db: PipelineDb, tmp_path: Path) -> PipelineDb:
    """c1 renamed on the 15th, c2 renamed on the 20th, c3 never renamed; normalized."""
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    load_reference(uuid.uuid4(), path, pipeline_db.etl)
    rows = [day_row(day, TAGGED if day < 15 else RENAMED) for day in range(1, 18)]
    rows += [day_row(day, TAGGED_2 if day < 20 else RENAMED_2, "c2") for day in range(1, 23)]
    rows += [day_row(day, "Acme | Meta | 2026-06 [ACME-META-2606c]", "c3") for day in (1, 2)]
    store_raw(pipeline_db, rows)
    run_normalize(uuid.uuid4(), pipeline_db.etl)
    return pipeline_db


def make_client(script: list[Scripted], run_cap: str = "1") -> tuple[LLMClient, FakeProvider]:
    fake = FakeProvider(script)
    client = LLMClient(
        provider=fake,
        limits=LLMLimits(model=MODEL, prices={MODEL: PRICE}),
        ledger=CostLedger(run_cap=Decimal(run_cap), monthly_cap=Decimal("10")),
        sleep=lambda _: None,
    )
    return client, fake


def answer(same: bool, reason: str) -> str:
    return json.dumps({"same_campaign": same, "confidence": "0.9", "reason": reason})


def issues(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {ISSUE_COLUMNS} FROM app.issues ORDER BY dedup_key")
        ).all()
    return {row.dedup_key: row for row in rows}


def aliases(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT campaign_id, alias, source, confidence, run_id, created_at, updated_at "
                "FROM core.campaign_aliases ORDER BY campaign_id, alias"
            )
        )
        return [tuple(row) for row in rows]


def test_rule_mode_writes_one_issue_per_rename(seeded: PipelineDb) -> None:
    run_id = uuid.uuid4()
    result = match_names(run_id, seeded.etl, seeded.app, None)

    assert (result.candidates, result.issues_written, result.judged) == (2, 2, 0)
    assert result.llm_calls == 0
    found = issues(seeded.owner)
    assert set(found) == {KEY, KEY_2}
    first = found[KEY]
    assert (first.type, first.severity, first.status) == (
        templates.CAMPAIGN_RENAMED,
        "medium",
        "open",
    )
    assert (first.entity_type, first.entity_id, first.period) == (
        "campaign",
        "c1",
        date(2026, 6, 1),
    )
    assert first.amount == Decimal("30.00")  # Days 15-17 under the new name.
    assert first.proposed_action == templates.RENAME_CHECK_ACTION
    assert f'"{RENAMED}" from 2026-06-15' in first.explanation
    assert "Model note" not in first.explanation
    assert first.run_id == run_id and first.client_id is not None


def test_no_alias_row_is_written_or_changed(seeded: PipelineDb) -> None:
    before = aliases(seeded.owner)
    assert {row[2] for row in before} == {"rule"}
    client, _ = make_client([answer(True, "Same."), answer(True, "Same.")])
    match_names(uuid.uuid4(), seeded.etl, seeded.app, client)
    match_names(uuid.uuid4(), seeded.etl, seeded.app, None)
    assert aliases(seeded.owner) == before


def test_llm_mode_proposes_both_actions(seeded: PipelineDb) -> None:
    client, fake = make_client(
        [answer(True, "Same brand and month."), answer(False, "A different promotion.")]
    )
    result = match_names(uuid.uuid4(), seeded.etl, seeded.app, client)

    assert len(fake.requests) == 2 and result.judged == 2
    assert result.llm_calls == 2 and result.llm_cost_eur > 0 and result.llm_tokens > 0
    found = issues(seeded.owner)
    assert found[KEY].proposed_action == templates.RENAME_CONFIRM_ACTION
    assert found[KEY_2].proposed_action == templates.RENAME_CHECK_ACTION
    assert 'Model note (unverified): "Same brand and month."' in found[KEY].explanation
    assert 'Model note (unverified): "A different promotion."' in found[KEY_2].explanation


def test_rerun_moves_only_the_run_id(seeded: PipelineDb) -> None:
    client, _ = make_client([answer(True, "Same."), answer(False, "Different.")])
    match_names(uuid.uuid4(), seeded.etl, seeded.app, client)
    with seeded.owner.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO app.users (email, role) VALUES (:e, 'finance') RETURNING id"),
            {"e": f"{uuid.uuid4().hex}@agency.example"},
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE app.issues SET status = 'approved', decided_by = :u, "
                "decided_at = '2026-07-02T08:00:00+00', decision_note = 'ok' "
                "WHERE dedup_key = :k"
            ),
            {"u": user_id, "k": KEY},
        )
    before = issues(seeded.owner)

    second_run = uuid.uuid4()
    client, fake = make_client([])
    result = match_names(second_run, seeded.etl, seeded.app, client)

    assert fake.requests == [] and result.llm_calls == 0
    assert (result.already_known, result.judged, result.issues_written) == (2, 0, 2)
    after = issues(seeded.owner)
    assert set(after) == set(before)
    for key, old in before.items():
        new = after[key]._asdict()
        assert new.pop("run_id") == second_run
        assert new == {k: v for k, v in old._asdict().items() if k != "run_id"}
    assert after[KEY].status == "approved" and after[KEY].decision_note == "ok"
    assert after[KEY].proposed_action == templates.RENAME_CONFIRM_ACTION


def test_rule_mode_rerun_does_not_overwrite_a_model_note(seeded: PipelineDb) -> None:
    client, _ = make_client([answer(True, "Same."), answer(True, "Same.")])
    match_names(uuid.uuid4(), seeded.etl, seeded.app, client)
    before = issues(seeded.owner)
    match_names(uuid.uuid4(), seeded.etl, seeded.app, None)
    after = issues(seeded.owner)
    assert [r.explanation for r in after.values()] == [r.explanation for r in before.values()]


def test_confirmed_alias_is_not_raised_again(seeded: PipelineDb) -> None:
    with seeded.owner.begin() as conn:
        conn.execute(
            text("UPDATE core.campaign_aliases SET source = 'human' WHERE alias = :a"),
            {"a": RENAMED},
        )
    result = match_names(uuid.uuid4(), seeded.etl, seeded.app, None)
    assert result.candidates == 1 and set(issues(seeded.owner)) == {KEY_2}


def test_cost_cap_still_writes_every_rename_and_makes_the_run_partial(seeded: PipelineDb) -> None:
    client, fake = make_client([answer(True, "Same.")], run_cap="0.000001")
    ctx = RunContext(
        run_id=uuid.uuid4(),
        data_dir=Path("unused"),
        etl=seeded.etl,
        app=seeded.app,
        client=None,  # type: ignore[arg-type]
        ingest_settings=None,  # type: ignore[arg-type]
        agency_timezone=TZ,
        name_settings=NameSettings(_env_file=None, judge=LLM),
        llm_client_factory=lambda _: client,
    )
    counts = runner.step_match_names(ctx)

    assert fake.requests == []
    assert counts["skipped_cost_cap"] == 2 and counts["issues_written"] == 2
    assert run_status([{"name": "match_names", "status": "succeeded", "counts": counts}]) == PARTIAL
    for row in issues(seeded.owner).values():
        assert row.proposed_action == templates.RENAME_CHECK_ACTION
        assert row.explanation.endswith(" No model judgment (model:skipped_cost_cap).")


def test_etl_cannot_take_the_issue_side(seeded: PipelineDb) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        match_names(uuid.uuid4(), seeded.etl, seeded.etl, None)
    assert issues(seeded.owner) == {}


# --- step wiring, without the database -----------------------------------------------------


def stub_context(judge: str) -> RunContext:
    def refuse(ctx: RunContext) -> LLMClient:
        raise AssertionError("an LLM client was built")

    return RunContext(
        run_id=uuid.uuid4(),
        data_dir=Path("unused"),
        etl=None,  # type: ignore[arg-type]
        app=None,  # type: ignore[arg-type]
        client=None,  # type: ignore[arg-type]
        ingest_settings=None,  # type: ignore[arg-type]
        agency_timezone=TZ,
        name_settings=NameSettings(_env_file=None, judge=judge),
        llm_client_factory=refuse,
    )


def test_name_judge_defaults_to_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAME_JUDGE", raising=False)
    assert NameSettings(_env_file=None).judge == RULE


def test_rule_mode_never_builds_an_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(runner, "match_names", lambda *args: calls.append(args[-1]) or Stub())
    counts = runner.step_match_names(stub_context(RULE))
    assert calls == [None] and counts["llm_cost_eur"] == "0"


def test_llm_mode_builds_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "match_names", lambda *args: pytest.fail("ran without a client"))
    with pytest.raises(AssertionError, match="an LLM client was built"):
        runner.step_match_names(stub_context(LLM))


def test_the_run_builds_one_client_for_every_step() -> None:
    built: list[object] = []
    ctx = replace(stub_context(LLM), llm_client_factory=lambda _: built.append(1) or "client")
    assert ctx.llm_client() == ctx.llm_client() == "client"
    assert built == [1]


class Stub:
    candidates = already_known = judged = issues_written = 0
    skipped_cost_cap = skipped_provider_error = llm_tokens = llm_calls = 0
    llm_cost_eur = Decimal(0)
