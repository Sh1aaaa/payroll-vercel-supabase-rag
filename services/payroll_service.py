from decimal import Decimal, ROUND_HALF_UP
from services.supabase_service import admin_client

MONEY = Decimal("0.01")


def D(v):
    return Decimal(str(v or 0))


def money(v):
    return D(v).quantize(MONEY, rounding=ROUND_HALF_UP)


def attendance_amount(employee, payable_hours):
    emp_type = employee.get("employee_type", "regular")
    if emp_type in ("casual", "ebjo", "cos", "part_time"):
        rate = D(employee.get("hourly_rate"))
    else:
        monthly = D(employee.get("monthly_salary"))
        daily = monthly / D(employee.get("workdays_per_month") or 22)
        rate = daily / D(employee.get("standard_hours") or 8)
    return money(rate * D(payable_hours))


def cut_off_base(employee, start_date, end_date, dtr_rows):
    emp_type = employee.get("employee_type", "regular")
    valid_amount = sum((attendance_amount(employee, r.get("payable_hours", 0)) for r in dtr_rows), Decimal("0"))
    if emp_type in ("casual", "ebjo", "cos", "part_time"):
        return money(valid_amount)

    # Monthly employees receive half basic salary per cut-off, less DTR attendance shortages.
    monthly = D(employee.get("monthly_salary"))
    half_basic = monthly / Decimal("2")
    daily = monthly / D(employee.get("workdays_per_month") or 22)
    expected_days = sum(1 for r in dtr_rows if r.get("is_expected_workday", True))
    full_hours = D(employee.get("standard_hours") or 8)
    expected = daily * Decimal(expected_days)
    actual = sum((daily * min(D(r.get("payable_hours")) / full_hours, Decimal("1")) for r in dtr_rows), Decimal("0"))
    attendance_deduction = max(Decimal("0"), expected - actual)
    return money(max(Decimal("0"), half_basic - attendance_deduction))


def deduction_for_cutoff(d, employee_type, cutoff_no):
    kind = d.get("kind", "other")
    amount = D(d.get("amount"))
    split = d.get("split_rule", "whole")

    if kind in ("pagibig", "philhealth", "gsis", "sss"):
        return amount if cutoff_no == 1 else Decimal("0")
    if kind == "employee_contribution":
        return amount if ((employee_type == "casual" and cutoff_no == 2) or (employee_type in ("ebjo","cos","part_time") and cutoff_no == 2) or (employee_type in ("regular","temporary","contractual") and cutoff_no == 1)) else Decimal("0")
    if kind in ("mandatory_loan", "pagibig_loan", "gsis_loan"):
        return amount if cutoff_no == 1 else Decimal("0")
    if kind in ("coop_loan", "balikatan_loan") or split == "half_each":
        return amount / Decimal("2")
    if kind == "tax":
        if employee_type in ("casual", "ebjo", "cos", "part_time"):
            return amount
        return amount if cutoff_no == 1 else Decimal("0")
    return amount if cutoff_no == 1 else Decimal("0")


def calculate(employee, start_date, end_date, cutoff_no, dtr_rows, configured_deductions):
    gross = cut_off_base(employee, start_date, end_date, dtr_rows)
    emp_type = employee.get("employee_type", "regular")
    applied = []
    total_deductions = Decimal("0")
    for d in configured_deductions:
        amt = money(deduction_for_cutoff(d, emp_type, cutoff_no))
        if amt > 0:
            applied.append({**d, "applied_amount": float(amt)})
            total_deductions += amt
    total_deductions = money(total_deductions)
    net = money(max(Decimal("0"), gross - total_deductions))
    return {
        "gross_pay": float(gross),
        "total_deductions": float(total_deductions),
        "net_pay": float(net),
        "deductions": applied,
    }
