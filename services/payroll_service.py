"""Payroll calculations for BulSU Payroll Portal.

Configured deduction amounts represent monthly obligations. For split_rule:
  * half_each: divide between cutoffs 1 and 2 (rounding remainder on cutoff 2)
  * whole: follow the existing deduction-kind payment schedule

Only active deduction records are applied. The caller should still fetch active
records, and must not reuse an already-approved payroll run when rates change.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MONEY = Decimal("0.01")
ZERO = Decimal("0.00")


def D(value):
    """Convert a database numeric value to Decimal without float arithmetic."""
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid monetary/numeric value: {value!r}")


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
    """Return the configured deduction for a cutoff, before net-pay capping.

    'half_each' is handled first because government deductions in this database
    use kind='government' rather than kind='philhealth' or kind='gsis'.
    """
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

    # Existing 'whole' scheduling behavior, including the actual DB kind 'government'.
    if kind == "employee_contribution":
        second_cutoff_types = ("casual", "ebjo", "cos", "part_time")
        chosen_cutoff = 2 if emp_type in second_cutoff_types else 1
        return amount if cutoff == chosen_cutoff else ZERO

    if kind == "tax" and emp_type in ("casual", "ebjo", "cos", "part_time"):
        # Preserve previous rule: tax on every cutoff for these employee types.
        # Tax amount must be independently configured/verified by payroll staff.
        return amount

    # For government, loan, tax and all other 'whole' records, apply on cutoff 1.
    return amount if cutoff == 1 else ZERO


def calculate(employee, start_date, end_date, cutoff_no, dtr_rows, configured_deductions):
    """Calculate gross, deductions and net. No database writes occur here."""
    gross = cut_off_base(employee, start_date, end_date, dtr_rows)
    employee_type = str(employee.get("employee_type") or "regular").lower()
    applied = []
    total_deductions = ZERO

    for deduction in configured_deductions or []:
        intended_amount = money(
            deduction_for_cutoff(deduction, employee_type, cutoff_no)
        )
        if intended_amount <= ZERO:
            continue

        # Keep displayed/saved itemized deductions consistent with net pay.
        # Any unpaid remainder needs HR handling, not an automatic carryover.
        remaining_pay = max(ZERO, gross - total_deductions)
        applied_amount = money(min(intended_amount, remaining_pay))
        if applied_amount <= ZERO:
            continue

        applied.append({**deduction, "applied_amount": float(applied_amount)})
        total_deductions += applied_amount

    total_deductions = money(total_deductions)
    net = money(gross - total_deductions)

    return {
        "gross_pay": float(gross),
        "total_deductions": float(total_deductions),
        "net_pay": float(net),
        "deductions": applied,
    }
