"""AI second: a model's opinion on a rename the rule already found (decision 047).

    names -> guards (threat model T1: names come from platform APIs)
          -> any finding: no model call, the cautious action
          -> LLMClient.complete_json (masking, strict schema, no tools, one retry)
          -> code picks the proposed action; the reason is quoted as unverified data

The model only chooses between two proposed actions and adds a short note. It never changes
the issue's type, key, amount or severity, and nothing here writes an alias: a person decides.
Cost-cap and provider errors are not caught: the pipeline decides what happens to the rest.
"""

import re
from decimal import Decimal

from app.extraction import guards
from app.extraction.schema import Score, Text
from app.llm.client import InputTooLarge, LLMClient, LLMOutputInvalid
from app.reconciliation import templates
from app.reconciliation.config import RENAME_CONFIRM_MIN_CONFIDENCE
from app.reconciliation.models import RenameCandidate, RenameVerdict
from app.reconciliation.renames import rule_verdict
from pydantic import BaseModel, ConfigDict

TASK = "campaign_rename"
PROMPT_ID = "campaign_rename_v1"
NAMES_TAG = re.compile(r"</?\s*names\b", re.IGNORECASE)

SUSPICIOUS_NAME = "name:suspicious_content"
MODEL_INVALID = "model:invalid"
REASON_DROPPED = "model:reason_dropped"


class RenameAnswer(BaseModel):
    """The only shape the model can answer in."""

    model_config = ConfigDict(extra="forbid", strict=True)

    same_campaign: bool
    confidence: Score
    reason: Text


def is_suspicious(text: str) -> bool:
    return bool(guards.scan(text)) or NAMES_TAG.search(text) is not None


def judge_input(candidate: RenameCandidate, client_name: str) -> str:
    """Only what the judgment needs; the names are wrapped as data."""
    later = "\n".join(f"later: {name}" for name in candidate.new_names)
    return (
        f"client: {client_name}\n"
        f"platform: {candidate.platform}\n"
        f"month: {templates.month_text(candidate.period)}\n"
        f"<names>\nearlier: {candidate.old_name}\n{later}\n</names>"
    )


def verdict_of(answer: RenameAnswer) -> RenameVerdict:
    if is_suspicious(answer.reason):
        return rule_verdict(REASON_DROPPED)
    confirm = answer.same_campaign and Decimal(answer.confidence) >= RENAME_CONFIRM_MIN_CONFIDENCE
    action = templates.RENAME_CONFIRM_ACTION if confirm else templates.RENAME_CHECK_ACTION
    return RenameVerdict(action=action, note=answer.reason)


def judge_rename(client: LLMClient, candidate: RenameCandidate, client_name: str) -> RenameVerdict:
    names = (candidate.old_name, *candidate.new_names, client_name)
    if any(is_suspicious(name) for name in names):
        return rule_verdict(SUSPICIOUS_NAME)
    text = judge_input(candidate, client_name)
    try:
        answer = client.complete_json(TASK, PROMPT_ID, text, RenameAnswer)
    except (InputTooLarge, LLMOutputInvalid):
        return rule_verdict(MODEL_INVALID)
    return verdict_of(answer.data)
