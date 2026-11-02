"""BulSU-style payroll payment slip PDF, using saved APPROVED payroll values.

Drop into services/payslip_service.py. Existing Flask call signature is preserved.
Optional approved details can be supplied inside payroll_item['attendance_summary']:
  hours_worked, minutes_late, minutes_undertime, rate_per_hour,
  equivalent_amount, underpayment, overpayment.
Missing values are shown as dashes; this module never fabricates them.
"""
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

CENT = Decimal("0.01")
BLUE = colors.HexColor("#DCE8F5")
INK = colors.HexColor("#111827")
BORDER = colors.HexColor("#454545")

# Names retained from the user's original payroll slip; other names are appended.
DEDUCTION_LABELS = [
    "WITHHOLDING TAX", "Additional withholding Tax", "PAG-IBIG FUND",
    "PHILHEALTH", "FACULTY FUND", "HMO", "COOP SAVINGS",
    "COOP SHARECAP", "PAG-IBIG MPL", "PAG-IBIG CL", "FAU LOAN/ELA",
    "PAG IBIG MP2", "MPCR", "MPCEM", "CHRISTMAS LOAN", "CALAMITY LOAN",
]
ALIASES = {
    "PAGIBIG": "PAG-IBIG FUND", "PAGIBIG FUND": "PAG-IBIG FUND",
    "PAG IBIG FUND": "PAG-IBIG FUND",
    "PAG IBIG": "PAG-IBIG FUND", "PAG-IBIG": "PAG-IBIG FUND",
    "PHILHEALTH": "PHILHEALTH", "GSIS": "GSIS", "SSS": "SSS",
}


def amount(value):
    try:
        return Decimal(str(0 if value is None or value == "" else value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid payroll amount: {value!r}") from exc


def peso(value):
    return f"{amount(value):,.2f}"


def label(value):
    return str(value or "-").replace("\n", " ").replace("\r", " ")


def truncate(text, font, size, width, pdf):
    text = label(text)
    if pdf.stringWidth(text, font, size) <= width:
        return text
    while text and pdf.stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text + "..."


def pretty_date(value):
    if not value:
        return "-"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%b %d, %Y")
    except ValueError:
        return str(value)


def canonical(name):
    plain = " ".join(str(name or "").replace("-", " ").upper().split())
    return ALIASES.get(plain, plain)


def generate_payslip_pdf(employee, payroll_item, payroll_run, deductions):
    """Return PDF bytes. Caller MUST verify logged-in employee owns this item."""
    if payroll_run.get("status") != "approved":
        raise ValueError("Payslip is available only for approved payroll.")

    gross = amount(payroll_item["gross_pay"])
    total = amount(payroll_item["total_deductions"])
    net = amount(payroll_item["net_pay"])
    if gross - total != net:
        raise ValueError("Approved gross/deduction/net amounts do not reconcile.")
    if deductions is None:
        raise ValueError("Deduction details have not been loaded.")
    detail_sum = sum((amount(row.get("amount")) for row in deductions), Decimal("0.00"))
    if detail_sum != total:
        raise ValueError("Saved deduction details do not match approved deductions.")

    grouped = {}
    for row in deductions:
        key = canonical(row.get("name") or "OTHER DEDUCTION")
        grouped[key] = grouped.get(key, Decimal("0.00")) + amount(row.get("amount"))
    # Display all categories from the supplied template; append any other saved categories.
    pairs = [(name, grouped.pop(canonical(name), None)) for name in DEDUCTION_LABELS]
    pairs.extend(sorted(grouped.items()))

    summary = payroll_item.get("attendance_summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    extra_rows = [
        ("Rate per hour", summary.get("rate_per_hour"), "per hour"),
        ("Hours work", summary.get("hours_worked"), "hours"),
        ("Minutes Late", summary.get("minutes_late"), "mins"),
        ("Minutes undertime", summary.get("minutes_undertime"), "mins"),
        ("Equivalent Amount", summary.get("equivalent_amount"), ""),
        ("Underpayment", summary.get("underpayment"), ""),
        ("Overpayment", summary.get("overpayment"), ""),
    ]

    buff = BytesIO()
    pdf = canvas.Canvas(buff, pagesize=A4, pageCompression=1)
    pdf.setTitle("Bulacan State University - Payroll Payment Slip")
    pw, ph = A4
    left, right = 17*mm, pw-17*mm
    width = right-left
    col1, col2 = left + width*.52, left + width*.82
    y = ph - 17*mm

    def text(x, baseline, value, size=8.5, bold=False, align="left"):
        font = "Helvetica-Bold" if bold else "Helvetica"
        pdf.setFont(font, size)
        pdf.setFillColor(INK)
        rendered = truncate(value, font, size, (right-left)-6*mm, pdf)
        if align == "center": pdf.drawCentredString(x, baseline, rendered)
        elif align == "right": pdf.drawRightString(x, baseline, rendered)
        else: pdf.drawString(x, baseline, rendered)

    def band(title, height=9*mm):
        nonlocal y
        pdf.setFillColor(BLUE)
        pdf.rect(left, y-height, width, height, fill=1, stroke=0)
        pdf.setStrokeColor(BORDER); pdf.setLineWidth(.65)
        pdf.rect(left, y-height, width, height, fill=0, stroke=1)
        text((left+right)/2, y-height+height*.32, title, 10, True, "center")
        y -= height

    def line_row(caption, value="", unit="", height=6.3*mm, bold=False, shade=False):
        nonlocal y
        if shade:
            pdf.setFillColor(BLUE); pdf.rect(left,y-height,width,height,fill=1,stroke=0)
        pdf.setStrokeColor(BORDER); pdf.setLineWidth(.35)
        pdf.rect(left,y-height,width,height,fill=0,stroke=1)
        pdf.line(col1,y,col1,y-height)
        if unit:
            pdf.line(col2,y,col2,y-height)
        baseline = y-height+height*.29
        text(left+2*mm,baseline,caption,8.0,bold)
        if value is not None and value != "":
            v = value
            text((col2 if unit else right)-2*mm,baseline,v,8.2,bold,"right")
        if unit:
            text(col2+2*mm,baseline,unit,7.7)
        y -= height

    # Header intentionally has no invented university seal, officer, or signature.
    text(pw/2,y,"Bulacan State University",15,True,"center"); y-=6*mm
    text(pw/2,y,"City of Malolos, Bulacan",9,False,"center"); y-=8*mm
    text(pw/2,y,"PAYROLL PAYMENT SLIP",13,True,"center"); y-=10*mm
    text(left+2*mm,y, f"{label(employee.get('department') or 'Department not specified')} | {label(employee.get('employee_type') or 'Employee')}", 9,True)
    y-=5.5*mm
    text(left+2*mm,y,f"Period Covered: {pretty_date(payroll_run.get('start_date'))} - {pretty_date(payroll_run.get('end_date'))}",8.4)
    y-=6*mm
    text(left+2*mm,y,f"Employee: {label(employee.get('full_name'))}",9,True); y-=5*mm
    text(left+2*mm,y,f"Employee No.: {label(employee.get('employee_no'))}     Cutoff: {label(payroll_run.get('cutoff_no'))}",8.3)
    y-=7*mm

    band("EARNINGS")
    for cap,val,unit in extra_rows:
        if val is None or val == "":
            displayed = "-"
        elif cap in ("Minutes Late","Minutes undertime"):
            displayed = str(val)
        else:
            displayed = peso(val)
        line_row(cap, displayed, unit)
    line_row("Gross Amount",peso(gross),bold=True,shade=True,height=7.3*mm)
    y-=3.5*mm
    band("DEDUCTIONS")
    # Compact rows scale to fit one A4 page, even with extra categories.
    available = y - 42*mm
    row_h = max(3.5*mm, min(5.2*mm, available/(len(pairs)+1)))
    for name,value in pairs:
        line_row(name, peso(value) if value is not None else "-", height=row_h)
    line_row("TOTAL DEDUCTIONS",peso(total),bold=True,shade=True,height=7*mm)
    y-=3.5*mm
    band("TAKE HOME PAY")
    line_row("TOTAL AMOUNT",peso(net),bold=True,shade=True,height=8*mm)
    y-=5*mm
    text(pw/2,y,"This payslip summarizes the approved payroll record for the period shown.",7.8,align="center")
    y-=6*mm
    text(pw/2,y,"Authorized certification/signature: not supplied",7.6,align="center")
    if y < 13*mm:
        raise ValueError(f"Payslip has too many deductions to fit on one page; y={y/mm:.1f}mm")
    pdf.showPage(); pdf.save()
    return buff.getvalue()
