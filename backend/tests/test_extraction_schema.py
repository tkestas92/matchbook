"""The strict model-output schema (task 2.5, threat model T2 and T3)."""

import json
import typing

import pytest
from app.extraction.schema import ExtractionResult, InvoiceExtraction, LineExtraction
from pydantic import ValidationError
from tests.extraction_samples import META_ANSWER, as_json

PII_WORDS = ("email", "phone", "iban", "address", "personal", "card", "contact")


def test_a_valid_answer_parses() -> None:
    parsed = InvoiceExtraction.model_validate_json(as_json(META_ANSWER))
    assert parsed.total == "1210.00"


@pytest.mark.parametrize("field", ["total", "subtotal", "vat"])
def test_float_money_is_rejected(field: str) -> None:
    payload = json.dumps({**META_ANSWER, field: 1210.0})
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(payload)


def test_float_line_amount_is_rejected() -> None:
    lines = [{**META_ANSWER["lines"][0], "amount": 1000.0}]
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER, lines=lines))


@pytest.mark.parametrize("extra", [{"status": "approved"}, {"approved": True}, {"iban": "x"}])
def test_extra_fields_are_rejected(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER, **extra))


def test_extra_line_fields_are_rejected() -> None:
    lines = [{**META_ANSWER["lines"][0], "tool": "pay"}]
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER, lines=lines))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", "1,210.00"),
        ("total", "1210"),
        ("total", "€1210.00"),
        ("vat_rate", "21%"),
        ("issue_date", "1 June 2026"),
        ("currency", "eur"),
        ("number", "x" * 41),
        ("vendor", "v" * 121),
        ("model_confidence", "1.5"),
        ("model_confidence", 0.9),
        ("contains_instructions", "false"),
        ("document_type", "approved"),
    ],
)
def test_patterns_and_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER, **{field: value}))


def test_too_many_lines_are_rejected() -> None:
    lines = META_ANSWER["lines"] * 51
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER, lines=lines))


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate_json(as_json(META_ANSWER)[:-5])


def test_schema_has_no_pii_field() -> None:
    names = set(InvoiceExtraction.model_fields) | set(LineExtraction.model_fields)
    assert not [name for name in names if any(word in name for word in PII_WORDS)]


def test_no_status_means_approved() -> None:
    status = typing.get_type_hints(ExtractionResult)["status"]
    assert set(typing.get_args(status)) == {"extracted", "needs_review", "rejected", "failed"}
