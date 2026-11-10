"""Every reconciliation threshold, in one place."""

from decimal import Decimal

# Rule 1 fires only when the variance is strictly above both.
VARIANCE_MIN_EUR = Decimal("1.00")
VARIANCE_MIN_SHARE = Decimal("0.005")

# Share of the campaign's highest daily spend that one missing day can account for.
MISSING_DAY_SPEND_FACTOR = Decimal("1")

# Rule 2 fires only when delivery is strictly below the first or above the second share of budget.
BUDGET_MIN_SHARE = Decimal("0.85")
BUDGET_MAX_SHARE = Decimal("1.15")

# Rule 3 (fee and credits on client invoices) fires only when the difference is strictly above.
CLIENT_INVOICE_MIN_EUR = Decimal("1.00")


def missing_days_limit(gap_days: int, highest_daily_spend: Decimal) -> Decimal:
    """The largest variance that a campaign's missing days can account for."""
    return gap_days * highest_daily_spend * MISSING_DAY_SPEND_FACTOR
