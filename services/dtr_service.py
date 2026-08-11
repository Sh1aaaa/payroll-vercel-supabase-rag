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
    # Full datetime values keep their actual date so next-day logout can be rejected.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p"):
        try:
            t = datetime.strptime(value, fmt).time()
            return datetime.combine(work_date, t)
        except ValueError:
            pass
    raise ValueError(f"Unsupported time: {value}")


def evaluate_day(work_date, time_in, time_out, break_minutes=60, standard_hours=8):
    """Apply the hard DTR rule before any salary calculation."""
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

    minutes = Decimal(str((time_out - time_in).total_seconds() / 60))
    if minutes > Decimal("300"):
        minutes -= Decimal(str(break_minutes))
    minutes = max(Decimal("0"), minutes)
    hours = (minutes / Decimal("60")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Regular hours are capped here; overtime can be handled as a separate approved item.
    hours = min(hours, Decimal(str(standard_hours)))
    return {"status": "VALID", "payable_hours": hours, "requires_review": False, "reason": "Valid same-day DTR."}


def parse_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")
    cols = {k: _find(reader.fieldnames, k) for k in HEADER_ALIASES}
    missing = [k for k, v in cols.items() if not v]
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
            "work_date": wd,
            "time_in": ti,
            "time_out": to,
        })
    return rows
