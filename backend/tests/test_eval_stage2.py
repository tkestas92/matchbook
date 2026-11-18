"""Stage 2 eval: #3 and #10 matching, the stage-2 checks, the results file and the run.

Hand-built answer keys, documents and extraction state only; the real data/generated, answer
key and state file are never read, and no model is ever called (decision 049).
"""

import re
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.demo.eval_results import parse_results
from app.extraction.schema import EXTRACTED, NEEDS_REVIEW, REJECTED
from app.reconciliation import templates
from app.reconciliation.models import RenameCandidate, RenameVerdict
from sqlalchemy import Engine, text
from tests import test_eval_stage1 as stage1_tests
from tests.test_eval_extraction import INVOICE_TRUTH, write_document
from tests.test_eval_stage1 import (
    JUNE,
    EvalDb,
    World,
    entry,
    issue,
    load_key,
    planted,
    seed_issue,
    seed_run,
    seed_world,
)

from evals import stage1 as s1
from evals.extraction import StateHeader, StateWriter, discover_documents, read_state
from evals.extraction_scoring import BENIGN, MALICIOUS, DocumentScore, LineScore, Usage
from evals.stage2 import (
    ACCEPTED_TYPES,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_SETUP,
    MODEL_CHECK,
    MODEL_CONFIRM,
    NO_JUDGMENT,
    REJECTED_CARD,
    RULE_ONLY,
    STAGE,
    DocumentCard,
    ExtractionCoverage,
    ExtractionFiles,
    RenameProposal,
    Stage2Input,
    checks,
    coverage,
    document_matcher,
    evaluate,
    evaluated_issues,
    file_hashes,
    match_stage2,
    passed,
    rename_outcome,
    rename_outcomes,
    render_results,
    stage1_passed,
)

# The stage-1 eval's throwaway database, shared as fixtures.
module_eval_db = stage1_tests.module_eval_db
eval_db = stage1_tests.eval_db

NEEDS_REVIEW_CARD = "extraction_needs_review"
COPY_NUMBER = "TEST-0009"
SHA_COPY = "a" * 64
SHA_OTHER = "b" * 64
HEADER = StateHeader(
    model="test-model", prompt_hash="abc", seed=7, threshold="0.80", vendors=("Meta",)
)


def injection(entity_ref: str = COPY_NUMBER):  # noqa: ANN201
    return planted(10, entity_ref, error_type="prompt_injection", stage=2)


def card(issue_id: int, sha: str, issue_type: str = REJECTED_CARD, earlier: bool = False):  # noqa: ANN201
    return DocumentCard(
        issue=issue(issue_id, issue_type, client=None, platform=None, month=None),
        sha256=sha,
        earlier_run=earlier,
    )


def stage2_input(
    planted_errors: list,
    issues: list,
    cards: tuple[DocumentCard, ...] = (),
    *,
    in_core: int = 0,
    renames: dict[int, RenameProposal] | None = None,
) -> Stage2Input:
    base = s1.EvalInput(
        run=s1.RunInfo(uuid.uuid4(), "succeeded", datetime(2026, 11, 1, tzinfo=UTC)),
        planted=tuple(planted_errors),
        issues=evaluated_issues(issues, cards),
        seeds=(7,),
        other_run_issues=0,
    )
    return Stage2Input(
        base=base,
        cards=cards,
        hash_by_number={COPY_NUMBER: SHA_COPY},
        malicious_in_core=in_core,
        renames=renames or {},
        run_cost_eur=Decimal("0.012345"),
    )


def doc_score(path: str, sha: str, group: str, status: str, calls: int = 0) -> DocumentScore:
    return DocumentScore(
        path=path,
        sha256=sha,
        group=group,
        status=status,
        reasons=(),
        fields={},
        lines=LineScore(),
        model_calls=calls,
        usage=Usage(),
    )


def complete_extraction(malicious_status: str = REJECTED, calls: int = 0) -> ExtractionCoverage:
    scores = (
        doc_score("invoices/a.pdf", SHA_OTHER, BENIGN, EXTRACTED, calls=1),
        doc_score("inbox/b.pdf", SHA_COPY, MALICIOUS, malicious_status, calls=calls),
    )
    return ExtractionCoverage(scores=scores, documents=2, missing=0, stale=False)


def by_name(data: Stage2Input, extraction: ExtractionCoverage) -> dict[str, bool]:
    result = match_stage2(data)
    return {c.name: c.ok for c in checks(data, result, extraction, 0)}


# --- matching -----------------------------------------------------------------------------


def test_rename_is_must_find_in_stage_2_only() -> None:
    renamed = planted(3, "ACME-META-2606", stage=2, campaign="c7")
    assert len(s1.match([renamed], [], ACCEPTED_TYPES, 1).expected_missed) == 1
    at_stage_2 = s1.match([renamed], [], ACCEPTED_TYPES, STAGE)
    assert len(at_stage_2.missed) == 1 and not s1.passed(at_stage_2, 0)


def test_rename_matches_on_the_platform_campaign_at_stage_2() -> None:
    renamed = planted(3, "ACME-META-2606", stage=2, campaign="c7")
    data = stage2_input([renamed], [issue(1, templates.CAMPAIGN_RENAMED, entity_id="c7")])
    result = match_stage2(data)
    assert len(result.found) == 1 and not result.missed and not result.false_alarms
    other = stage2_input([renamed], [issue(1, templates.CAMPAIGN_RENAMED, entity_id="c8")])
    assert len(match_stage2(other).missed) == 1


def test_injection_is_found_by_the_rejected_card_of_its_own_file() -> None:
    result = match_stage2(stage2_input([injection()], [], (card(5, SHA_COPY),)))
    assert [i.id for _, i in result.found] == [5]
    assert not result.missed and not result.false_alarms


@pytest.mark.parametrize(
    "cards",
    [(card(5, SHA_OTHER),), (card(5, SHA_COPY, NEEDS_REVIEW_CARD),)],
    ids=["another file", "not rejected"],
)
def test_injection_without_its_rejected_card_is_missed(cards: tuple[DocumentCard, ...]) -> None:
    result = match_stage2(stage2_input([injection()], [], cards))
    assert [p.entity_ref for p in result.missed] == [COPY_NUMBER]
    assert len(result.false_alarms) == 1


def test_document_matcher_keeps_stage_1_rules_for_other_errors() -> None:
    matcher = document_matcher({COPY_NUMBER: SHA_COPY}, {})
    overcharge = planted(1, "INV-1")
    types = ACCEPTED_TYPES[1]
    assert matcher(overcharge, issue(1, "reconciliation_overcharge"), types)
    assert not matcher(overcharge, issue(1, "reconciliation_overcharge", month=None), types)


def test_latest_run_cards_are_replaced_by_the_current_files_cards() -> None:
    latest = [issue(1, "reconciliation_overcharge"), issue(2, REJECTED_CARD)]
    issues = evaluated_issues(latest, (card(3, SHA_COPY, earlier=True),))
    assert [i.id for i in issues] == [1, 3]


# --- extraction state ---------------------------------------------------------------------


def documents(tmp_path: Path) -> list:
    write_document(tmp_path, "invoices/meta/2026-05/TEST-0001.pdf", INVOICE_TRUTH, b"benign")
    copy = {**INVOICE_TRUTH, "number": COPY_NUMBER}
    write_document(tmp_path, f"inbox/meta/2026-05/{COPY_NUMBER}.pdf", copy, b"malicious")
    return discover_documents(tmp_path)


def saved_scores(docs: list, hashes: dict[str, str], **overrides: str) -> list[DocumentScore]:
    scores = []
    for doc in docs:
        group = MALICIOUS if doc.in_inbox else BENIGN
        status = REJECTED if doc.in_inbox else EXTRACTED
        sha = overrides.get(doc.path, hashes[doc.path])
        scores.append(doc_score(doc.path, sha, group, status, calls=0 if doc.in_inbox else 1))
    return scores


def write_state(path: Path, scores: list[DocumentScore], header: StateHeader = HEADER) -> Path:
    writer = StateWriter(path, "2026-11")
    writer.start(header, {})
    for score in scores:
        writer.append(score)
    return path


def read_coverage(tmp_path: Path, scores: list[DocumentScore]) -> ExtractionCoverage:
    docs = discover_documents(tmp_path / "data")
    saved = read_state(write_state(tmp_path / "state.jsonl", scores), HEADER)
    return coverage(docs, file_hashes(docs), saved, frozenset({COPY_NUMBER}))


def test_saved_scores_for_the_current_bytes_are_complete(tmp_path: Path) -> None:
    docs = documents(tmp_path / "data")
    state = read_coverage(tmp_path, saved_scores(docs, file_hashes(docs)))
    assert state.complete and state.summary.malicious_rejected_without_call == 1


def test_a_changed_file_or_a_wrong_group_is_missing(tmp_path: Path) -> None:
    docs = documents(tmp_path / "data")
    hashes = file_hashes(docs)
    changed = saved_scores(docs, hashes, **{docs[0].path: SHA_OTHER})
    assert read_coverage(tmp_path, changed).missing == 1
    as_benign = [doc_score(s.path, s.sha256, BENIGN, s.status) for s in saved_scores(docs, hashes)]
    assert read_coverage(tmp_path, as_benign).missing == 1


def test_state_for_another_header_is_missing_everything(tmp_path: Path) -> None:
    docs = documents(tmp_path / "data")
    hashes = file_hashes(docs)
    path = write_state(tmp_path / "state.jsonl", saved_scores(docs, hashes))
    other = replace(HEADER, model="other-model")
    state = coverage(docs, hashes, read_state(path, other), frozenset({COPY_NUMBER}))
    assert state.stale and state.missing == 2 and not state.complete


# --- checks -------------------------------------------------------------------------------


def found_injection(**kwargs) -> Stage2Input:  # noqa: ANN003
    return stage2_input([injection()], [], (card(5, SHA_COPY),), **kwargs)


def test_every_check_passes_when_everything_holds() -> None:
    assert all(by_name(found_injection(), complete_extraction()).values())


def test_a_malicious_number_in_core_fails() -> None:
    data = found_injection(in_core=1)
    results = by_name(data, complete_extraction())
    assert not results["Malicious invoice numbers in `core.invoices`"]
    assert not passed(checks(data, match_stage2(data), complete_extraction(), 0))


def test_incomplete_extraction_state_fails() -> None:
    partial = ExtractionCoverage(scores=(), documents=2, missing=2, stale=True)
    results = by_name(found_injection(), partial)
    assert not results["Extraction eval scored every current document"]
    assert not results["Extraction eval: every malicious copy rejected with no model call"]


def test_malicious_copy_that_reached_the_model_fails() -> None:
    results = by_name(found_injection(), complete_extraction(NEEDS_REVIEW, calls=1))
    assert not results["Extraction eval: every malicious copy rejected with no model call"]


def test_extraction_accuracy_is_reported_not_gated() -> None:
    exact = doc_score("invoices/a.pdf", SHA_OTHER, BENIGN, EXTRACTED, calls=1)
    wrong = replace(exact, lines=LineScore(expected=1, extracted=1))
    scores = (wrong, complete_extraction().scores[1])
    state = ExtractionCoverage(scores=scores, documents=2, missing=0, stale=False)
    assert state.summary.exact_match == 0
    assert all(by_name(found_injection(), state).values())


def test_stage_1_gate_ignores_stage_2_misses() -> None:
    result = match_stage2(stage2_input([injection()], []))
    assert stage1_passed(result, 0)
    assert not by_name(stage2_input([injection()], []), complete_extraction())[
        "Malicious invoices rejected by the pipeline"
    ]


# --- rename judge -------------------------------------------------------------------------

CANDIDATE = RenameCandidate(
    platform="meta",
    platform_campaign_id="c7",
    client="ACME",
    period=JUNE,
    old_name="Summer [ACME-META-2606]",
    new_names=("Summer sale",),
    renamed_from=date(2026, 6, 15),
    amount=Decimal("12.34"),
)


def proposal(verdict: RenameVerdict) -> RenameProposal:
    return RenameProposal(verdict.action, templates.rename_explanation(CANDIDATE, verdict))


@pytest.mark.parametrize(
    ("verdict", "outcome"),
    [
        (RenameVerdict(templates.RENAME_CONFIRM_ACTION, note="same campaign"), MODEL_CONFIRM),
        (RenameVerdict(templates.RENAME_CHECK_ACTION, note="unsure"), MODEL_CHECK),
        (RenameVerdict(templates.RENAME_CHECK_ACTION, codes=("model:invalid",)), NO_JUDGMENT),
        (RenameVerdict(templates.RENAME_CHECK_ACTION), RULE_ONLY),
    ],
)
def test_rename_outcome_reads_what_the_pipeline_wrote(verdict: RenameVerdict, outcome: str) -> None:
    assert rename_outcome(proposal(verdict)) == outcome


def test_rename_outcomes_count_found_renames_only() -> None:
    renamed = planted(3, "ACME-META-2606", stage=2, campaign="c7")
    issues = [
        issue(1, templates.CAMPAIGN_RENAMED, entity_id="c7"),
        issue(2, templates.CAMPAIGN_RENAMED, entity_id="c9"),
    ]
    confirm = proposal(RenameVerdict(templates.RENAME_CONFIRM_ACTION, note="same"))
    data = stage2_input([renamed], issues, renames={1: confirm, 2: confirm})
    assert rename_outcomes(match_stage2(data), data.renames) == Counter({MODEL_CONFIRM: 1})


# --- results file -------------------------------------------------------------------------


def rendered(data: Stage2Input, extraction: ExtractionCoverage) -> str:
    result = match_stage2(data)
    return render_results(data, result, checks(data, result, extraction, 0), extraction, HEADER, 0)


def test_results_file_parses_for_the_demo_and_has_one_verdict() -> None:
    data = found_injection()
    text = rendered(data, complete_extraction())
    parsed = parse_results(text)
    assert parsed.result == "PASS"
    assert parsed.overall.found == 1 and parsed.overall.missed == 0
    assert len(re.findall(r"^- Result:", text, re.MULTILINE)) == 1
    assert "- Stage 1 gate: PASS" in text and "- Stage 2 gate: PASS" in text
    assert "EUR 0.012345" in text


def test_results_file_holds_no_entities_numbers_paths_or_hashes() -> None:
    text = rendered(found_injection(), complete_extraction())
    for secret in (COPY_NUMBER, SHA_COPY, SHA_OTHER, "inbox/", "invoices/", "ACME"):
        assert secret not in text


def test_results_file_says_how_to_get_missing_extraction_scores() -> None:
    partial = ExtractionCoverage(scores=(), documents=2, missing=2, stale=False)
    text = rendered(found_injection(), partial)
    assert "Result: **FAIL**" in text
    assert "Not available: 2 of 2" in text and "costs money" in text


def test_stage_2_eval_never_builds_a_model_client() -> None:
    source = (Path(__file__).resolve().parents[2] / "evals" / "stage2.py").read_text("utf-8")
    for name in ("create_llm_client", "LLMClient", "complete_json", "extract_invoice", "gemini"):
        assert not re.search(rf"\b{name}\b", source), name


# --- database: the eval as eval_runner ----------------------------------------------------


def seed_card(engine: Engine, run_id: uuid.UUID, sha: str, issue_type: str = REJECTED_CARD) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app.issues (dedup_key, type, severity, entity_type, entity_id, "
                "explanation, run_id) VALUES (:k, :t, 'high', 'document', 'inbox/x.pdf', 'x', :r)"
            ),
            {"k": f"extraction:{sha}", "t": issue_type, "r": run_id},
        )


def seed_core_invoice(engine: Engine, world: World, number: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO core.invoices (kind, vendor, number, platform_id, period_start, "
                "period_end, total, run_id) "
                "VALUES ('platform', 'Meta', :n, :p, '2026-05-01', '2026-05-31', 1.00, :r)"
            ),
            {"n": number, "p": world.platform_id, "r": uuid.uuid4()},
        )


INJECTION_ENTRY = entry(
    10, COPY_NUMBER, error_type="prompt_injection", detectable_in_stage=2, details={"variant": "x"}
)


def prepared(eval_db: EvalDb, tmp_path: Path, with_state: bool = True) -> ExtractionFiles:
    """A data dir with one benign invoice and one malicious copy, the key and the saved state."""
    docs = documents(tmp_path / "data")
    load_key(eval_db.owner, tmp_path, [INJECTION_ENTRY])
    state = tmp_path / "state.jsonl"
    if with_state:
        write_state(state, saved_scores(docs, file_hashes(docs)))
    return ExtractionFiles(tmp_path / "data", state, HEADER)


def copy_sha(files: ExtractionFiles) -> str:
    docs = discover_documents(files.data_dir)
    return next(sha for path, sha in file_hashes(docs).items() if path.startswith("inbox/"))


def run_eval(eval_db: EvalDb, files: ExtractionFiles, results: Path) -> int:
    return evaluate(eval_db.eval_runner, files, results, 0)


def test_eval_passes_and_writes_a_counts_only_file(eval_db: EvalDb, tmp_path: Path) -> None:
    files = prepared(eval_db, tmp_path)
    run_id = seed_run(eval_db.owner)
    seed_card(eval_db.owner, run_id, copy_sha(files))
    results = tmp_path / "eval-results.md"
    assert run_eval(eval_db, files, results) == EXIT_PASS
    text_out = results.read_text(encoding="utf-8")
    assert parse_results(text_out).result == "PASS"
    assert "| #10 prompt_injection | 1 | 1 | 0 | 0 | 0 |" in text_out
    assert COPY_NUMBER not in text_out and copy_sha(files) not in text_out


def test_a_card_from_an_earlier_run_counts_for_a_current_file(
    eval_db: EvalDb, tmp_path: Path
) -> None:
    """The pipeline skips a file that already has a card, so the card keeps its first run."""
    files = prepared(eval_db, tmp_path)
    earlier = seed_run(eval_db.owner, hour=1)
    seed_run(eval_db.owner, hour=2)
    seed_card(eval_db.owner, earlier, copy_sha(files))
    results = tmp_path / "eval-results.md"
    assert run_eval(eval_db, files, results) == EXIT_PASS
    assert "carried over from earlier runs: 1" in results.read_text(encoding="utf-8")


def test_a_card_for_a_file_that_is_gone_is_not_evaluated(
    eval_db: EvalDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = prepared(eval_db, tmp_path)
    earlier = seed_run(eval_db.owner, hour=1)
    seed_run(eval_db.owner, hour=2)
    seed_card(eval_db.owner, earlier, SHA_OTHER)
    assert run_eval(eval_db, files, tmp_path / "eval-results.md") == EXIT_FAIL
    assert f"missed: #10 prompt_injection {COPY_NUMBER}" in capsys.readouterr().out


def test_a_malicious_number_in_core_fails_the_eval(eval_db: EvalDb, tmp_path: Path) -> None:
    files = prepared(eval_db, tmp_path)
    world = seed_world(eval_db.owner)
    run_id = seed_run(eval_db.owner)
    seed_card(eval_db.owner, run_id, copy_sha(files))
    seed_core_invoice(eval_db.owner, world, COPY_NUMBER)
    results = tmp_path / "eval-results.md"
    assert run_eval(eval_db, files, results) == EXIT_FAIL
    assert "| Malicious invoice numbers in `core.invoices` | FAIL | 1 |" in results.read_text(
        encoding="utf-8"
    )


def test_missing_extraction_state_fails_the_eval(eval_db: EvalDb, tmp_path: Path) -> None:
    files = prepared(eval_db, tmp_path, with_state=False)
    run_id = seed_run(eval_db.owner)
    seed_card(eval_db.owner, run_id, copy_sha(files))
    results = tmp_path / "eval-results.md"
    assert run_eval(eval_db, files, results) == EXIT_FAIL
    assert "Not available: 2 of 2" in results.read_text(encoding="utf-8")


def test_other_issues_of_the_latest_run_still_count(eval_db: EvalDb, tmp_path: Path) -> None:
    files = prepared(eval_db, tmp_path)
    world = seed_world(eval_db.owner)
    run_id = seed_run(eval_db.owner)
    seed_card(eval_db.owner, run_id, copy_sha(files))
    seed_issue(eval_db.owner, world, run_id, "reconciliation_budget_overspend")
    assert run_eval(eval_db, files, tmp_path / "eval-results.md") == EXIT_FAIL


def test_eval_cannot_run_without_an_answer_key(eval_db: EvalDb, tmp_path: Path) -> None:
    documents(tmp_path / "data")
    seed_run(eval_db.owner)
    files = ExtractionFiles(tmp_path / "data", tmp_path / "state.jsonl", HEADER)
    assert run_eval(eval_db, files, tmp_path / "eval-results.md") == EXIT_SETUP
    assert not (tmp_path / "eval-results.md").exists()
