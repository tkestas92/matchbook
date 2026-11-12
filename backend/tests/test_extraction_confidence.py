"""Confidence computed in code (task 2.5, threat model T3). Hand-written texts only."""

from decimal import Decimal
from typing import Any

import pytest
from app.extraction.confidence import amounts_in, evaluate
from app.extraction.schema import InvoiceExtraction
from app.extraction.vendors import KnownVendors
from tests.extraction_samples import (
    AGENCY_ANSWER,
    AGENCY_TEXT,
    CREDIT_NOTE_ANSWER,
    CREDIT_NOTE_TEXT,
    EMAIL,
    GOOGLE_ANSWER,
    GOOGLE_TEXT,
    IBAN,
    KNOWN_VENDORS,
    META_ANSWER,
    META_TEXT,
    PHONE,
    SAMPLES,
    as_json,
)


def run(
    text: str,
    answer: dict[str, Any],
    known: frozenset[str] | None = None,
    vendors: KnownVendors = KNOWN_VENDORS,
    **changes: Any,
):
    raw = InvoiceExtraction.model_validate_json(as_json(answer, **changes))
    return evaluate(raw, text, known, known_vendors=vendors)


@pytest.mark.parametrize("layout", sorted(SAMPLES))
def test_correct_answers_pass_every_check(layout: str) -> None:
    text, answer = SAMPLES[layout]
    evaluation = run(text, answer)
    assert evaluation.reasons == ()
    assert set(evaluation.field_confidence.values()) == {Decimal("0.95")}


def test_derived_fields_come_from_code() -> None:
    meta = run(META_TEXT, META_ANSWER).document
    assert (meta.type, meta.kind, meta.platform, meta.client_code) == (
        "invoice",
        "platform",
        "meta",
        "SAU",
    )
    agency = run(AGENCY_TEXT, AGENCY_ANSWER).document
    assert (agency.kind, agency.platform, agency.client_code) == ("client", None, "SAU")
    assert [line.line_no for line in agency.lines] == [1, 2, 3]
    note = run(CREDIT_NOTE_TEXT, CREDIT_NOTE_ANSWER).document
    assert (note.type, note.kind, note.platform, note.client_code) == (
        "credit_note",
        None,
        "meta",
        "SAU",
    )


def test_money_is_decimal_and_account_id_has_no_dashes() -> None:
    document = run(GOOGLE_TEXT, GOOGLE_ANSWER).document
    assert document.total == Decimal("1493.82") and isinstance(document.total, Decimal)
    assert document.account_id == "1234567890"


# --- arithmetic ----------------------------------------------------------------------------


def test_lines_not_summing_to_the_subtotal_are_caught() -> None:
    lines = [dict(line) for line in AGENCY_ANSWER["lines"]]
    lines[2]["amount"] = "80.00"
    # Printed wrong too, so grounding passes and only the arithmetic fails.
    text = AGENCY_TEXT.replace("on media 90.00 €", "on media 80.00 €")
    evaluation = run(text, AGENCY_ANSWER, lines=lines)
    assert "arithmetic:subtotal" in evaluation.reasons
    assert evaluation.field_confidence["subtotal"] == 0
    assert all(evaluation.field_confidence[f"lines[{i}].amount"] == 0 for i in range(3))
    assert evaluation.field_confidence["vendor"] == Decimal("0.95")


def test_wrong_vat_is_caught() -> None:
    text = META_TEXT.replace("€210.00", "€200.00").replace("€1,210.00", "€1,200.00")
    evaluation = run(text, META_ANSWER, vat="200.00", total="1200.00")
    assert evaluation.reasons == ("arithmetic:vat",)


def test_wrong_total_is_caught() -> None:
    text = META_TEXT.replace("€1,210.00", "€1,220.00")
    evaluation = run(text, META_ANSWER, total="1220.00")
    assert evaluation.reasons == ("arithmetic:total",)


def test_unknown_vat_rate_is_caught() -> None:
    evaluation = run(META_TEXT, META_ANSWER, vat_rate="0.09")
    assert "format:vat_rate" in evaluation.reasons


# --- grounding -----------------------------------------------------------------------------


def test_a_changed_total_is_not_grounded() -> None:
    evaluation = run(META_TEXT, META_ANSWER, total="1.00")
    assert "grounding:total" in evaluation.reasons
    assert evaluation.field_confidence["total"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor", "Other Vendor"),
        ("bill_to", "Someone Else"),
        ("account_id", "999999999999999"),
        ("issue_date", "2026-06-09"),
        ("number", "FBADS-456789-000000000"),
    ],
)
def test_an_ungrounded_value_gets_zero(field: str, value: str) -> None:
    evaluation = run(META_TEXT, META_ANSWER, **{field: value})
    assert f"grounding:{field}" in evaluation.reasons
    assert evaluation.field_confidence[field] == 0


def test_printed_vendor_heading_resolves_to_the_canonical_name() -> None:
    evaluation = run(META_TEXT, META_ANSWER, vendor="Meta Ads")
    assert evaluation.document.vendor == "Meta"
    assert evaluation.reasons == ()
    assert evaluation.field_confidence["vendor"] == Decimal("0.95")


def test_a_grounded_but_unknown_vendor_gets_zero_and_a_fixed_reason() -> None:
    text = META_TEXT.replace("Meta Ads", "Acme Media Ads")
    evaluation = run(text, META_ANSWER, vendor="Acme Media")
    assert evaluation.reasons == ("unknown:vendor",)
    assert evaluation.field_confidence["vendor"] == 0
    assert evaluation.document.vendor == "Acme Media"  # printed value kept for the reviewer


def test_empty_known_vendors_resolve_nothing() -> None:
    evaluation = run(META_TEXT, META_ANSWER, vendors=KnownVendors.of(()))
    assert evaluation.reasons == ("unknown:vendor",)
    assert evaluation.field_confidence["vendor"] == 0


def test_vendor_reasons_carry_no_document_text() -> None:
    text = META_TEXT.replace("Meta Ads", f"Acme Media Ads {EMAIL}")
    evaluation = run(text, META_ANSWER, vendor=f"Acme Media {EMAIL}")
    assert "unknown:vendor" in evaluation.reasons
    for reason in evaluation.reasons:
        assert all(s.casefold() not in reason.casefold() for s in ("acme", EMAIL, IBAN, PHONE))


def test_dates_ground_in_every_printed_format() -> None:
    assert run(META_TEXT, META_ANSWER).field_confidence["issue_date"] > 0  # June 1, 2026
    assert run(GOOGLE_TEXT, GOOGLE_ANSWER).field_confidence["issue_date"] > 0  # 02 Jun 2026


def test_amounts_are_read_in_every_layout_format_with_sign() -> None:
    text = "€1,234.56 | 2,000.00 EUR | EUR 300.00 | 4,000.00 € | -5.00 € | -€6.00 | EUR -7.00"
    assert amounts_in(text) == {
        Decimal(v) for v in ("1234.56", "2000.00", "300.00", "4000.00", "-5.00", "-6.00", "-7.00")
    }


def test_a_dropped_minus_sign_is_not_grounded() -> None:
    lines = [dict(line) for line in AGENCY_ANSWER["lines"]]
    lines[1]["amount"] = "100.00"
    evaluation = run(AGENCY_TEXT, AGENCY_ANSWER, lines=lines)
    assert "grounding:lines[1].amount" in evaluation.reasons


# --- format --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"issue_date": "2026-02-30"}, "format:issue_date"),
        ({"due_date": "2026-05-01"}, "format:due_date"),
        ({"currency": "USD"}, "format:currency"),
        ({"number": "INV-1"}, "format:number"),
        ({"document_type": "credit_note"}, "format:type"),
        ({"account_id": None}, "missing:account_id"),
    ],
)
def test_format_checks(changes: dict[str, Any], reason: str) -> None:
    assert reason in run(META_TEXT, META_ANSWER, **changes).reasons


def test_bad_campaign_ref_format_and_platform() -> None:
    lines = [{**META_ANSWER["lines"][0], "campaign_ref": "SAU-GOOGLE-2605"}]
    evaluation = run(META_TEXT, META_ANSWER, lines=lines)
    assert "format:lines[0].campaign_ref" in evaluation.reasons


def test_unknown_campaign_ref_when_known_refs_are_given() -> None:
    assert run(META_TEXT, META_ANSWER, frozenset({"SAU-META-2605"})).reasons == ()
    evaluation = run(META_TEXT, META_ANSWER, frozenset({"GIN-META-2605"}))
    assert evaluation.reasons == ("unknown_ref:lines[0].campaign_ref",)


def test_credit_note_requires_its_fields() -> None:
    evaluation = run(CREDIT_NOTE_TEXT, CREDIT_NOTE_ANSWER, reason=None, original_number=None)
    assert {"missing:reason", "missing:original_number"} <= set(evaluation.reasons)


def test_credit_note_arithmetic() -> None:
    text = CREDIT_NOTE_TEXT.replace("€121.00", "€131.00")
    assert run(text, CREDIT_NOTE_ANSWER, total="131.00").reasons == ("arithmetic:total",)


# --- the model's score -----------------------------------------------------------------------


def test_model_score_only_lowers() -> None:
    low = run(META_TEXT, META_ANSWER, model_confidence="0.3")
    assert set(low.field_confidence.values()) == {Decimal("0.3")}
    high_but_wrong = run(META_TEXT, META_ANSWER, model_confidence="1", total="1.00")
    assert high_but_wrong.field_confidence["total"] == 0
    assert high_but_wrong.confidence == 0
