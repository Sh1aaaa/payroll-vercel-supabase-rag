"""Payroll calculations for BulSU Payroll Portal.

Configured deduction amounts represent monthly obligations. For split_rule:
  * half_each: divide between cutoffs 1 and 2 (rounding remainder on cutoff 2)
  * whole: follow the existing deduction-kind payment schedule

Only active deduction records are applied. The caller should still fetch active
records, and must not reuse an already-approved payroll run when rates change.

Tax integration:
  * COMPENSATION: automatically calculate BIR Annex E withholding.
  * PROFESSIONAL_EWT: calculate RR 14-2018 EWT only for a verified independent
    individual professional; do not use for an employee relationship.
  * MANUAL: retain the existing configured kind='tax' deduction amount.
  * EXEMPT: no automatic withholding; requires an approved basis note.
  * REVIEW_REQUIRED: block payroll until HR classifies the payee correctly.

Taxable compensation is gross pay less only deduction rows explicitly marked
 tax_exempt_from_compensation=True. Mark only qualifying mandatory employee
contributions/non-taxable deductions in that column; do not infer it from kind.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from services.tax_service import (
    TaxConfigurationError,
    calculate_compensation_withholding,
    calculate_individual_professional_ewt,
)

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
TAX_TREATMENTS = {
    "COMPENSATION",
    "PROFESSIONAL_EWT",
    "MANUAL",
    "EXEMPT",
    "REVIEW_REQUIRED",
}


def D(value):
    """Convert a database numeric value to Decimal without float arithmetic."""
    if value is None or value == "":
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid monetary/numeric value: {value!r}")
    if not result.is_finite():
        raise ValueError(f"Invalid non-finite monetary/numeric value: {value!r}")
    return result


def money(value):
    return D(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def attendance_amount(employee, payable_hours):
    emp_type = str(employee.get("employee_type") or "regular").lower()
    hours = max(Decimal("0"), D(payable_hours))

    if emp_type in ("casual", "ebjo", "cos", "part_time"):
        rate = D(employee.get("hourly_rate"))
    else:
        monthly = D(employee.get("monthly_salary"))
        workdays = D(employee.get("workdays_per_month") or 22)
        standard_hours = D(employee.get("standard_hours") or 8)
        if workdays <= 0 or standard_hours <= 0:
            raise ValueError("Workdays per month and standard hours must be positive")
        rate = (monthly / workdays) / standard_hours

    return money(rate * hours)


def cut_off_base(employee, start_date, end_date, dtr_rows):
    """Calculate cutoff gross from the existing attendance/payroll model."""
    emp_type = str(employee.get("employee_type") or "regular").lower()
    if emp_type in ("casual", "ebjo", "cos", "part_time"):
        valid_amount = sum(
            (attendance_amount(employee, row.get("payable_hours", 0)) for row in dtr_rows),
            Decimal("0"),
        )
        return money(max(ZERO, valid_amount))

    monthly = D(employee.get("monthly_salary"))
    workdays = D(employee.get("workdays_per_month") or 22)
    full_hours = D(employee.get("standard_hours") or 8)
    if workdays <= 0 or full_hours <= 0:
        raise ValueError("Workdays per month and standard hours must be positive")

    half_basic = monthly / Decimal("2")
    daily = monthly / workdays
    expected_days = sum(1 for row in dtr_rows if row.get("is_expected_workday", True))
    expected = daily * Decimal(expected_days)
    actual = sum(
        (
            daily * min(max(D(row.get("payable_hours")), ZERO) / full_hours, Decimal("1"))
            for row in dtr_rows
        ),
        Decimal("0"),
    )
    attendance_deduction = max(ZERO, expected - actual)
    return money(max(ZERO, half_basic - attendance_deduction))


def deduction_for_cutoff(deduction, employee_type, cutoff_no):
    """Return configured deduction for a cutoff, before net-pay capping."""
    if deduction.get("active") is False:
        return ZERO

    cutoff = int(cutoff_no)
    if cutoff not in (1, 2):
        raise ValueError("cutoff_no must be 1 or 2")

    kind = str(deduction.get("kind") or "other").strip().lower()
    split = str(deduction.get("split_rule") or "whole").strip().lower()
    emp_type = str(employee_type or "regular").lower()
    amount = money(deduction.get("amount"))

    if amount < ZERO:
        raise ValueError(f"Deduction amount cannot be negative: {deduction.get('name', kind)}")

    if split == "half_each":
        first_half = money(amount / Decimal("2"))
        return first_half if cutoff == 1 else money(amount - first_half)
    if split != "whole":
        raise ValueError(f"Unsupported split_rule: {split}")

    if kind == "employee_contribution":
        second_cutoff_types = ("casual", "ebjo", "cos", "part_time")
        chosen_cutoff = 2 if emp_type in second_cutoff_types else 1
        return amount if cutoff == chosen_cutoff else ZERO

    if kind == "tax" and emp_type in ("casual", "ebjo", "cos", "part_time"):
        # Retained for MANUAL tax profiles only. This is not a BIR tax formula.
        return amount

    # Existing whole scheduling behavior: cutoff 1 for other kinds.
    return amount if cutoff == 1 else ZERO


def _tax_treatment(employee):
    value = str(employee.get("tax_treatment") or "REVIEW_REQUIRED").strip().upper()
    if value not in TAX_TREATMENTS:
        raise TaxConfigurationError(f"Unsupported tax_treatment {value!r}")
    if value == "REVIEW_REQUIRED":
        employee_no = employee.get("employee_no") or employee.get("id") or "unknown employee"
        raise TaxConfigurationError(
            f"Tax treatment is not configured for {employee_no}. Set tax_treatment in the employee record before generating payroll."
        )
    if value == "EXEMPT" and not str(employee.get("tax_exemption_basis") or "").strip():
        raise TaxConfigurationError(
            f"Tax-exempt profile for {employee.get('employee_no') or employee.get('id')} requires tax_exemption_basis."
        )
    return value


def _intended_deductions(configured_deductions, employee_type, cutoff_no, treatment):
    rows = []
    for deduction in configured_deductions or []:
        if deduction.get("active") is False:
            continue
        kind = str(deduction.get("kind") or "other").strip().lower()
        # Automatic tax treatment must replace, not double-charge, a manually
        # configured tax line. EXEMPT means no withholding tax is applied.
        if kind == "tax" and treatment in ("COMPENSATION", "PROFESSIONAL_EWT", "EXEMPT"):
            continue
        intended = money(deduction_for_cutoff(deduction, employee_type, cutoff_no))
        if intended > ZERO:
            rows.append((deduction, intended))
    return rows


def _apply_deduction_rows(rows, gross, starting_total=ZERO):
    applied = []
    total = money(starting_total)
    for deduction, intended_amount in rows:
        remaining_pay = max(ZERO, gross - total)
        applied_amount = money(min(intended_amount, remaining_pay))
        if applied_amount <= ZERO:
            continue
        applied.append({**deduction, "applied_amount": float(applied_amount)})
        total += applied_amount
    return applied, money(total)


def calculate(employee, start_date, end_date, cutoff_no, dtr_rows, configured_deductions):
    """Calculate gross, deductions, tax and net. No database writes occur here."""
    gross = cut_off_base(employee, start_date, end_date, dtr_rows)
    employee_type = str(employee.get("employee_type") or "regular").lower()
    treatment = _tax_treatment(employee)
    intended_rows = _intended_deductions(
        configured_deductions, employee_type, cutoff_no, treatment
    )

    tax_exempt_contributions = sum(
        (
            amount for deduction, amount in intended_rows
            if deduction.get("tax_exempt_from_compensation") is True
        ),
        Decimal("0"),
    )
    tax_exempt_contributions = min(gross, money(tax_exempt_contributions))
    taxable_compensation = money(max(ZERO, gross - tax_exempt_contributions))

    tax_details = {
        "tax_treatment": treatment,
        "withholding_tax": 0.0,
        "taxable_compensation": float(taxable_compensation),
        "tax_period_type": None,
        "tax_rate": 0.0,
    }
    tax_amount = ZERO
    generated_tax_row = None

    if treatment == "COMPENSATION":
        period = employee.get("tax_period_type") or "SEMI_MONTHLY"
        tax_result = calculate_compensation_withholding(taxable_compensation, period)
        tax_amount = money(tax_result["withholding_tax"])
        tax_details.update(tax_result)
        generated_tax_row = {
            "id": None,
            "name": "WITHHOLDING TAX",
            "kind": "tax",
            "source": "BIR_ANNEX_E",
            "applied_amount": float(tax_amount),
        }

    elif treatment == "PROFESSIONAL_EWT":
        # Explicitly for verified independent individual professionals only.
        # Annual gross income / VAT / declaration fields must be maintained by HR.
        ewt_result = calculate_individual_professional_ewt(
            gross_payment=gross,
            current_year_gross_income=employee.get("tax_current_year_gross_income", 0),
            vat_registered=bool(employee.get("tax_vat_registered", False)),
            sworn_declaration_submitted=bool(employee.get("tax_sworn_declaration_submitted", False)),
        )
        tax_amount = money(ewt_result["withholding_tax"])
        tax_details.update(ewt_result)
        generated_tax_row = {
            "id": None,
            "name": "EXPANDED WITHHOLDING TAX",
            "kind": "tax",
            "source": "RR_14_2018",
            "applied_amount": float(tax_amount),
        }

    elif treatment == "MANUAL":
        # Manual tax deduction amounts are still configured as kind='tax'.
        tax_details.update({
            "tax_type": "MANUAL_CONFIGURED_WITHHOLDING",
            "note": "No tax rate was inferred. Itemized tax amount comes from employee_deductions.",
        })

    elif treatment == "EXEMPT":
        tax_details.update({
            "tax_type": "DOCUMENTED_EXEMPTION",
            "exemption_basis": str(employee.get("tax_exemption_basis")).strip(),
        })

    # Apply explicitly marked mandatory contributions first, then withholding,
    # then other deductions. The calculated tax is never silently truncated:
    # if it cannot be withheld after the configured priority deductions, block
    # the run for HR review rather than recording an understated tax amount.
    contribution_rows = [
        pair for pair in intended_rows
        if pair[0].get("tax_exempt_from_compensation") is True
        or str(pair[0].get("kind") or "").strip().lower() == "employee_contribution"
    ]
    contribution_ids = {id(pair[0]) for pair in contribution_rows}
    other_rows = [pair for pair in intended_rows if id(pair[0]) not in contribution_ids]

    applied, total_deductions = _apply_deduction_rows(contribution_rows, gross)

    if treatment in ("COMPENSATION", "PROFESSIONAL_EWT") and tax_amount > ZERO:
        available = max(ZERO, gross - total_deductions)
        if tax_amount > available:
            raise ValueError(
                f"Calculated withholding tax ({tax_amount}) exceeds remaining pay ({available}). Review payroll deductions for employee {employee.get('employee_no') or employee.get('id')}."
            )
        applied.append(generated_tax_row)
        total_deductions = money(total_deductions + tax_amount)

    other_applied, total_deductions = _apply_deduction_rows(
        other_rows, gross, total_deductions
    )
    applied.extend(other_applied)

    # For MANUAL profiles, configured kind='tax' rows remain part of other_rows.
    if treatment == "MANUAL":
        manual_tax = sum(
            (money(row.get("applied_amount")) for row in applied
             if str(row.get("kind") or "").strip().lower() == "tax"),
            ZERO,
        )
        tax_amount = money(manual_tax)
        tax_details["withholding_tax"] = float(tax_amount)

    total_deductions = money(total_deductions)
    net = money(gross - total_deductions)
    tax_details["withholding_tax"] = float(tax_amount)

    return {
        "gross_pay": float(gross),
        "total_deductions": float(total_deductions),
        "net_pay": float(net),
        "deductions": applied,
        "tax_treatment": treatment,
        "taxable_compensation": float(taxable_compensation) if treatment == "COMPENSATION" else None,
        "withholding_tax": float(tax_amount),
        "tax_period_type": tax_details.get("period_type") if treatment == "COMPENSATION" else None,
        "tax_details": tax_details,
    }
