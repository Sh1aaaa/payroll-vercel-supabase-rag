"""BIR tax calculation helpers for the BulSU Payroll Portal.

Compensation withholding uses BIR Annex E (effective 2023 onwards).
Professional EWT helpers are separate and must only be used for genuine
independent professional/service payments after HR has verified the payee's
classification and supporting documents. They are NOT automatically applied
to everyone labelled COS, EBJO, contractual, casual, or part-time.

All calculations use Decimal and return JSON-friendly primitive values.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class TaxConfigurationError(ValueError):
    """Raised when an employee/payee tax profile is incomplete or invalid."""


def to_decimal(value, field_name="amount"):
    if value is None or value == "":
        value = 0
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite number")
    return result


def money(value):
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def normalize_period(period_type):
    value = str(period_type or "SEMI_MONTHLY").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "DAY": "DAILY",
        "WEEK": "WEEKLY",
        "SEMIMONTHLY": "SEMI_MONTHLY",
        "TWICE_MONTHLY": "SEMI_MONTHLY",
        "MONTH": "MONTHLY",
    }
    value = aliases.get(value, value)
    if value not in ("DAILY", "WEEKLY", "SEMI_MONTHLY", "MONTHLY"):
        raise TaxConfigurationError(
            "tax_period_type must be DAILY, WEEKLY, SEMI_MONTHLY, or MONTHLY"
        )
    return value


# Annex E values: ranges are represented as the lower bound of each bracket.
# The daily last-bracket base is computed as PHP 6,034.30 to preserve continuity:
# 1,102.60 + 30% * (21,918 - 5,479) = 6,034.30. The PDF text extraction
# appears malformed at this cell ("6,034.00.30"); verify against BIR if needed.
COMPENSATION_BRACKETS = {
    "DAILY": [
        (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        (Decimal("685.01"), Decimal("685"), Decimal("0"), Decimal("0.15")),
        (Decimal("1096"), Decimal("1096"), Decimal("61.65"), Decimal("0.20")),
        (Decimal("2192"), Decimal("2192"), Decimal("280.85"), Decimal("0.25")),
        (Decimal("5479"), Decimal("5479"), Decimal("1102.60"), Decimal("0.30")),
        (Decimal("21918"), Decimal("21918"), Decimal("6034.30"), Decimal("0.35")),
    ],
    "WEEKLY": [
        (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        (Decimal("4808.01"), Decimal("4808"), Decimal("0"), Decimal("0.15")),
        (Decimal("7692"), Decimal("7692"), Decimal("432.60"), Decimal("0.20")),
        (Decimal("15385"), Decimal("15385"), Decimal("1971.20"), Decimal("0.25")),
        (Decimal("38462"), Decimal("38462"), Decimal("7740.45"), Decimal("0.30")),
        (Decimal("153846"), Decimal("153846"), Decimal("42355.65"), Decimal("0.35")),
    ],
    "SEMI_MONTHLY": [
        (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        (Decimal("10417.01"), Decimal("10417"), Decimal("0"), Decimal("0.15")),
        (Decimal("16667"), Decimal("16667"), Decimal("937.50"), Decimal("0.20")),
        (Decimal("33333"), Decimal("33333"), Decimal("4270.70"), Decimal("0.25")),
        (Decimal("83333"), Decimal("83333"), Decimal("16770.70"), Decimal("0.30")),
        (Decimal("333333"), Decimal("333333"), Decimal("91770.70"), Decimal("0.35")),
    ],
    "MONTHLY": [
        (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        (Decimal("20833.01"), Decimal("20833"), Decimal("0"), Decimal("0.15")),
        (Decimal("33333"), Decimal("33333"), Decimal("1875.00"), Decimal("0.20")),
        (Decimal("66667"), Decimal("66667"), Decimal("8541.80"), Decimal("0.25")),
        (Decimal("166667"), Decimal("166667"), Decimal("33541.80"), Decimal("0.30")),
        (Decimal("666667"), Decimal("666667"), Decimal("183541.80"), Decimal("0.35")),
    ],
}


def calculate_compensation_withholding(taxable_compensation, period_type="SEMI_MONTHLY"):
    """Calculate withholding on taxable regular compensation for one pay period.

    The caller must first determine taxable regular compensation, including
    taxable regular allowances and deducting only valid mandatory employee
    contributions/non-taxable items for that same payroll period.

    This function does not annualize compensation or calculate withholding on
    supplementary compensation such as bonuses/OT separately.
    """
    income = money(taxable_compensation)
    if income < ZERO:
        raise ValueError("Taxable compensation cannot be negative")
    period = normalize_period(period_type)
    applicable = [row for row in COMPENSATION_BRACKETS[period] if income >= row[0]]
    lower_bound, excess_over, base_tax, rate = applicable[-1]
    tax = base_tax + max(ZERO, income - excess_over) * rate
    tax = money(max(ZERO, tax))
    return {
        "tax_type": "COMPENSATION_WITHHOLDING",
        "schedule": "BIR_ANNEX_E_2023_ONWARDS",
        "period_type": period,
        "bracket_minimum": float(lower_bound),
        "excess_over": float(excess_over),
        "base_tax": float(base_tax),
        "tax_rate": float(rate),
        "taxable_compensation": float(income),
        "withholding_tax": float(tax),
    }


def calculate_individual_professional_ewt(
    gross_payment,
    current_year_gross_income,
    vat_registered=False,
    sworn_declaration_submitted=False,
):
    """Calculate EWT for a qualifying individual professional payee.

    RR 14-2018 rate: 5% if current-year gross income including this payment
    does not exceed PHP 3M and the payee is not VAT-registered; 10% if over
    PHP 3M or VAT-registered. Requiring the declaration flag for the 5% rate
    is a conservative system control. HR must verify the declaration and
    Certificate of Registration. current_year_gross_income is YTD receipts
    before the current payment; this helper adds gross_payment to the total.

    Do not use for a person who is actually an employee.
    """
    payment = money(gross_payment)
    ytd_gross_before_payment = money(current_year_gross_income)
    if payment < ZERO or ytd_gross_before_payment < ZERO:
        raise ValueError("Payment and annual gross income cannot be negative")
    annual_gross = money(ytd_gross_before_payment + payment)

    if vat_registered or annual_gross > Decimal("3000000") or not sworn_declaration_submitted:
        rate = Decimal("0.10")
        reason = "10_PERCENT_VAT_OR_OVER_THRESHOLD_OR_NO_DECLARATION"
    else:
        rate = Decimal("0.05")
        reason = "5_PERCENT_NON_VAT_WITH_DECLARATION_AND_WITHIN_THRESHOLD"

    tax = money(payment * rate)
    return {
        "tax_type": "EXPANDED_WITHHOLDING_TAX",
        "payee_type": "INDIVIDUAL_PROFESSIONAL",
        "gross_payment": float(payment),
        "current_year_gross_income": float(annual_gross),
        "tax_rate": float(rate),
        "withholding_tax": float(tax),
        "reason": reason,
        "legal_reference": "RR_14_2018",
    }


def calculate_nonindividual_professional_ewt(gross_payment, current_year_gross_income):
    """Calculate EWT for qualifying non-individual professional payees.

    RR 14-2018: 10% if gross income does not exceed PHP 720,000; 15% if it
    exceeds PHP 720,000. This is provided as a separate helper; normal
    employee payroll should not call it.
    """
    payment = money(gross_payment)
    annual_gross = money(current_year_gross_income)
    if payment < ZERO or annual_gross < ZERO:
        raise ValueError("Payment and annual gross income cannot be negative")
    rate = Decimal("0.15") if annual_gross > Decimal("720000") else Decimal("0.10")
    tax = money(payment * rate)
    return {
        "tax_type": "EXPANDED_WITHHOLDING_TAX",
        "payee_type": "NON_INDIVIDUAL_PROFESSIONAL",
        "gross_payment": float(payment),
        "current_year_gross_income": float(annual_gross),
        "tax_rate": float(rate),
        "withholding_tax": float(tax),
        "legal_reference": "RR_14_2018",
    }
