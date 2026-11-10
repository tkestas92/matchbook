"""The planted-error answer key: the generator's JSON shape and its `eval.answer_key` rows.

Only evals/ may use this module (AGENTS.md rule 8). `eval.answer_key` has no columns for the
client, platform, error number or stage, so they are kept in `details` next to the generator's
own details under `planted` (decision 033).
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "error_no",
    "error_type",
    "entity_type",
    "entity_ref",
    "client_code",
    "platform",
    "period_start",
    "period_end",
    "magnitude",
    "expected_detector",
    "detectable_in_stage",
    "details",
)


class AnswerKeyError(ValueError):
    """The answer key file or row does not have the expected shape."""


@dataclass(frozen=True)
class PlantedError:
    error_no: int
    error_type: str
    entity_type: str
    entity_ref: str
    client_code: str
    platform: str | None
    period_start: date
    period_end: date
    magnitude: Decimal  # EUR, net of VAT; signed
    expected_detector: str
    detectable_in_stage: int
    details: dict[str, Any]

    @property
    def platform_campaign_id(self) -> str | None:
        return self.details.get("platform_campaign_id")


@dataclass(frozen=True)
class AnswerKeyFile:
    seed: int
    errors: tuple[PlantedError, ...]


def reject_float(text: str) -> Any:
    raise AnswerKeyError(f"floats are not allowed in the answer key: {text}")


def read_answer_key(path: Path) -> AnswerKeyFile:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_float=reject_float)
    except json.JSONDecodeError as exc:
        raise AnswerKeyError(f"{path} is not valid JSON: {exc}") from exc
    return parse_answer_key(payload)


def parse_answer_key(payload: Any) -> AnswerKeyFile:
    if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
        raise AnswerKeyError("the answer key must be an object with an 'errors' list")
    seed = payload.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AnswerKeyError("the answer key 'seed' must be an integer")
    return AnswerKeyFile(seed=seed, errors=tuple(parse_entry(e) for e in payload["errors"]))


def parse_entry(raw: Any) -> PlantedError:
    if not isinstance(raw, dict):
        raise AnswerKeyError("every answer key entry must be an object")
    missing = [name for name in REQUIRED_FIELDS if name not in raw]
    if missing:
        raise AnswerKeyError(f"answer key entry is missing {', '.join(missing)}")
    platform = raw["platform"]
    if platform is not None and not isinstance(platform, str):
        raise AnswerKeyError("'platform' must be a string or null")
    if not isinstance(raw["details"], dict):
        raise AnswerKeyError("'details' must be an object")
    return PlantedError(
        error_no=integer(raw, "error_no"),
        error_type=text_field(raw, "error_type"),
        entity_type=text_field(raw, "entity_type"),
        entity_ref=text_field(raw, "entity_ref"),
        client_code=text_field(raw, "client_code"),
        platform=platform,
        period_start=iso_date(raw, "period_start"),
        period_end=iso_date(raw, "period_end"),
        magnitude=money(raw["magnitude"]),
        expected_detector=text_field(raw, "expected_detector"),
        detectable_in_stage=integer(raw, "detectable_in_stage"),
        details=raw["details"],
    )


def integer(raw: dict[str, Any], name: str) -> int:
    value = raw[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnswerKeyError(f"'{name}' must be an integer")
    return value


def text_field(raw: dict[str, Any], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str) or not value:
        raise AnswerKeyError(f"'{name}' must be a non-empty string")
    return value


def iso_date(raw: dict[str, Any], name: str) -> date:
    try:
        return date.fromisoformat(text_field(raw, name))
    except ValueError as exc:
        raise AnswerKeyError(f"'{name}' must be an ISO date") from exc


def money(value: Any) -> Decimal:
    """A decimal string with at most two places; anything else is rejected, never guessed."""
    if not isinstance(value, str):
        raise AnswerKeyError("'magnitude' must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise AnswerKeyError(f"'magnitude' is not a decimal: {value!r}") from exc
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        raise AnswerKeyError(f"'magnitude' must have at most two decimal places: {value!r}")
    return amount


def to_row(entry: PlantedError, seed: int) -> dict[str, Any]:
    """Column values for one `eval.answer_key` row."""
    return {
        "error_type": entry.error_type,
        "entity_ref": entry.entity_ref,
        "period_start": entry.period_start,
        "period_end": entry.period_end,
        "magnitude": entry.magnitude,
        "expected_detector": entry.expected_detector,
        "details": {
            "error_no": entry.error_no,
            "entity_type": entry.entity_type,
            "client_code": entry.client_code,
            "platform": entry.platform,
            "detectable_in_stage": entry.detectable_in_stage,
            "seed": seed,
            "planted": entry.details,
        },
    }


def from_row(row: Any) -> PlantedError:
    """The inverse of `to_row`, from a row with the `eval.answer_key` columns."""
    meta = row.details or {}
    try:
        return PlantedError(
            error_no=meta["error_no"],
            error_type=row.error_type,
            entity_type=meta["entity_type"],
            entity_ref=row.entity_ref,
            client_code=meta["client_code"],
            platform=meta["platform"],
            period_start=row.period_start,
            period_end=row.period_end,
            magnitude=row.magnitude if row.magnitude is not None else Decimal("0.00"),
            expected_detector=row.expected_detector,
            detectable_in_stage=meta["detectable_in_stage"],
            details=meta["planted"],
        )
    except KeyError as exc:
        raise AnswerKeyError(
            f"eval.answer_key row {row.id} was not written by the loader: missing {exc}"
        ) from exc
