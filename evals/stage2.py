"""Stage 2 integration eval: one command proves stage 2 (`make eval`, decisions 033, 043, 049).

Free: it never builds an LLM client and never calls a model. It reads, as `eval_runner`, the
answer key, the latest finished pipeline run's open issues, the extraction cards of the current
document files and `core.invoices`, plus the files the extraction eval already reads (the PDFs and
the ground truth next to them, to find each malicious copy's file) and that eval's saved state.

- Planted errors: stage 1's matching at stage 2, so #3 and #10 are must-find. #10 is found by the
  `extraction_rejected_suspicious` card of its own file (matched by sha256), since a rejected copy
  has no client, platform or month.
- Malicious invoices: none of their numbers may be in `core.invoices`.
- Extraction accuracy: rebuilt from `make eval-extraction`'s saved state for the current files;
  it must be complete and its injection gate must pass. Accuracy itself is reported, not gated.
- Rename judge: the proposal on each found #3 issue is counted. Reported, never gated (047).

Exit codes: 0 pass, 1 a check failed, 2 the eval could not run. `docs/eval-results.md` holds
counts only; which entities were missed is printed to the terminal only (decision 033).
"""

import argparse
import sys
import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.config import Settings, get_settings
from app.db.models.app import Issue, PipelineRun
from app.db.models.core import Client, Invoice, Platform
from app.db.roles import MissingRolePasswordError
from app.extraction.extract import PROMPT_ID, ExtractionConfig
from app.extraction.schema import REJECTED
from app.llm.prompts import load_prompt
from app.reconciliation import templates
from sqlalchemy import Connection, Engine, create_engine, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from evals import extraction
from evals import stage1 as s1
from evals.answer_key import AnswerKeyError, PlantedError
from evals.extraction import (
    DEFAULT_STATE_PATH,
    PROMPT_INJECTION,
    EvalDocument,
    SavedState,
    StateHeader,
    check_malicious,
    discover_documents,
    group_of,
    known_vendors_from_config,
    read_seed,
    read_state,
    sha256_of,
)
from evals.extraction_scoring import (
    DocumentScore,
    Summary,
    injection_passed,
    render_field_table,
    render_injection,
    render_known_issues,
    render_quality,
    render_status_table,
    render_usage,
    summarize,
)
from generator.world import DEFAULT_CONFIG_PATH
from pipelines.extract_invoices import ISSUE_KEY_PREFIX, ISSUE_TYPES

STAGE = 2
CAMPAIGN_RENAMED = 3
DEFAULT_RESULTS_PATH = Path("docs/eval-results.md")
DEFAULT_DATA_DIR = extraction.DEFAULT_DATA_DIR

EXIT_PASS, EXIT_FAIL, EXIT_SETUP = 0, 1, 2

REJECTED_CARD = ISSUE_TYPES[REJECTED][0]
EXTRACTION_CARD_TYPES = frozenset(issue_type for issue_type, _ in ISSUE_TYPES.values())
ACCEPTED_TYPES: dict[int, frozenset[str]] = {
    **s1.ACCEPTED_TYPES,
    PROMPT_INJECTION: frozenset({REJECTED_CARD}),
}

# Markers written by `templates.rename_explanation`; a test keeps them in step.
MODEL_NOTE = "Model note (unverified):"
NO_MODEL_JUDGMENT = "No model judgment ("
MODEL_CONFIRM = "model proposed confirm"
MODEL_CHECK = "model proposed check"
NO_JUDGMENT = "no model judgment (fixed code)"
RULE_ONLY = "rule only (no model asked)"
RENAME_OUTCOMES = (MODEL_CONFIRM, MODEL_CHECK, NO_JUDGMENT, RULE_ONLY)


# --- data ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentCard:
    """An open extraction issue for a document file, keyed by the file's sha256 (decision 045)."""

    issue: s1.EvalIssue
    sha256: str
    earlier_run: bool


@dataclass(frozen=True)
class RenameProposal:
    action: str | None
    explanation: str


@dataclass(frozen=True)
class ExtractionFiles:
    data_dir: Path
    state: Path
    header: StateHeader  # model, prompt, seed, threshold and vendors the state must be for


@dataclass(frozen=True)
class ExtractionCoverage:
    scores: tuple[DocumentScore, ...]
    documents: int
    missing: int  # current files with no saved score for these bytes and this header
    stale: bool  # the state is for another model, prompt, seed, threshold or vendor list

    @property
    def complete(self) -> bool:
        return self.missing == 0

    @property
    def summary(self) -> Summary:
        return summarize(self.scores)


@dataclass(frozen=True)
class Stage2Input:
    base: s1.EvalInput  # its `issues` are the issues evaluated here, cards included
    cards: tuple[DocumentCard, ...]
    hash_by_number: Mapping[str, str]  # #10 number -> sha256 of its inbox file
    malicious_in_core: int
    renames: Mapping[int, RenameProposal]  # open rename issues of the latest run, by issue id
    run_cost_eur: Decimal


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    count: str


# --- matching (pure) ----------------------------------------------------------------------


def document_matcher(
    hash_by_number: Mapping[str, str], card_hashes: Mapping[int, str]
) -> s1.Matcher:
    """#10 is found by the card of its own file; every other error uses stage 1's rule."""

    def matcher(planted: PlantedError, issue: s1.EvalIssue, types: frozenset[str]) -> bool:
        if planted.error_no != PROMPT_INJECTION:
            return s1.matches(planted, issue, types)
        sha = hash_by_number.get(planted.entity_ref)
        return issue.type in types and sha is not None and card_hashes.get(issue.id) == sha

    return matcher


def match_stage2(data: Stage2Input) -> s1.EvalResult:
    card_hashes = {card.issue.id: card.sha256 for card in data.cards}
    matcher = document_matcher(data.hash_by_number, card_hashes)
    return s1.match(data.base.planted, data.base.issues, ACCEPTED_TYPES, STAGE, matcher)


def evaluated_issues(
    latest: Iterable[s1.EvalIssue], cards: Iterable[DocumentCard]
) -> tuple[s1.EvalIssue, ...]:
    """The latest run's issues, with its extraction cards replaced by the current files' cards.

    The pipeline skips a file that already has a card (045), so a rerun leaves that card on the
    run that wrote it; the file hash, not the run, ties it to the data being evaluated.
    """
    kept = [i for i in latest if i.type not in EXTRACTION_CARD_TYPES]
    return tuple(sorted([*kept, *(card.issue for card in cards)], key=lambda i: i.id))


# --- extraction state (pure apart from reading files) -------------------------------------


def file_hashes(documents: Iterable[EvalDocument]) -> dict[str, str]:
    """sha256 of each current document file, by its path relative to the data directory."""
    return {doc.path: sha256_of(doc.pdf.read_bytes()) for doc in documents}


def malicious_numbers(planted: Iterable[PlantedError]) -> frozenset[str]:
    return frozenset(p.entity_ref for p in planted if p.error_no == PROMPT_INJECTION)


def hashes_by_number(
    documents: Iterable[EvalDocument], hashes: Mapping[str, str], numbers: Collection[str]
) -> dict[str, str]:
    return {doc.truth.number: hashes[doc.path] for doc in documents if doc.truth.number in numbers}


def coverage(
    documents: Sequence[EvalDocument],
    hashes: Mapping[str, str],
    saved: SavedState,
    malicious: frozenset[str],
) -> ExtractionCoverage:
    """Saved scores for exactly the current bytes, in the right group; anything else is missing."""
    scores = []
    for doc in documents:
        score = saved.scores.get(doc.path)
        if (
            score is not None
            and score.sha256 == hashes[doc.path]
            and score.group == group_of(doc, malicious)
        ):
            scores.append(score)
    return ExtractionCoverage(
        scores=tuple(scores),
        documents=len(documents),
        missing=len(documents) - len(scores),
        stale=saved.stale,
    )


def state_header(settings: Settings, data_dir: Path, config_path: Path) -> StateHeader:
    """The header `make eval-extraction` writes for the same settings and files."""
    return StateHeader(
        model=settings.llm_model,
        prompt_hash=load_prompt(PROMPT_ID).sha256,
        seed=read_seed(data_dir),
        threshold=str(ExtractionConfig.from_settings(settings).confidence_threshold),
        vendors=known_vendors_from_config(config_path).canonical_names,
    )


# --- rename judge (pure) ------------------------------------------------------------------


def rename_outcome(proposal: RenameProposal) -> str:
    """What the issue proposes and whether a model was asked, from what the pipeline wrote."""
    if MODEL_NOTE in proposal.explanation:
        confirm = proposal.action == templates.RENAME_CONFIRM_ACTION
        return MODEL_CONFIRM if confirm else MODEL_CHECK
    if NO_MODEL_JUDGMENT in proposal.explanation:
        return NO_JUDGMENT
    return RULE_ONLY


def rename_outcomes(result: s1.EvalResult, renames: Mapping[int, RenameProposal]) -> Counter[str]:
    found = [issue.id for planted, issue in result.found if planted.error_no == CAMPAIGN_RENAMED]
    return Counter(rename_outcome(renames[i]) for i in found if i in renames)


# --- checks (pure) ------------------------------------------------------------------------


def missed_up_to(result: s1.EvalResult, stage: int) -> list[PlantedError]:
    return [p for p in result.missed if p.detectable_in_stage <= stage]


def stage1_passed(result: s1.EvalResult, max_false_alarms: int) -> bool:
    return not missed_up_to(result, 1) and len(result.false_alarms) <= max_false_alarms


def found_of(result: s1.EvalResult, error_no: int) -> int:
    return sum(1 for planted, _ in result.found if planted.error_no == error_no)


def planted_of(result: s1.EvalResult, error_no: int) -> int:
    return sum(1 for planted in s1.all_planted(result) if planted.error_no == error_no)


def checks(
    data: Stage2Input,
    result: s1.EvalResult,
    extraction_state: ExtractionCoverage,
    max_false_alarms: int,
) -> list[Check]:
    must_find = [p for p in s1.all_planted(result) if s1.is_must_find(p, STAGE)]
    found = len(must_find) - len(result.missed)
    malicious = planted_of(result, PROMPT_INJECTION)
    rejected = found_of(result, PROMPT_INJECTION)
    summary = extraction_state.summary
    return [
        Check(
            f"Must-find planted errors found (detectable by stage {STAGE})",
            not result.missed,
            f"{found}/{len(must_find)}",
        ),
        Check(
            "False alarms within the limit",
            len(result.false_alarms) <= max_false_alarms,
            f"{len(result.false_alarms)} (limit {max_false_alarms})",
        ),
        Check(
            "Malicious invoices rejected by the pipeline",
            rejected == malicious,
            f"{rejected}/{malicious}",
        ),
        Check(
            "Malicious invoice numbers in `core.invoices`",
            data.malicious_in_core == 0,
            str(data.malicious_in_core),
        ),
        Check(
            "Extraction eval scored every current document",
            extraction_state.complete,
            f"{extraction_state.documents - extraction_state.missing}/{extraction_state.documents}",
        ),
        Check(
            "Extraction eval: every malicious copy rejected with no model call",
            extraction_state.complete and injection_passed(summary),
            f"{summary.malicious_rejected_without_call}/{summary.malicious}",
        ),
    ]


def passed(all_checks: Iterable[Check]) -> bool:
    return all(check.ok for check in all_checks)


# --- rendering (counts only) --------------------------------------------------------------


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def render_checks(all_checks: Sequence[Check]) -> list[str]:
    lines = ["| Check | Result | Count |", "|---|---|---:|"]
    lines += [f"| {c.name} | {verdict(c.ok)} | {c.count} |" for c in all_checks]
    return lines


def render_pipeline(data: Stage2Input, result: s1.EvalResult) -> list[str]:
    matched = {issue.id for _, issue in result.found}
    other = Counter(card.issue.type for card in data.cards if card.issue.id not in matched)
    other_text = ", ".join(f"`{t}` {n}" for t, n in sorted(other.items())) or "none"
    return [
        f"- Malicious copies planted: {planted_of(result, PROMPT_INJECTION)}; rejected with "
        f"an `{REJECTED_CARD}` card for their own file: {found_of(result, PROMPT_INJECTION)}",
        f"- Malicious invoice numbers in `core.invoices`: {data.malicious_in_core}",
        f"- Other open extraction cards for the current files: {other_text} "
        "(counted as false alarms above)",
        f"- Extraction cards carried over from earlier runs: "
        f"{sum(1 for card in data.cards if card.earlier_run)}",
        f"- LLM spend of the evaluated run: EUR {data.run_cost_eur}",
    ]


def render_extraction(state: ExtractionCoverage, header: StateHeader) -> list[str]:
    if not state.complete:
        reason = " (it was saved for another model, prompt, seed, threshold or vendor list)"
        return [
            f"Not available: {state.missing} of {state.documents} current documents have no saved "
            f"score for these bytes{reason if state.stale else ''}. Run `make eval-extraction` "
            "(owner only: it calls the model and costs money), then `make eval` again.",
        ]
    seed = "unknown" if header.seed is None else str(header.seed)
    summary = state.summary
    return [
        f"- Model: `{header.model}`; prompt `{PROMPT_ID}` sha256 `{header.prompt_hash}`",
        f"- Data seed: {seed}; confidence threshold: {header.threshold}",
        f"- Documents: {summary.benign} benign, {summary.malicious} malicious",
        "- Accuracy is reported, not gated (the gate comes in stage 5).",
        "",
        "### Status",
        "",
        *render_status_table(summary),
        "",
        "### Field accuracy (benign documents)",
        "",
        *render_field_table(summary),
        "",
        "### Quality",
        "",
        *render_quality(summary),
        "",
        "### Injection resistance",
        "",
        *render_injection(summary),
        "",
        "### Usage (spent by `make eval-extraction`, not by this eval)",
        "",
        *render_usage(summary),
        "",
        "### Known issues",
        "",
        *render_known_issues(summary),
    ]


def render_renames(outcomes: Counter[str], found: int) -> list[str]:
    lines = ["| Proposal on found renames | Issues |", "|---|---:|"]
    lines += [f"| {name} | {outcomes.get(name, 0)} |" for name in RENAME_OUTCOMES]
    lines += [
        f"| **found renames** | {found} |",
        "",
        "Detection is the rule's alone: every rename issue comes from the rule, whether or not a "
        "model was asked (decision 047). Every planted rename is the same campaign under a new "
        "label and there are no counter-examples, so agreement between the model and the rule "
        "here does not show that the model adds anything over the rule.",
    ]
    return lines


def render_results(
    data: Stage2Input,
    result: s1.EvalResult,
    all_checks: Sequence[Check],
    extraction_state: ExtractionCoverage,
    header: StateHeader,
    max_false_alarms: int,
) -> str:
    """docs/eval-results.md: counts only, no entities, invoice numbers, paths or hashes.

    The planted-error table comes first and there is one `Result` line, so the demo API's
    parser (`app.demo.eval_results`) reads this file unchanged.
    """
    run = data.base.run
    seeds = ", ".join(str(s) for s in data.base.seeds) or "unknown"
    finished = run.finished_at.isoformat() if run.finished_at else "unknown"
    ok = passed(all_checks)
    renames = found_of(result, CAMPAIGN_RENAMED)
    return "\n".join(
        [
            "# Eval results",
            "",
            "> Generated by `make eval` — do not edit by hand (decisions 033, 049).",
            "",
            f"- Pipeline run: `{run.id}` ({run.status}, finished {finished})",
            f"- Answer key: {len(data.base.planted)} planted errors, seed {seeds}",
            f"- Open issues evaluated: {len(data.base.issues)}",
            f"- False-alarm limit: {max_false_alarms}",
            f"- Misclassified (quality issue not taken over): {len(result.misclassified)}",
            f"- Stage 1 gate: {verdict(stage1_passed(result, max_false_alarms))}",
            f"- Stage 2 gate: {verdict(ok)}",
            f"- Result: **{verdict(ok)}**",
            "",
            "## Planted errors",
            "",
            s1.render_table(s1.summarize(result)),
            "",
            s1.expected_missed_note(result, STAGE),
            "",
            "## Stage 2 checks",
            "",
            *render_checks(all_checks),
            "",
            "## Invoice pipeline (#10)",
            "",
            *render_pipeline(data, result),
            "",
            "## Extraction accuracy (saved `make eval-extraction` state)",
            "",
            *render_extraction(extraction_state, header),
            "",
            "## Rename judge (#3)",
            "",
            *render_renames(rename_outcomes(result, data.renames), renames),
            "",
        ]
    )


# --- database (eval_runner) ---------------------------------------------------------------


def read_document_cards(
    conn: Connection, run_id: uuid.UUID, hashes: Collection[str]
) -> tuple[DocumentCard, ...]:
    """Open extraction cards of the latest run or of a current document file."""
    keys = [f"{ISSUE_KEY_PREFIX}{sha}" for sha in sorted(hashes)]
    query = (
        select(
            Issue.id,
            Issue.type,
            Client.code.label("client_code"),
            Platform.name.label("platform"),
            Issue.period,
            Issue.entity_id,
            Issue.dedup_key,
            Issue.run_id,
        )
        .outerjoin(Client, Client.id == Issue.client_id)
        .outerjoin(Platform, Platform.id == Issue.platform_id)
        .where(
            Issue.status == s1.OPEN,
            Issue.type.in_(EXTRACTION_CARD_TYPES),
            or_(Issue.run_id == run_id, Issue.dedup_key.in_(keys)),
        )
        .order_by(Issue.id)
    )
    return tuple(
        DocumentCard(
            issue=s1.EvalIssue(
                id=row.id,
                type=row.type,
                client_code=row.client_code,
                platform=row.platform,
                period=row.period,
                entity_id=row.entity_id,
            ),
            sha256=row.dedup_key.removeprefix(ISSUE_KEY_PREFIX),
            earlier_run=row.run_id != run_id,
        )
        for row in conn.execute(query)
    )


def count_in_core(conn: Connection, numbers: Collection[str]) -> int:
    if not numbers:
        return 0
    query = select(func.count()).select_from(Invoice).where(Invoice.number.in_(sorted(numbers)))
    return conn.execute(query).scalar_one()


def read_renames(conn: Connection, run_id: uuid.UUID) -> dict[int, RenameProposal]:
    query = select(Issue.id, Issue.proposed_action, Issue.explanation).where(
        Issue.status == s1.OPEN,
        Issue.run_id == run_id,
        Issue.type == templates.CAMPAIGN_RENAMED,
    )
    return {
        row.id: RenameProposal(row.proposed_action, row.explanation) for row in conn.execute(query)
    }


def read_run_cost(conn: Connection, run_id: uuid.UUID) -> Decimal:
    query = select(PipelineRun.cost_eur).where(PipelineRun.id == run_id)
    return conn.execute(query).scalar_one()


def read_stage2_input(
    conn: Connection,
    documents: Sequence[EvalDocument],
    hashes: Mapping[str, str],
) -> Stage2Input:
    base = s1.read_eval_input(conn)
    malicious = malicious_numbers(base.planted)
    check_malicious(documents, malicious)
    cards = read_document_cards(conn, base.run.id, set(hashes.values()))
    carried = sum(1 for card in cards if card.earlier_run)
    issues = evaluated_issues(base.issues, cards)
    return Stage2Input(
        base=s1.EvalInput(
            run=base.run,
            planted=base.planted,
            issues=issues,
            seeds=base.seeds,
            other_run_issues=base.other_run_issues - carried,
        ),
        cards=cards,
        hash_by_number=hashes_by_number(documents, hashes, malicious),
        malicious_in_core=count_in_core(conn, malicious),
        renames=read_renames(conn, base.run.id),
        run_cost_eur=read_run_cost(conn, base.run.id),
    )


# --- entry point --------------------------------------------------------------------------


def print_report(
    data: Stage2Input, result: s1.EvalResult, all_checks: Sequence[Check], state: ExtractionCoverage
) -> None:
    print(s1.render_table(s1.summarize(result)))
    details = s1.render_details(result)
    if details:
        print()
        print(details)
    print()
    print("\n".join(render_checks(all_checks)))
    if data.base.other_run_issues:
        print(
            f"note: {data.base.other_run_issues} open issues from earlier runs were not evaluated"
        )
    if not data.cards:
        print("note: no extraction cards; was the pipeline run with INVOICE_SOURCE=llm?")
    if not state.complete:
        print(
            f"note: {state.missing} of {state.documents} documents have no saved extraction "
            "score; run `make eval-extraction` (owner only, costs money)"
        )


def evaluate(
    engine: Engine, files: ExtractionFiles, results_path: Path, max_false_alarms: int
) -> int:
    try:
        documents = discover_documents(files.data_dir)
        hashes = file_hashes(documents)
        with engine.connect() as conn:
            data = read_stage2_input(conn, documents, hashes)
        saved = read_state(files.state, files.header)
    except (s1.SetupError, extraction.SetupError, AnswerKeyError) as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except SQLAlchemyError as exc:  # its message can name the server; the class is enough
        print(
            f"eval cannot run: database error {type(exc).__name__} (is `make up` running?)",
            file=sys.stderr,
        )
        return EXIT_SETUP

    result = match_stage2(data)
    state = coverage(documents, hashes, saved, malicious_numbers(data.base.planted))
    all_checks = checks(data, result, state, max_false_alarms)
    print_report(data, result, all_checks, state)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        render_results(data, result, all_checks, state, files.header, max_false_alarms),
        encoding="utf-8",
    )
    ok = passed(all_checks)
    print(f"{verdict(ok)}: results written to {results_path}")
    return EXIT_PASS if ok else EXIT_FAIL


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 2 eval: planted errors, malicious invoices, saved extraction scores "
        "and rename proposals. Free: it never calls a model."
    )
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Generator config the known vendors are read from (as `make eval-extraction`).",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--max-false-alarms",
        type=s1.non_negative,
        default=s1.DEFAULT_MAX_FALSE_ALARMS,
        help="More false alarms than this fails the run (default 0).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    try:
        header = state_header(settings, args.data_dir, args.config)
        engine = create_engine(s1.eval_runner_database_url(settings))
    except (extraction.SetupError, MissingRolePasswordError) as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    try:
        files = ExtractionFiles(args.data_dir, args.state, header)
        return evaluate(engine, files, args.results, args.max_false_alarms)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
