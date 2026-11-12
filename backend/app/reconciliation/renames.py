"""Campaign renames: a tagged campaign later reported under a name without its tag (decision 047).

Pure code, no AI and no database. The platform campaign id stays the same, so delivery stays
linked; what is lost is the `[campaign_ref]` a person would read. The rule finds every such
rename, and the issue is written whether or not a model is asked about it.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Set
from datetime import date
from decimal import Decimal

from app.reconciliation import templates
from app.reconciliation.models import (
    IssueDraft,
    NameObservation,
    RenameCandidate,
    RenameVerdict,
    month_start,
)

# The same pattern as `pipelines.load_plans.CAMPAIGN_REF_TAG`; `app` never imports `pipelines`.
CAMPAIGN_REF_TAG = re.compile(r"\[([^\[\]]+)\]\s*$")
ISSUE_KEY_PREFIX = "reconciliation:campaign_renamed:"
ENTITY_CAMPAIGN = "campaign"

CampaignKey = tuple[str, str]
# (platform, platform campaign id, alias) of aliases a person or model has already confirmed.
ConfirmedAlias = tuple[str, str, str]


def has_ref_tag(name: str) -> bool:
    return CAMPAIGN_REF_TAG.search(name) is not None


def issue_key(candidate: RenameCandidate) -> str:
    return (
        f"{ISSUE_KEY_PREFIX}{candidate.platform}:{candidate.platform_campaign_id}:"
        f"{templates.month_text(candidate.period)}"
    )


def by_campaign(
    observations: Iterable[NameObservation],
) -> dict[CampaignKey, list[NameObservation]]:
    grouped: dict[CampaignKey, list[NameObservation]] = defaultdict(list)
    for observation in sorted(observations, key=lambda o: o.date):
        grouped[(observation.platform, observation.platform_campaign_id)].append(observation)
    return dict(grouped)


def first_seen(days: Iterable[NameObservation]) -> dict[str, NameObservation]:
    """Each name's first reported day, in order of first appearance."""
    seen: dict[str, NameObservation] = {}
    for day in days:
        seen.setdefault(day.name, day)
    return seen


def campaign_renames(
    days: list[NameObservation], confirmed: Set[ConfirmedAlias]
) -> list[RenameCandidate]:
    """One candidate per month in which an untagged, unconfirmed later name first appears."""
    names = first_seen(days)
    old_name, *later = names
    if not has_ref_tag(old_name):
        return []
    first = days[0]
    unconfirmed = [
        name
        for name in later
        if not has_ref_tag(name)
        and (first.platform, first.platform_campaign_id, name) not in confirmed
    ]
    by_month: dict[date, list[str]] = defaultdict(list)
    for name in unconfirmed:
        by_month[month_start(names[name].date)].append(name)
    return [
        RenameCandidate(
            platform=first.platform,
            platform_campaign_id=first.platform_campaign_id,
            client=first.client,
            period=period,
            old_name=old_name,
            new_names=tuple(new_names),
            renamed_from=min(names[name].date for name in new_names),
            amount=sum(
                (
                    day.spend
                    for day in days
                    if day.name in new_names
                    and day.spend is not None
                    and month_start(day.date) == period
                ),
                Decimal("0.00"),
            ),
        )
        for period, new_names in sorted(by_month.items())
    ]


def find_renames(
    observations: Iterable[NameObservation], confirmed: Set[ConfirmedAlias] = frozenset()
) -> list[RenameCandidate]:
    """Renamed campaigns in (platform, campaign id, month) order."""
    return [
        candidate
        for _, days in sorted(by_campaign(observations).items())
        for candidate in campaign_renames(days, confirmed)
    ]


def rule_verdict(*codes: str) -> RenameVerdict:
    """The cautious action, used whenever no model judgment is available."""
    return RenameVerdict(action=templates.RENAME_CHECK_ACTION, codes=codes)


def rename_draft(candidate: RenameCandidate, verdict: RenameVerdict) -> IssueDraft:
    return IssueDraft(
        dedup_key=issue_key(candidate),
        type=templates.CAMPAIGN_RENAMED,
        severity=templates.RENAME_SEVERITY,
        client=candidate.client,
        platform=candidate.platform,
        period=candidate.period,
        entity_type=ENTITY_CAMPAIGN,
        entity_id=candidate.platform_campaign_id,
        amount=candidate.amount,
        explanation=templates.rename_explanation(candidate, verdict),
        proposed_action=verdict.action,
    )
