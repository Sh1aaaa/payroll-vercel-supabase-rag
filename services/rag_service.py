import json
import os
from openai import OpenAI
from services.supabase_service import admin_client


def _client():
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def embedding(text):
    res = _client().embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        input=text,
    )
    return res.data[0].embedding


def retrieve_policy(query, limit=5):
    vec = embedding(query)
    res = admin_client().rpc("match_knowledge", {
        "query_embedding": vec,
        "match_count": limit,
        "match_threshold": 0.30,
    }).execute()
    return res.data or []


def employee_context(employee_id):
    db = admin_client()
    payroll = db.table("payroll_items").select("*,payroll_runs(start_date,end_date,cutoff_no,status)").eq("employee_id", employee_id).order("created_at", desc=True).limit(3).execute().data or []
    dtr = db.table("dtr_entries").select("work_date,time_in,time_out,status,payable_hours,reason,requires_review").eq("employee_id", employee_id).order("work_date", desc=True).limit(15).execute().data or []
    return {"recent_payroll": payroll, "recent_dtr": dtr}


def assess_complaint(complaint, employee_id):
    docs = retrieve_policy(complaint)
    facts = employee_context(employee_id)
    policy_text = "\n\n".join([f"[{d.get('title','Policy')}] {d.get('content','')}" for d in docs])
    prompt = f'''You are an HR payroll complaint assessment assistant. Use ONLY the supplied payroll/DTR facts and policy excerpts. Do not invent missing records. The result assists HR; it is not a final legal or disciplinary decision.

COMPLAINT:
{complaint}

EMPLOYEE RECORDS:
{json.dumps(facts, default=str)}

RETRIEVED POLICY:
{policy_text}

Return ONLY valid JSON with these keys:
category (one of: dtr, missing_logout, late_undertime, deduction, tax, loan, contribution, salary_computation, other),
severity (low|medium|high),
likely_cause, explanation, recommended_action, evidence (array of short strings), confidence (0 to 1), needs_human_review (boolean).
Set needs_human_review=true when records conflict, evidence is missing, or the complaint could change a person's pay.'''
    res = _client().responses.create(
        model=os.getenv("OPENAI_CHAT_MODEL", "gpt-5.6-luna"),
        input=prompt,
    )
    text = res.output_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {
            "category": "other", "severity": "medium", "likely_cause": "AI response could not be parsed",
            "explanation": text[:1500], "recommended_action": "HR should review the complaint manually.",
            "evidence": [], "confidence": 0, "needs_human_review": True,
        }
    return result, docs, facts
