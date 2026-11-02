import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

HEADER_ALIASES = {
    "employee_no": ["employee_no", "employee id", "employee_id", "id", "emp_no", "emp id"],
    "work_date": ["work_date", "date", "dtr_date", "day"],
    "time_in": ["time_in", "time in", "login", "log_in", "in"],
    "time_out": ["time_out", "time out", "logout", "log_out", "out"],
}

def _norm(s):
    return str(s or "").strip().lower().replace("-", "_")

def _find(headers, key):
    normalized = {_norm(h): h for h in headers}
    for alias in HEADER_ALIASES[key]:
        if _norm(alias) in normalized:
            return normalized[_norm(alias)]
    return None

def parse_date(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date: {value}")

def parse_datetime_on_date(value, work_date):
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.combine(work_date, datetime.strptime(value, fmt).time())
        except ValueError:
            pass
    raise ValueError(f"Unsupported time: {value}")

def evaluate_day(work_date, time_in, time_out, break_minutes=60, standard_hours=8):
    """Preserve original DTR pay and validation rules."""
    if time_in and not time_out:
        return {"status": "NO_LOGOUT_NO_PAY", "payable_hours": Decimal("0"), "requires_review": True,
                "reason": "Time-in exists but no time-out was recorded by 11:59 PM on the same work date."}
    if not time_in and not time_out:
        return {"status": "ABSENT", "payable_hours": Decimal("0"), "requires_review": False,
                "reason": "No DTR record for the day."}
    if not time_in and time_out:
        return {"status": "NO_LOGIN_NO_PAY", "payable_hours": Decimal("0"), "requires_review": True,
                "reason": "Time-out exists but no valid time-in exists."}
    deadline = datetime.combine(work_date, time(23, 59, 59))
    if time_out > deadline or time_out.date() != work_date:
        return {"status": "NO_LOGOUT_NO_PAY", "payable_hours": Decimal("0"), "requires_review": True,
                "reason": "Logout was not completed by 11:59 PM on the same work date."}
    if time_in.date() != work_date or time_out <= time_in:
        return {"status": "INVALID_DTR_NO_PAY", "payable_hours": Decimal("0"), "requires_review": True,
                "reason": "Invalid time sequence or time-in date."}
    minutes = Decimal(str((time_out-time_in).total_seconds()/60))
    if minutes > Decimal("300"):
        minutes -= Decimal(str(break_minutes))
    minutes = max(Decimal("0"), minutes)
    hours = (minutes/Decimal("60")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    hours = min(hours, Decimal(str(standard_hours)))
    return {"status": "VALID", "payable_hours": hours, "requires_review": False, "reason": "Valid same-day DTR."}

def parse_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")
    cols = {k:_find(reader.fieldnames,k) for k in HEADER_ALIASES}
    missing = [k for k,v in cols.items() if not v]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))
    rows = []
    for i, raw in enumerate(reader, start=2):
        wd = parse_date(raw[cols["work_date"]])
        ti = parse_datetime_on_date(raw[cols["time_in"]], wd)
        to = parse_datetime_on_date(raw[cols["time_out"]], wd)
        rows.append({
            "row_number": i,
            "employee_no": str(raw[cols["employee_no"]]).strip(),
            "work_date": wd, "time_in": ti, "time_out": to,
        })
    return rows

# --- Added only to populate snapshot metadata on newly generated payroll ---
def _dtr_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

def payroll_attendance_summary(employee, dtr_rows, shift_start=None, shift_end=None):
    """Read-only snapshot helper. Does NOT change gross pay or deductions.

    Only calculate lateness/undertime when real approved shift clock times
    are provided. Inputs are HH:MM local clock times, e.g. "08:00", "17:00".
    """
    hours = sum((Decimal(str(r.get("payable_hours") or 0)) for r in dtr_rows), Decimal("0"))
    emp_type = str(employee.get("employee_type") or "regular").lower()
    if emp_type in ("casual", "ebjo", "cos", "part_time"):
        rate = Decimal(str(employee.get("hourly_rate") or 0))
    else:
        monthly = Decimal(str(employee.get("monthly_salary") or 0))
        workdays = Decimal(str(employee.get("workdays_per_month") or 22))
        standard_hours = Decimal(str(employee.get("standard_hours") or 8))
        if workdays <= 0 or standard_hours <= 0:
            raise ValueError("Invalid employee workdays or standard hours")
        rate = monthly / workdays / standard_hours

    result = {
        "hours_worked": float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "rate_per_hour": float(rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
    }
    if not shift_start or not shift_end:
        return result
    try:
        start = time.fromisoformat(shift_start)
        end = time.fromisoformat(shift_end)
    except ValueError as exc:
        raise ValueError("Shift times must use HH:MM format") from exc
    if end <= start:
        raise ValueError("Overnight shifts require a separate attendance policy")

    late = Decimal("0")
    undertime = Decimal("0")
    for row in dtr_rows:
        if row.get("status") != "VALID":
            continue
        clock_in = _dtr_datetime(row.get("time_in"))
        clock_out = _dtr_datetime(row.get("time_out"))
        if not clock_in or not clock_out:
            continue
        clock_start = datetime.combine(clock_in.date(), start, tzinfo=clock_in.tzinfo)
        clock_end = datetime.combine(clock_in.date(), end, tzinfo=clock_in.tzinfo)
        late += Decimal(str(max(0, (clock_in-clock_start).total_seconds()))) / Decimal("60")
        undertime += Decimal(str(max(0, (clock_end-clock_out).total_seconds()))) / Decimal("60")
    result["minutes_late"] = int(late.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    result["minutes_undertime"] = int(undertime.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return result
