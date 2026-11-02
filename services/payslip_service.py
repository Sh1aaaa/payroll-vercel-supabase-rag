"""Create a printable employee payslip from a saved, approved payroll item."""
from io import BytesIO
from decimal import Decimal, ROUND_HALF_UP
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

CENT = Decimal('0.01')

def amount(value):
    return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)

def peso(value):
    return f'PHP {amount(value):,.2f}'

def safe(value):
    return escape(str(value or ''))

def generate_payslip_pdf(employee, payroll_item, payroll_run, deductions):
    """Return PDF bytes. Inputs must be fetched and authorized by caller."""
    if payroll_run.get('status') != 'approved':
        raise ValueError('Payroll must be approved before a payslip can be generated.')
    gross = amount(payroll_item['gross_pay'])
    total = amount(payroll_item['total_deductions'])
    net = amount(payroll_item['net_pay'])
    if gross - total != net:
        raise ValueError('Approved payroll totals are inconsistent.')
    ded_sum = sum((amount(d['amount']) for d in deductions), Decimal('0.00'))
    if ded_sum != total:
        raise ValueError('Saved deduction details do not match the approved total.')

    buff = BytesIO()
    doc = SimpleDocTemplate(buff, pagesize=A4, leftMargin=16*mm, rightMargin=16*mm,
                            topMargin=17*mm, bottomMargin=17*mm,
                            title='Bulacan State University - Payroll Payment Slip')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='PayslipTitle', parent=styles['Title'], alignment=TA_CENTER,
                              fontName='Helvetica-Bold', fontSize=16, leading=20, spaceAfter=3))
    styles.add(ParagraphStyle(name='PayslipSmall', parent=styles['Normal'], alignment=TA_CENTER,
                              fontSize=9, leading=12))
    normal = styles['Normal']
    story = [Paragraph('Bulacan State University', styles['PayslipTitle']),
             Paragraph('City of Malolos, Bulacan', styles['PayslipSmall']),
             Spacer(1, 5*mm),
             Paragraph('<b>PAYROLL PAYMENT SLIP</b>', styles['PayslipTitle']), Spacer(1, 4*mm),
             Paragraph(f"<b>Employee:</b> {safe(employee.get('full_name'))}", normal),
             Paragraph(f"<b>Employee Number:</b> {safe(employee.get('employee_no'))}", normal),
             Paragraph(f"<b>Classification:</b> {safe(employee.get('employee_type'))}", normal),
             Paragraph(f"<b>Department:</b> {safe(employee.get('department'))}", normal),
             Paragraph(f"<b>Period Covered:</b> {safe(payroll_run.get('start_date'))} to {safe(payroll_run.get('end_date'))}", normal),
             Paragraph(f"<b>Cutoff:</b> {safe(payroll_run.get('cutoff_no'))}", normal), Spacer(1, 5*mm)]

    rows = [['EARNINGS', 'AMOUNT'], ['Approved Gross Pay', peso(gross)],
            ['DEDUCTIONS', 'AMOUNT']]
    for deduction in deductions:
        rows.append([safe(deduction.get('name') or 'Deduction'), peso(deduction['amount'])])
    rows.extend([['TOTAL DEDUCTIONS', peso(total)], ['TAKE HOME PAY', peso(net)]])

    table = Table(rows, colWidths=[120*mm, 58*mm], repeatRows=1, hAlign='CENTER')
    layout = [('BOX',(0,0),(-1,-1),0.75,colors.HexColor('#223759')),
              ('INNERGRID',(0,0),(-1,-1),0.35,colors.HexColor('#CBD5E1')),
              ('FONTNAME',(0,0),(-1,-1),'Helvetica'),
              ('FONTSIZE',(0,0),(-1,-1),9),
              ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
              ('ALIGN',(1,0),(1,-1),'RIGHT'),
              ('TOPPADDING',(0,0),(-1,-1),6),
              ('BOTTOMPADDING',(0,0),(-1,-1),6)]
    for i, row in enumerate(rows):
        if row[0] in ('EARNINGS','DEDUCTIONS','TOTAL DEDUCTIONS','TAKE HOME PAY'):
            layout.extend([('BACKGROUND',(0,i),(-1,i),colors.HexColor('#DCE9F8')),
                           ('FONTNAME',(0,i),(-1,i),'Helvetica-Bold')])
    table.setStyle(TableStyle(layout))
    story.append(table)
    story.extend([Spacer(1,7*mm), Paragraph('This document summarizes the approved payroll record for the period shown.', styles['PayslipSmall'])])
    doc.build(story)
    buff.seek(0)
    return buff.getvalue()
