# Payroll and DTR Rules

## Same-day logout requirement
A Daily Time Record with a time-in but no valid time-out recorded by 11:59 PM on the same work date is a NO_LOGOUT_NO_PAY record. Payable hours for that day are zero. The record must be retained and flagged for HR review; the system must not invent or automatically infer a logout time.

## Regular, temporary and contractual employees
Late and undertime affect salary. During the first cut-off, mandatory PAG-IBIG, PhilHealth and GSIS contributions and applicable employee contributions are deducted. Regular/temporary/contractual loans are deducted in the first cut-off. Applicable tax follows the configured tax table. During the second cut-off, the source guide states there are no regular mandatory deductions and employees receive half basic salary, subject to DTR attendance adjustments implemented by the system.

## Casual employees
Late and undertime are salary deductions. PAG-IBIG, PhilHealth and GSIS are deducted in the first cut-off. Staff employee contribution is deducted during the second cut-off. Mandatory PAG-IBIG/GSIS loans are whole in the first cut-off; COOP/Balikatan-type loans are split across the two cut-offs. Applicable tax may be deducted in both cut-offs.

## EBJO, COS and part-time employees
Late and undertime are salary deductions. PAG-IBIG is mandatory in the first cut-off. PhilHealth is voluntary if requested. SSS is voluntary for EBJO/COS if requested. Employee contribution is deducted in the second cut-off. PAG-IBIG loan is whole in the first cut-off; COOP/Balikatan-type loans may be split across cut-offs. Tax rules must be configured and reviewed by HR.

## Complaint assessment
AI assessment is advisory. It must cite DTR/payroll facts and retrieved payroll rules, flag uncertainty, and send pay-changing disputes to human HR review.
