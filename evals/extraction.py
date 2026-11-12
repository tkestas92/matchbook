"""Extraction eval: `extract_invoice` on every generated PDF vs its ground truth (decision 043).

Owner-run (`make eval-extraction`); it calls the real model and costs money, so tests and CI
never run `main`. The extractor gets PDF bytes only; this harness reads the ground-truth `.json`
next to each PDF and compares. The malicious list is the #10 rows of `eval.answer_key`, read as
`eval_runner` (decision 033), and must equal the copies in `inbox/`.

Model calls are throttled to stay under free-tier rate limits, the per-run and monthly caps hold,
and every scored document is appended to a resume state under the gitignored `data/`, so a run
stopped by the cap or the provider exits 3 and the next run carries on. Nothing printed or saved
holds document text, field values or the key; per-document lines go to the terminal only.

Exit codes: 0 complete and every malicious copy rejected with no model call; 1 a malicious copy
was not rejected or reached the model; 2 the eval could not run; 3 stopped early, resumable.
"""

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models.eval import AnswerKey
from app.db.roles import MissingRolePasswordError
from app.extraction.extract import PROMPT_ID, ExtractionConfig, extract_invoice
from app.extraction.schema import ExtractionResult
from app.llm.client import MissingApiKeyError, create_llm_client
from app.llm.cost import CostCapExceeded, CostLedger, monthly_spend
from app.llm.prompts import load_prompt
from app.llm.provider import LLMError, ModelRequest, ModelResponse, Provider, ProviderError
from sqlalchemy import Connection, create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from evals.answer_key import AnswerKeyError, from_row
from evals.extraction_scoring import (
    BENIGN,
    MALICIOUS,
    DocumentScore,
    FieldScore,
    GroundTruth,
    GroundTruthError,
    LineScore,
    RunInfo,
    Usage,
    injection_passed,
    parse_ground_truth,
    render_details,
    render_results,
    render_summary,
    score_document,
    summarize,
)
from evals.stage1 import eval_runner_database_url

DEFAULT_DATA_DIR = Path("data/generated")
DEFAULT_RESULTS_PATH = Path("docs/eval-results-extraction.md")
DEFAULT_STATE_PATH = Path("data/eval/extraction_state.jsonl")
DEFAULT_MIN_INTERVAL = 4.5  # seconds between model calls: ~13 per minute, under the free tier
DOCUMENT_DIRS = ("invoices", "credit_notes", "inbox")
INBOX = "inbox"
MANIFEST_NAME = "manifest.json"
PROMPT_INJECTION = 10

EXIT_PASS, EXIT_FAIL, EXIT_SETUP, EXIT_PARTIAL = 0, 1, 2, 3


class SetupError(RuntimeError):
    """The eval cannot run: missing or inconsistent inputs."""


# --- documents ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalDocument:
    path: str  # relative to the data directory
    pdf: Path
    truth: GroundTruth
    in_inbox: bool


def discover_documents(data_dir: Path) -> list[EvalDocument]:
    """Every PDF under invoices/, credit_notes/ and inbox/ with the ground truth next to it."""
    documents = []
    for directory in DOCUMENT_DIRS:
        for pdf in sorted((data_dir / directory).rglob("*.pdf")):
            relative = pdf.relative_to(data_dir).as_posix()
            truth_path = pdf.with_suffix(".json")
            if not truth_path.is_file():
                raise SetupError(f"{relative} has no ground-truth .json next to it")
            try:
                truth = parse_ground_truth(truth_path.read_text(encoding="utf-8"))
            except GroundTruthError as exc:
                raise SetupError(f"{relative}: bad ground truth: {exc}") from exc
            documents.append(EvalDocument(relative, pdf, truth, directory == INBOX))
    if not documents:
        raise SetupError(f"no PDFs under {data_dir}; run `make generate` first")
    return documents


def read_seed(data_dir: Path) -> int | None:
    try:
        seed = json.loads((data_dir / MANIFEST_NAME).read_text(encoding="utf-8")).get("seed")
    except (OSError, ValueError, AttributeError):
        return None
    return seed if isinstance(seed, int) and not isinstance(seed, bool) else None


def check_malicious(documents: Sequence[EvalDocument], key_numbers: frozenset[str]) -> None:
    """The answer key's #10 copies must be exactly the documents in inbox/ (counts only)."""
    if not key_numbers:
        raise SetupError("eval.answer_key has no #10 rows; run `make eval-load-key` first")
    inbox = {doc.truth.number for doc in documents if doc.in_inbox}
    outside = {doc.truth.number for doc in documents if not doc.in_inbox} & key_numbers
    if inbox != key_numbers or outside:
        raise SetupError(
            f"answer key has {len(key_numbers)} #10 rows, inbox/ has {len(inbox)} copies; "
            f"{len(key_numbers - inbox)} key rows are not in inbox/, "
            f"{len(inbox - key_numbers)} inbox copies are not in the key"
        )


def group_of(doc: EvalDocument, malicious_numbers: frozenset[str]) -> str:
    return MALICIOUS if doc.truth.number in malicious_numbers else BENIGN


def select_documents(
    documents: Sequence[EvalDocument], malicious_numbers: frozenset[str], limit: int | None
) -> list[EvalDocument]:
    """Malicious copies first (no model cost, and they decide the verdict), then benign ones."""
    malicious = [d for d in documents if group_of(d, malicious_numbers) == MALICIOUS]
    benign = [d for d in documents if group_of(d, malicious_numbers) == BENIGN]
    return [*malicious, *(benign if limit is None else benign[:limit])]


# --- throttling and counting --------------------------------------------------------------


class ThrottledCounter:
    """Wraps the provider: keeps model calls `min_interval` apart and counts them.

    Retries inside `LLMClient` go through here too, so they are throttled and counted as well.
    """

    def __init__(
        self,
        inner: Provider,
        min_interval: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self.calls = 0
        self._last: float | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self.calls += 1
        try:
            return self.inner.generate(request)
        finally:
            self._last = self.clock()


# --- resume state -------------------------------------------------------------------------


@dataclass(frozen=True)
class StateHeader:
    model: str
    prompt_hash: str
    seed: int | None
    threshold: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "seed": self.seed,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class SavedState:
    scores: dict[str, DocumentScore]  # by relative path
    spent_by_month: dict[str, Decimal]
    stale: bool  # the file was for another model, prompt, data seed or threshold


def score_to_record(score: DocumentScore, month: str) -> dict[str, Any]:
    """Outcomes, counts and usage only: never a value or any document text."""
    return {
        "path": score.path,
        "sha256": score.sha256,
        "group": score.group,
        "status": score.status,
        "reasons": list(score.reasons),
        "fields": {
            name: [f.outcome, f.high_confidence] for name, f in sorted(score.fields.items())
        },
        "lines": [
            score.lines.expected,
            score.lines.extracted,
            score.lines.matched,
            score.lines.known_gap,
        ],
        "model_calls": score.model_calls,
        "usage": {
            "input_tokens": score.usage.input_tokens,
            "output_tokens": score.usage.output_tokens,
            "cost_eur": str(score.usage.cost_eur),
            "latency_s": score.usage.latency_s,
            "attempts": score.usage.attempts,
        },
        "month": month,
    }


def score_from_record(record: dict[str, Any]) -> DocumentScore:
    usage = record["usage"]
    expected, extracted, matched, known_gap = record["lines"]
    return DocumentScore(
        path=record["path"],
        sha256=record["sha256"],
        group=record["group"],
        status=record["status"],
        reasons=tuple(record["reasons"]),
        fields={name: FieldScore(o, bool(h)) for name, (o, h) in record["fields"].items()},
        lines=LineScore(expected, extracted, matched, known_gap),
        model_calls=record["model_calls"],
        usage=Usage(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_eur=Decimal(usage["cost_eur"]),
            latency_s=usage["latency_s"],
            attempts=usage["attempts"],
        ),
    )


def add_spend(spent: dict[str, Decimal], month: str, cost: Decimal) -> None:
    spent[month] = spent.get(month, Decimal(0)) + cost


def read_state(path: Path, header: StateHeader) -> SavedState:
    """Scores saved for this model, prompt, seed and threshold; spend is kept either way."""
    if not path.is_file():
        return SavedState({}, {}, stale=False)
    scores: dict[str, DocumentScore] = {}
    spent: dict[str, Decimal] = {}
    matches_header = False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if "header" in entry:
                matches_header = entry["header"] == header.as_dict()
            elif "spend" in entry:
                add_spend(spent, entry["spend"]["month"], Decimal(entry["spend"]["cost_eur"]))
            elif "doc" in entry:
                record = entry["doc"]
                add_spend(spent, record["month"], Decimal(record["usage"]["cost_eur"]))
                if matches_header:
                    score = score_from_record(record)
                    scores[score.path] = score
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise SetupError(f"{path} is damaged ({type(exc).__name__}); rerun with --fresh") from exc
    return SavedState(scores if matches_header else {}, spent, stale=not matches_header)


class StateWriter:
    def __init__(self, path: Path, month: str) -> None:
        self.path = path
        self.month = month

    def start(self, header: StateHeader, spent_by_month: dict[str, Decimal]) -> None:
        """A new file: the header, then spend carried over so the monthly cap still counts it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"header": header.as_dict()}]
        lines += [
            {"spend": {"month": month, "cost_eur": str(cost)}}
            for month, cost in sorted(spent_by_month.items())
            if cost
        ]
        self.path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    def append(self, score: DocumentScore) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"doc": score_to_record(score, self.month)}) + "\n")


# --- the run ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutcome:
    scores: tuple[DocumentScore, ...]
    stopped: str | None  # why the run ended early; None when every document was scored


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_documents(
    documents: Sequence[EvalDocument],
    malicious_numbers: frozenset[str],
    saved: dict[str, DocumentScore],
    extract: Callable[[bytes], ExtractionResult],
    counter: ThrottledCounter,
    threshold: Decimal,
    on_scored: Callable[[DocumentScore], None],
) -> RunOutcome:
    """Score each document; a saved score for the same bytes is reused, not paid for again."""
    scores: list[DocumentScore] = []
    for doc in documents:
        data = doc.pdf.read_bytes()
        sha = sha256_of(data)
        previous = saved.get(doc.path)
        if previous is not None and previous.sha256 == sha:
            scores.append(previous)
            continue
        before = counter.calls
        try:
            result = extract(data)
        except CostCapExceeded as exc:
            return RunOutcome(tuple(scores), f"cost cap: {exc}")
        except ProviderError as exc:
            return RunOutcome(tuple(scores), f"provider: {type(exc).__name__}: {exc}")
        score = score_document(
            doc.path,
            sha,
            group_of(doc, malicious_numbers),
            doc.truth,
            result,
            counter.calls - before,
            threshold,
        )
        on_scored(score)
        scores.append(score)
    return RunOutcome(tuple(scores), None)


def exit_code(outcome: RunOutcome) -> int:
    """A failed malicious copy fails even a partial run; malicious copies run first."""
    if not injection_passed(summarize(outcome.scores)):
        return EXIT_FAIL
    return EXIT_PARTIAL if outcome.stopped is not None else EXIT_PASS


# --- database (eval_runner) ---------------------------------------------------------------


def read_malicious_numbers(conn: Connection) -> frozenset[str]:
    rows = conn.execute(select(AnswerKey.__table__).order_by(AnswerKey.id)).all()
    if not rows:
        raise SetupError("eval.answer_key is empty; run `make eval-load-key` first")
    planted = (from_row(row) for row in rows)
    return frozenset(p.entity_ref for p in planted if p.error_no == PROMPT_INJECTION)


# --- entry point --------------------------------------------------------------------------


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return number


def positive_eur(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("must be above zero")
    return amount


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extraction eval: PDFs vs ground truth.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--limit",
        type=positive_int,
        help="Only the first N benign documents (all malicious copies always run: no model cost). "
        "A sample never writes the results file.",
    )
    parser.add_argument(
        "--min-interval",
        type=non_negative_float,
        default=DEFAULT_MIN_INTERVAL,
        help=f"Seconds between model calls (default {DEFAULT_MIN_INTERVAL}).",
    )
    parser.add_argument(
        "--max-cost",
        type=positive_eur,
        help="Lower the per-run cap in EUR for this run (never raises LLM_RUN_CAP_EUR).",
    )
    parser.add_argument("--fresh", action="store_true", help="Discard saved progress.")
    return parser.parse_args(argv)


def print_report(outcome: RunOutcome) -> None:
    print(render_summary(summarize(outcome.scores)))
    details = render_details(outcome.scores)
    if details:
        print()
        print(details)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    now = datetime.now(UTC)
    month = now.astimezone(ZoneInfo(settings.agency_timezone)).strftime("%Y-%m")
    try:
        documents = discover_documents(args.data_dir)
        engine = create_engine(eval_runner_database_url(settings))
        try:
            with engine.connect() as conn:
                malicious_numbers = read_malicious_numbers(conn)
            spent_by_pipeline = monthly_spend(engine, now, settings.agency_timezone)
        finally:
            engine.dispose()
        check_malicious(documents, malicious_numbers)
        prompt = load_prompt(PROMPT_ID)
        config = ExtractionConfig.from_settings(settings)
        header = StateHeader(
            model=settings.llm_model,
            prompt_hash=prompt.sha256,
            seed=read_seed(args.data_dir),
            threshold=str(config.confidence_threshold),
        )
        saved = read_state(args.state, header)
    except (SetupError, AnswerKeyError, MissingRolePasswordError) as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except SQLAlchemyError as exc:  # its message can name the server; the class is enough
        print(
            f"eval cannot run: database error {type(exc).__name__} (is `make up` running?)",
            file=sys.stderr,
        )
        return EXIT_SETUP

    if saved.stale:
        print("note: saved progress is for another model, prompt, seed or threshold; starting over")
    scores = {} if args.fresh else saved.scores
    run_cap = settings.llm_run_cap_eur
    if args.max_cost is not None:
        run_cap = min(run_cap, args.max_cost)
    ledger = CostLedger(
        run_cap=run_cap,
        monthly_cap=settings.llm_monthly_cap_eur,
        spent_this_month_before_run=spent_by_pipeline + saved.spent_by_month.get(month, Decimal(0)),
    )
    try:
        client = create_llm_client(settings, ledger)
    except MissingApiKeyError as exc:
        print(f"eval cannot run: {exc}", file=sys.stderr)
        return EXIT_SETUP
    counter = ThrottledCounter(client.provider, args.min_interval)
    client.provider = counter

    writer = StateWriter(args.state, month)
    if args.fresh or saved.stale or not args.state.is_file():
        writer.start(header, saved.spent_by_month)
    selected = select_documents(documents, malicious_numbers, args.limit)
    print(
        f"model {settings.llm_model}, prompt {PROMPT_ID} {prompt.sha256[:12]}, "
        f"{len(selected)} documents ({len(scores)} saved), run cap EUR {run_cap}"
    )
    started = time.monotonic()
    try:
        outcome = run_documents(
            selected,
            malicious_numbers,
            scores,
            lambda data: extract_invoice(data, client, config),
            counter,
            config.confidence_threshold,
            writer.append,
        )
    except LLMError as exc:
        print(f"eval stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except Exception as exc:  # anything else may carry document text, so only its class is shown
        print(f"eval stopped: {type(exc).__name__}", file=sys.stderr)
        return EXIT_SETUP

    print_report(outcome)
    print()
    print(
        f"this invocation: {counter.calls} model calls, EUR {ledger.total}, "
        f"{time.monotonic() - started:.0f} s wall time"
    )
    code = exit_code(outcome)
    if outcome.stopped is not None:
        print(f"PARTIAL: {outcome.stopped}. Progress is saved; rerun to resume.")
    elif args.limit is not None:
        print(f"SAMPLE of {args.limit} benign documents: {args.results} not written.")
    else:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        info = RunInfo(
            model=settings.llm_model,
            prompt_id=PROMPT_ID,
            prompt_hash=prompt.sha256,
            seed=header.seed,
            threshold=config.confidence_threshold,
        )
        args.results.write_text(render_results(summarize(outcome.scores), info), encoding="utf-8")
        print(f"results written to {args.results}")
    print("PASS" if code == EXIT_PASS else "FAIL" if code == EXIT_FAIL else "PARTIAL")
    return code


if __name__ == "__main__":
    sys.exit(main())
