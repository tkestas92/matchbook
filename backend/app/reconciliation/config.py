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

# Pacing: the agency's daily planning curve by weekday, Monday first.
PACING_WEEKDAY_WEIGHTS = tuple(
    Decimal(weight) for weight in ("1.00", "1.05", "1.05", "1.00", "0.95", "0.85", "0.85")
)
# The forecast extends the average of at most this many most recent days.
PACING_FORECAST_WINDOW_DAYS = 7
# Earlier days of a flight are too noisy to raise an issue on.
PACING_MIN_DAYS_FOR_ISSUE = 7
# Forecast as a share of budget: green inside the green bounds (inclusive), yellow inside the
# yellow bounds (inclusive), red outside them.
PACING_GREEN_MIN = Decimal("0.98")
PACING_GREEN_MAX = Decimal("1.15")
PACING_YELLOW_MIN = Decimal("0.85")
PACING_YELLOW_MAX = Decimal("1.25")

# A model judgment proposes confirming a rename only at or above this confidence.
RENAME_CONFIRM_MIN_CONFIDENCE = Decimal("0.80")


def missing_days_limit(gap_days: int, highest_daily_spend: Decimal) -> Decimal:
    """The largest variance that a campaign's missing days can account for."""
    return gap_days * highest_daily_spend * MISSING_DAY_SPEND_FACTOR
