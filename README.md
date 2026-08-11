# Payroll System - DTR + Supabase + RAG (Vercel)

A Flask payroll MVP designed for Vercel deployment. It uses Supabase Postgres/Auth, DTR CSV imports, configurable employee deductions, payroll cut-offs, and RAG-assisted payroll complaint assessment.

## Core rule added
If a DTR has a **time-in but no valid time-out by 11:59 PM on the same work date**, the day is stored as `NO_LOGOUT_NO_PAY`, `payable_hours = 0`, and `requires_review = true`. The raw DTR is retained for audit/review.

## Roles
- `super_admin`: payroll generation/approval, HR pages, complaints
- `hr`: employee/DTR/payroll draft management and complaint review
- `employee`: own complaint submission (link `employees.profile_id` to their profile)

## Setup
1. Create a Supabase project.
2. Open Supabase SQL Editor and run `sql/schema.sql`.
3. In Authentication, create/register users. Set the first admin profile manually:
   `update public.profiles set role='super_admin', approved=true where id='USER_UUID';`
4. Copy `.env.example` to `.env` and fill all secrets.
5. Install: `pip install -r requirements.txt`
6. Seed RAG knowledge: `python scripts/seed_knowledge.py`
7. Local run: `python app.py`

## Vercel
Push this folder to GitHub, import it in Vercel, then add these Environment Variables in Project Settings:
`FLASK_SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`.

Do **not** put the Supabase service-role key or OpenAI key in browser JavaScript or commit `.env`.

## DTR CSV
See `samples/dtr_sample.csv`. Required logical columns are employee number, work date, time in and time out. Common aliases are accepted. A blank time-out triggers the no-pay rule.

## Payroll assumptions that HR should verify before production
The source guide explains deduction timing but does not include a complete, current government contribution/tax table. Therefore amounts are stored in `employee_deductions` and should be populated from the institution's authorized tables. Regular monthly payroll is modeled as half basic salary per cut-off less DTR attendance shortage. Casual/EBJO/COS/part-time payroll uses payable DTR hours times hourly rate.

## RAG complaint flow
1. Complaint is stored.
2. Complaint text is embedded.
3. Supabase `pgvector` retrieves relevant payroll/DTR knowledge chunks.
4. Recent employee DTR and payroll facts are added as structured context.
5. The model returns category, severity, likely cause, evidence, recommendation, confidence, and whether HR review is required.
6. AI output is advisory; pay-changing disputes should be reviewed by HR.
