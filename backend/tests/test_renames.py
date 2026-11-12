"""The rename rule and its issue text: pure code, hand-built observations only (decision 047)."""

from datetime import date
from decimal import Decimal

from app.reconciliation import templates
from app.reconciliation.models import NameObservation, RenameVerdict
from app.reconciliation.renames import (
    CAMPAIGN_REF_TAG,
    find_renames,
    has_ref_tag,
    issue_key,
    rename_draft,
    rule_verdict,
)

from pipelines import load_plans

TAGGED = "Acme | Meta | 2026-06 [ACME-META-2606]"
RENAMED = "Acme Meta – Promo June"
OTHER = "Acme Meta – Retargeting"


def seen(
    day: int,
    name: str,
    spend: str | None = "10.00",
    month: int = 6,
    campaign: str = "c1",
    platform: str = "meta",
) -> NameObservation:
    return NameObservation(
        platform=platform,
        platform_campaign_id=campaign,
        client="ACME",
        date=date(2026, month, day),
        name=name,
        spend=None if spend is None else Decimal(spend),
    )


def test_tag_pattern_is_the_one_plans_link_by() -> None:
    assert CAMPAIGN_REF_TAG.pattern == load_plans.CAMPAIGN_REF_TAG.pattern


def test_ref_tag_must_end_the_name() -> None:
    assert has_ref_tag(TAGGED) and has_ref_tag("x [A-B-1]  ")
    assert not has_ref_tag(RENAMED) and not has_ref_tag("[A-B-1] x")


def test_tagged_name_later_reported_without_its_tag_is_found() -> None:
    found = find_renames([seen(1, TAGGED), seen(14, TAGGED), seen(15, RENAMED), seen(16, RENAMED)])
    assert len(found) == 1
    candidate = found[0]
    assert (candidate.platform, candidate.platform_campaign_id, candidate.client) == (
        "meta",
        "c1",
        "ACME",
    )
    assert candidate.old_name == TAGGED and candidate.new_names == (RENAMED,)
    assert candidate.period == date(2026, 6, 1) and candidate.renamed_from == date(2026, 6, 15)
    assert candidate.amount == Decimal("20.00")


def test_input_order_does_not_matter() -> None:
    days = [seen(15, RENAMED), seen(1, TAGGED)]
    assert find_renames(days) == find_renames(list(reversed(days)))


def test_a_name_that_keeps_a_tag_is_not_a_rename() -> None:
    assert find_renames([seen(1, TAGGED), seen(15, "Acme June [ACME-META-2606]")]) == []


def test_an_untagged_first_name_is_not_a_rename() -> None:
    assert find_renames([seen(1, "Acme always on"), seen(15, RENAMED)]) == []


def test_one_name_only_is_not_a_rename() -> None:
    assert find_renames([seen(1, TAGGED), seen(2, TAGGED)]) == []


def test_a_confirmed_alias_is_not_a_rename_again() -> None:
    days = [seen(1, TAGGED), seen(15, RENAMED)]
    assert find_renames(days, {("meta", "c1", RENAMED)}) == []
    assert len(find_renames(days, {("meta", "c2", RENAMED)})) == 1
    assert len(find_renames(days, {("google", "c1", RENAMED)})) == 1


def test_a_name_that_continues_into_the_next_month_gives_one_candidate() -> None:
    days = [seen(1, TAGGED), seen(29, RENAMED), seen(30, RENAMED), seen(1, RENAMED, month=7)]
    found = find_renames(days)
    assert [c.period for c in found] == [date(2026, 6, 1)]
    assert found[0].amount == Decimal("20.00")  # Only the month of the candidate.


def test_two_new_names_in_one_month_are_one_candidate() -> None:
    days = [seen(1, TAGGED), seen(10, RENAMED), seen(20, OTHER, "5.50")]
    found = find_renames(days)
    assert len(found) == 1
    assert found[0].new_names == (RENAMED, OTHER)
    assert found[0].renamed_from == date(2026, 6, 10)
    assert found[0].amount == Decimal("15.50")


def test_new_names_first_seen_in_different_months_are_two_candidates() -> None:
    days = [seen(1, TAGGED), seen(20, RENAMED), seen(3, OTHER, month=7)]
    found = find_renames(days)
    assert [(c.period, c.new_names) for c in found] == [
        (date(2026, 6, 1), (RENAMED,)),
        (date(2026, 7, 1), (OTHER,)),
    ]


def test_return_to_the_tagged_name_does_not_hide_the_rename() -> None:
    found = find_renames([seen(1, TAGGED), seen(10, RENAMED), seen(11, TAGGED)])
    assert [c.new_names for c in found] == [(RENAMED,)]


def test_a_null_spend_day_adds_nothing_to_the_amount() -> None:
    found = find_renames([seen(1, TAGGED), seen(15, RENAMED, None), seen(16, RENAMED, "1.25")])
    assert found[0].amount == Decimal("1.25")


def test_campaigns_are_judged_separately_and_in_key_order() -> None:
    days = [
        seen(1, TAGGED, campaign="c2"),
        seen(15, RENAMED, campaign="c2"),
        seen(1, TAGGED, campaign="c1"),
        seen(1, "Plain", campaign="c3"),
        seen(15, RENAMED, campaign="c1", platform="google"),
    ]
    found = find_renames(days)
    assert [(c.platform, c.platform_campaign_id) for c in found] == [("meta", "c2")]


def test_issue_key_is_per_platform_campaign_and_month() -> None:
    candidate = find_renames([seen(1, TAGGED), seen(15, RENAMED)])[0]
    assert issue_key(candidate) == "reconciliation:campaign_renamed:meta:c1:2026-06"


def test_rule_draft_proposes_the_cautious_action() -> None:
    candidate = find_renames([seen(1, TAGGED), seen(15, RENAMED, "12.34")])[0]
    draft = rename_draft(candidate, rule_verdict())
    assert draft.type == templates.CAMPAIGN_RENAMED == "reconciliation_campaign_renamed"
    assert (draft.severity, draft.client, draft.platform) == ("medium", "ACME", "meta")
    assert (draft.entity_type, draft.entity_id) == ("campaign", "c1")
    assert draft.period == date(2026, 6, 1) and draft.amount == Decimal("12.34")
    assert draft.proposed_action == templates.RENAME_CHECK_ACTION
    assert draft.explanation == (
        'ACME meta 2026-06: campaign c1 is reported as "Acme Meta – Promo June" from 2026-06-15 '
        'instead of "Acme | Meta | 2026-06 [ACME-META-2606]". The new name has no campaign ref '
        "tag; delivery stays linked by campaign id. Spend reported under the new name this "
        "month: EUR 12.34."
    )


def test_model_note_is_quoted_as_unverified_data() -> None:
    candidate = find_renames([seen(1, TAGGED), seen(15, RENAMED)])[0]
    verdict = RenameVerdict(templates.RENAME_CONFIRM_ACTION, note='Same "brand" and month.')
    draft = rename_draft(candidate, verdict)
    assert draft.proposed_action == templates.RENAME_CONFIRM_ACTION
    assert draft.explanation.endswith(" Model note (unverified): \"Same 'brand' and month.\".")


def test_codes_say_why_there_is_no_model_judgment() -> None:
    candidate = find_renames([seen(1, TAGGED), seen(15, RENAMED)])[0]
    draft = rename_draft(candidate, rule_verdict("model:invalid"))
    assert draft.explanation.endswith(" No model judgment (model:invalid).")


def test_quoted_text_is_cleaned_and_cut() -> None:
    assert templates.quoted("a​b\nc\x00d  e") == '"ab c d e"'
    assert templates.quoted("x" * 10, limit=5) == '"xxxx…"'
