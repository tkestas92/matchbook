"""Stage-1 results for the demo, parsed from the committed `docs/eval-results.md`.

Only the per-type counts, the € sums and the verdict are kept; the run id, the seed and every
other line of the file are dropped (decisions 033, 035). Any change to the table layout makes
the parser refuse the file instead of guessing.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_RESULTS_PATH = Path(__file__).resolve().parents[3] / "docs" / "eval-results.md"

HEADER = (
    "Error type",
    "Planted",
    "Found",
    "Missed",
    "Expected missed",
    "False alarms",
    "€ planted",
    "€ found",
)
LABEL = re.compile(r"^(#\d{1,2} [a-z_]+|other issue types|overall)$")
COUNT = re.compile(r"^\d{1,6}$")
MONEY = re.compile(r"^-?\d{1,3}(,\d{3})*\.\d{2}$")
VERDICT = re.compile(r"^- Result: \*\*(PASS|FAIL)\*\*$", re.MULTILINE)


class ResultsUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResultRow:
    error_type: str
    planted: int
    found: int
    missed: int
    expected_missed: int
    false_alarms: int
    eur_planted: Decimal
    eur_found: Decimal


@dataclass(frozen=True)
class Results:
    result: str
    rows: list[ResultRow]
    overall: ResultRow


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_count(text: str) -> int:
    if not COUNT.match(text):
        raise ResultsUnavailableError("count cell is not an integer")
    return int(text)


def parse_money(text: str) -> Decimal:
    if not MONEY.match(text):
        raise ResultsUnavailableError("money cell is not a 2-decimal amount")
    return Decimal(text.replace(",", ""))


def parse_row(line: str) -> ResultRow:
    values = cells(line)
    if len(values) != len(HEADER):
        raise ResultsUnavailableError("row has the wrong number of cells")
    label = values[0].strip("*")
    if not LABEL.match(label):
        raise ResultsUnavailableError("unexpected row label")
    counts = [parse_count(value) for value in values[1:6]]
    return ResultRow(label, *counts, parse_money(values[6]), parse_money(values[7]))


def parse_results(text: str) -> Results:
    lines = text.splitlines()
    header_at = next((i for i, line in enumerate(lines) if tuple(cells(line)) == HEADER), None)
    verdict = VERDICT.search(text)
    if header_at is None or verdict is None:
        raise ResultsUnavailableError("results table or verdict not found")
    body = []
    for line in lines[header_at + 2 :]:
        if not line.startswith("|"):
            break
        body.append(parse_row(line))
    if not body or body[-1].error_type != "overall":
        raise ResultsUnavailableError("overall row missing")
    return Results(result=verdict.group(1), rows=body[:-1], overall=body[-1])


def read_results(path: Path) -> Results:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResultsUnavailableError("results file cannot be read") from exc
    return parse_results(text)
