"""Stage 1 integration eval: app.issues vs eval.answer_key (`make eval-stage1`, decision 033).

Logs in as `eval_runner`, the only role that can read `eval`. Compares the open issues of the
latest finished pipeline run with the planted errors and reports found / missed / false alarms
per error type. Exit codes: 0 pass, 1 a must-find error was missed or there are more false alarms
than allowed, 2 the eval could not run.

The results file holds counts only. Which entities were missed is printed to the terminal and
never committed, so the rules cannot be tuned to the answer key.
"""

import argparse
import sys
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.config import Settings, get_settings
from app.db.models.app import Issue, PipelineRun
from app.db.models.core import Campaign, Client, InsertionOrder, Platform
from app.db.models.eval import AnswerKey
from app.db.roles import MissingRolePasswordError
from app.reconciliation import templates
from sqlalchemy import ColumnElement, Connection, Engine, and_, case, create_engine, func, select
from sqlalchemy.engine import URL, make_url

from evals.answer_key import AnswerKeyError, PlantedError, from_row
from generator.errors import ERROR_TYPES
from pipelines import quality

STAGE = 1
DEFAULT_RESULTS_PATH = Path("docs/eval-results.md")
DEFAULT_MAX_FALSE_ALARMS = 0
FINISHED_RUN_STATUSES = ("succeeded", "partial")
OPEN = "open"
CAMPAIGN, INSERTION_ORDER = "campaign", "insertion_order"  # app.issues.entity_type
ZERO = Decimal("0.00")

EXIT_PASS, EXIT_FAIL, EXIT_SETUP = 0, 1, 2


def quality_type(check: str) -> str:
    return f"quality_{check}"


# Issue types that count as finding each planted error (decisions 030, 031, 033).
ACCEPTED_TYPES: dict[int, frozenset[str]] = {
    1: frozenset({templates.OVERCHARGE}),
    2: frozenset({templates.DUPLICATE_INVOICE}),
    4: frozenset({templates.TIMEZONE_SHIFT, templates.MONTH_BOUNDARY_SHIFT}),
    5: frozenset({templates.WRONG_MARKUP}),
    6: frozenset({templates.CREDIT_NOT_APPLIED}),
    7: frozenset({templates.PACING_OVERPACING}),
    9: frozenset({quality_type(quality.MISSING_DAYS)}),
}

# A quality issue that a later step should have taken over. On a planted error it means the
# error was seen but misclassified: still missed, but not counted again as a false alarm.
PRECURSOR_TYPES: dict[int, frozenset[str]] = {
    4: frozenset({quality_type(quality.MONTH_BOUNDARY_SHIFT)}),
    7: frozenset({quality_type(quality.ZERO_SPEND)}),
}


def planted_entity(planted: PlantedError) -> str | None:
    return planted.entity_ref


def planted_campaign(planted: PlantedError) -> str | None:
    return planted.platform_campaign_id


# Besides client, platform and month, the issue's entity (see ISSUE_ENTITY) must equal this.
ENTITY_OF: dict[int, Callable[[PlantedError], str | None]] = {
    2: planted_entity,  # the copy's invoice number
    4: planted_campaign,
    7: planted_entity,  # campaign_ref
    9: planted_campaign,
}


def issue_entity(issue: "EvalIssue") -> str | None:
    return issue.entity_id


def issue_order(issue: "EvalIssue") -> str | None:
    return issue.order_ref


# The issue side of ENTITY_OF; `entity_id` unless named here. A pacing issue that took over a
# zero-spend issue keeps its platform campaign id, so #7 compares the resolved order code.
ISSUE_ENTITY: dict[int, Callable[["EvalIssue"], str | None]] = {
    7: issue_order,
}

ERROR_NAMES = {error.no: error.slug for error in ERROR_TYPES}


# --- data ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalIssue:
    id: int
    type: str
    client_code: str | None
    platform: str | None
    period: date | None
    entity_id: str | None
    order_ref: str | None = None  # campaign_ref of the insertion order the issue points at


@dataclass(frozen=True)
class FalseAlarm:
    issue: EvalIssue
    error_no: int | None  # the error type the issue's type stands for; None = other type
    reason: str


@dataclass(frozen=True)
class Misclassified:
    planted: PlantedError
    issue: EvalIssue


@dataclass(frozen=True)
class EvalResult:
    found: tuple[tuple[PlantedError, EvalIssue], ...]
    missed: tuple[PlantedError, ...]  # must-find errors only
    expected_missed: tuple[PlantedError, ...]
    misclassified: tuple[Misclassified, ...]
    false_alarms: tuple[FalseAlarm, ...]


@dataclass(frozen=True)
class SummaryRow:
    label: str
    planted: int
    found: int
    missed: int
    expected_missed: int
    false_alarms: int
    eur_planted: Decimal
    eur_found: Decimal


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    status: str
    finished_at: datetime | None


@dataclass(frozen=True)
class EvalInput:
    run: RunInfo
    planted: tuple[PlantedError, ...]
    issues: tuple[EvalIssue, ...]
    seeds: tuple[int, ...]
    other_run_issues: int


class SetupError(RuntimeError):
    """The eval cannot run: nothing to compare."""


# --- matching (pure) ----------------------------------------------------------------------


def is_must_find(planted: PlantedError) -> bool:
    return planted.detectable_in_stage <= STAGE


def matches(planted: PlantedError, issue: EvalIssue, types: frozenset[str]) -> bool:
    entity = ENTITY_OF.get(planted.error_no)
    entity_of_issue = ISSUE_ENTITY.get(planted.error_no, issue_entity)
    return (
        issue.type in types
        and issue.client_code == planted.client_code
        and issue.platform == planted.platform
        and issue.period == planted.period_start
        and (entity is None or entity_of_issue(issue) == entity(planted))
    )


def first_match(
    planted: PlantedError,
    issues: Sequence[EvalIssue],
    used: set[int],
    types: frozenset[str],
) -> EvalIssue | None:
    return next(
        (i for i in issues if i.id not in used and matches(planted, i, types)),
        None,
    )


def match(
    planted: Iterable[PlantedError],
    issues: Iterable[EvalIssue],
    accepted: dict[int, frozenset[str]] = ACCEPTED_TYPES,
) -> EvalResult:
    """Pair each planted error with at most one issue and each issue with at most one error.

    Greedy and deterministic: errors in (error_no, entity_ref) order, issues in id order.
    """
    errors = sorted(planted, key=lambda p: (p.error_no, p.entity_ref))
    ordered = sorted(issues, key=lambda i: i.id)
    used: set[int] = set()
    found: list[tuple[PlantedError, EvalIssue]] = []
    unfound: list[PlantedError] = []
    for error in errors:
        issue = first_match(error, ordered, used, accepted.get(error.error_no, frozenset()))
        if issue is None:
            unfound.append(error)
        else:
            used.add(issue.id)
            found.append((error, issue))

    misclassified: list[Misclassified] = []
    for error in unfound:
        types = PRECURSOR_TYPES.get(error.error_no, frozenset())
        issue = first_match(error, ordered, used, types)
        if issue is not None:
            used.add(issue.id)
            misclassified.append(Misclassified(error, issue))

    found_errors = [error for error, _ in found]
    false_alarms = tuple(
        false_alarm(issue, found_errors, accepted) for issue in ordered if issue.id not in used
    )
    return EvalResult(
        found=tuple(found),
        missed=tuple(e for e in unfound if is_must_find(e)),
        expected_missed=tuple(e for e in unfound if not is_must_find(e)),
        misclassified=tuple(misclassified),
        false_alarms=false_alarms,
    )


def false_alarm(
    issue: EvalIssue,
    found: Sequence[PlantedError],
    accepted: dict[int, frozenset[str]],
) -> FalseAlarm:
    error_no = next((no for no, types in sorted(accepted.items()) if issue.type in types), None)
    duplicate = any(
        matches(error, issue, accepted.get(error.error_no, frozenset())) for error in found
    )
    reason = "duplicate detection" if duplicate else "no planted error"
    return FalseAlarm(issue=issue, error_no=error_no, reason=reason)


def passed(result: EvalResult, max_false_alarms: int) -> bool:
    return not result.missed and len(result.false_alarms) <= max_false_alarms


# --- summary (pure) -----------------------------------------------------------------------


def planted_key(planted: PlantedError) -> tuple[int, str]:
    """Unique per answer key row: each planted entity carries one error (decision 025)."""
    return (planted.error_no, planted.entity_ref)


def eur(errors: Iterable[PlantedError]) -> Decimal:
    return sum((abs(e.magnitude) for e in errors), ZERO)


def summary_row(
    label: str,
    planted: Sequence[PlantedError],
    result: EvalResult,
    false_alarms: int,
) -> SummaryRow:
    keys = {planted_key(p) for p in planted}
    found = [p for p, _ in result.found if planted_key(p) in keys]
    return SummaryRow(
        label=label,
        planted=len(planted),
        found=len(found),
        missed=sum(1 for p in result.missed if planted_key(p) in keys),
        expected_missed=sum(1 for p in result.expected_missed if planted_key(p) in keys),
        false_alarms=false_alarms,
        eur_planted=eur(planted),
        eur_found=eur(found),
    )


def all_planted(result: EvalResult) -> list[PlantedError]:
    return [
        *(p for p, _ in result.found),
        *result.missed,
        *result.expected_missed,
    ]


def summarize(result: EvalResult) -> list[SummaryRow]:
    """One row per error type, one for issue types that stand for no error, and the total."""
    planted = all_planted(result)
    numbers = sorted(
        {*ERROR_NAMES, *(p.error_no for p in planted)}
        | {f.error_no for f in result.false_alarms if f.error_no is not None}
    )
    rows = []
    for no in numbers:
        of_type = [p for p in planted if p.error_no == no]
        name = ERROR_NAMES.get(no) or (of_type[0].error_type if of_type else "unknown")
        alarms = sum(1 for f in result.false_alarms if f.error_no == no)
        rows.append(summary_row(f"#{no} {name}", of_type, result, alarms))
    other = sum(1 for f in result.false_alarms if f.error_no is None)
    rows.append(summary_row("other issue types", [], result, other))
    rows.append(summary_row("**overall**", planted, result, len(result.false_alarms)))
    return rows


def money_text(value: Decimal) -> str:
    return f"{value:,.2f}"


def render_table(rows: Sequence[SummaryRow]) -> str:
    lines = [
        "| Error type | Planted | Found | Missed | Expected missed | False alarms "
        "| € planted | € found |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.label} | {r.planted} | {r.found} | {r.missed} | {r.expected_missed} "
            f"| {r.false_alarms} | {money_text(r.eur_planted)} | {money_text(r.eur_found)} |"
        )
    return "\n".join(lines)


def render_results(data: EvalInput, result: EvalResult, max_false_alarms: int) -> str:
    """docs/eval-results.md: counts only, no entities; nothing here reads the clock."""
    verdict = "PASS" if passed(result, max_false_alarms) else "FAIL"
    seeds = ", ".join(str(s) for s in data.seeds) or "unknown"
    finished = data.run.finished_at.isoformat() if data.run.finished_at else "unknown"
    return "\n".join(
        [
            "# Stage 1 eval results",
            "",
            "> Generated by `make eval-stage1` — do not edit by hand (decision 033).",
            "",
            f"- Pipeline run: `{data.run.id}` ({data.run.status}, finished {finished})",
            f"- Answer key: {len(data.planted)} planted errors, seed {seeds}",
            f"- Open issues evaluated: {len(data.issues)}",
            f"- False-alarm limit: {max_false_alarms}",
            f"- Misclassified (quality issue not taken over): {len(result.misclassified)}",
            f"- Result: **{verdict}**",
            "",
            render_table(summarize(result)),
            "",
            "Expected missed: errors not detectable in stage 1 (#3 stage 2, #8 stage 4; "
            "#10 is not planted until stage 2). They never fail the run.",
            "",
        ]
    )


def describe_planted(p: PlantedError) -> str:
    return (
        f"#{p.error_no} {p.error_type} {p.entity_ref} "
        f"({p.client_code}, {p.platform or '-'}, {p.period_start:%Y-%m})"
    )


def describe_issue(i: EvalIssue) -> str:
    period = f"{i.period:%Y-%m}" if i.period else "-"
    order = f" [{i.order_ref}]" if i.order_ref and i.order_ref != i.entity_id else ""
    return (
        f"issue {i.id} {i.type} {i.entity_id or '-'}{order} "
        f"({i.client_code or '-'}, {i.platform or '-'}, {period})"
    )


def render_details(result: EvalResult) -> str:
    """Terminal only: which entities were missed or falsely flagged."""
    lines = [f"missed: {describe_planted(p)}" for p in result.missed]
    lines += [
        f"misclassified: {describe_planted(m.planted)} -> {describe_issue(m.issue)}"
        for m in result.misclassified
    ]
    lines += [f"false alarm ({f.reason}): {describe_issue(f.issue)}" for f in result.false_alarms]
    return "\n".join(lines)


# --- database -----------------------------------------------------------------------------


def eval_runner_database_url(settings: Settings) -> URL:
    """`DATABASE_URL` logged in as `eval_runner`, the only role that can read `eval`."""
    if settings.eval_runner_db_password is None:
        raise MissingRolePasswordError("Set EVAL_RUNNER_DB_PASSWORD in .env (see .env.example)")
    password = settings.eval_runner_db_password.get_secret_value()
    return make_url(settings.database_url).set(username="eval_runner", password=password)


def latest_run(conn: Connection) -> RunInfo:
    row = conn.execute(
        select(PipelineRun.id, PipelineRun.status, PipelineRun.finished_at)
        .where(PipelineRun.status.in_(FINISHED_RUN_STATUSES))
        .order_by(PipelineRun.finished_at.desc().nulls_last(), PipelineRun.started_at.desc())
        .limit(1)
    ).first()
    if row is None:
        raise SetupError("no succeeded or partial pipeline run; run `make pipeline` first")
    return RunInfo(id=row.id, status=row.status, finished_at=row.finished_at)


def read_planted(conn: Connection) -> tuple[tuple[PlantedError, ...], tuple[int, ...]]:
    rows = conn.execute(select(AnswerKey.__table__).order_by(AnswerKey.id)).all()
    if not rows:
        raise SetupError("eval.answer_key is empty; run `make eval-load-key` first")
    seeds = sorted({row.details["seed"] for row in rows if row.details and "seed" in row.details})
    return tuple(from_row(row) for row in rows), tuple(seeds)


def order_ref_column() -> ColumnElement[str | None]:
    """The insertion order's campaign_ref, itself or through the campaign's `io_id` link."""
    return case(
        (Issue.entity_type == INSERTION_ORDER, Issue.entity_id),
        else_=InsertionOrder.campaign_ref,
    ).label("order_ref")


def read_issues(conn: Connection, run_id: uuid.UUID) -> tuple[EvalIssue, ...]:
    query = (
        select(
            Issue.id,
            Issue.type,
            Client.code.label("client_code"),
            Platform.name.label("platform"),
            Issue.period,
            Issue.entity_id,
            order_ref_column(),
        )
        .outerjoin(Client, Client.id == Issue.client_id)
        .outerjoin(Platform, Platform.id == Issue.platform_id)
        .outerjoin(
            Campaign,
            and_(
                Issue.entity_type == CAMPAIGN,
                Campaign.platform_id == Issue.platform_id,
                Campaign.platform_campaign_id == Issue.entity_id,
            ),
        )
        .outerjoin(InsertionOrder, InsertionOrder.id == Campaign.io_id)
        .where(Issue.status == OPEN, Issue.run_id == run_id)
        .order_by(Issue.id)
    )
    return tuple(EvalIssue(**row._mapping) for row in conn.execute(query))


def count_other_run_issues(conn: Connection, run_id: uuid.UUID) -> int:
    query = select(func.count()).where(Issue.status == OPEN, Issue.run_id != run_id)
    return conn.execute(query.select_from(Issue)).scalar_one()


def read_eval_input(conn: Connection) -> EvalInput:
    planted, seeds = read_planted(conn)
    run = latest_run(conn)
    return EvalInput(
        run=run,
        planted=planted,
        issues=read_issues(conn, run.id),
        seeds=seeds,
        other_run_issues=count_other_run_issues(conn, run.id),
    )


# --- entry point --------------------------------------------------------------------------


def evaluate(engine: Engine, results_path: Path, max_false_alarms: int) -> int:
    try:
        with engine.connect() as conn:
            data = read_eval_input(conn)
    except (SetupError, AnswerKeyError) as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    result = match(data.planted, data.issues)
    print(render_table(summarize(result)))
    details = render_details(result)
    if details:
        print()
        print(details)
    if data.other_run_issues:
        print(f"note: {data.other_run_issues} open issues from earlier runs were not evaluated")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(render_results(data, result, max_false_alarms), encoding="utf-8")
    ok = passed(result, max_false_alarms)
    print(f"{'PASS' if ok else 'FAIL'}: results written to {results_path}")
    return EXIT_PASS if ok else EXIT_FAIL


def non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return number


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 1 eval: app.issues vs the answer key.")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument(
        "--max-false-alarms",
        type=non_negative,
        default=DEFAULT_MAX_FALSE_ALARMS,
        help="More false alarms than this fails the run (default 0).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        engine = create_engine(eval_runner_database_url(get_settings()))
    except MissingRolePasswordError as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    try:
        return evaluate(engine, args.results, args.max_false_alarms)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
