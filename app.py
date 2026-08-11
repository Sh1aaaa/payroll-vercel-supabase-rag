import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
from dotenv import load_dotenv
from services.supabase_service import public_client, admin_client
from services.auth_service import login_required, role_required, current_profile
from services.dtr_service import parse_csv, evaluate_day
from services.payroll_service import calculate
from services.rag_service import assess_complaint

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

@app.context_processor
def inject_profile():
    return {"current_profile": current_profile() if session.get("user_id") else None}

@app.get("/health")
def health():
    return {"ok": True}

@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("dashboard" if session.get("user_id") else "login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            res = public_client().auth.sign_in_with_password({"email": request.form["email"], "password": request.form["password"]})
            session["user_id"] = str(res.user.id)
            session["email"] = res.user.email
            return redirect(url_for("dashboard"))
        except Exception as e:
            flash("Invalid login or Supabase authentication error.", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        try:
            public_client().auth.sign_up({"email": request.form["email"], "password": request.form["password"], "options": {"data": {"full_name": request.form.get("full_name", "")}}})
            flash("Registration submitted. Ask the Super Admin to approve/assign your role.", "success")
            return redirect(url_for("login"))
        except Exception:
            flash("Could not register that account.", "danger")
    return render_template("register.html")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/dashboard")
@login_required
def dashboard():
    db = admin_client(); p = current_profile(); role=p.get("role")
    stats={}
    if role in ("super_admin","hr"):
        stats["employees"] = len(db.table("employees").select("id").execute().data or [])
        stats["review_dtr"] = len(db.table("dtr_entries").select("id").eq("requires_review", True).execute().data or [])
        stats["complaints"] = len(db.table("complaints").select("id").in_("status", ["submitted","assessed"]).execute().data or [])
    else:
        emp = db.table("employees").select("id").eq("profile_id", p["id"]).limit(1).execute().data
        stats["employee"] = emp[0] if emp else None
    return render_template("dashboard.html", stats=stats)

@app.route("/employees", methods=["GET", "POST"])
@login_required
@role_required("super_admin", "hr")
def employees():
    db=admin_client()
    if request.method == "POST":
        payload={
            "employee_no":request.form["employee_no"], "full_name":request.form["full_name"],
            "employee_type":request.form["employee_type"], "department":request.form.get("department"),
            "monthly_salary":float(request.form.get("monthly_salary") or 0), "hourly_rate":float(request.form.get("hourly_rate") or 0),
            "standard_hours":float(request.form.get("standard_hours") or 8), "workdays_per_month":float(request.form.get("workdays_per_month") or 22),
            "active":True
        }
        try:
            db.table("employees").insert(payload).execute(); flash("Employee added.","success")
        except Exception as e: flash(f"Could not add employee: {e}","danger")
        return redirect(url_for("employees"))
    rows=db.table("employees").select("*").order("employee_no").execute().data or []
    return render_template("employees.html", employees=rows)

@app.route("/dtr/upload", methods=["GET", "POST"])
@login_required
@role_required("super_admin", "hr")
def dtr_upload():
    db=admin_client()
    if request.method == "POST":
        f=request.files.get("file")
        if not f: flash("Choose a CSV file.","danger"); return redirect(request.url)
        try:
            rows=parse_csv(f.read()); batch=str(uuid.uuid4()); inserted=0; flagged=0; errors=[]
            for row in rows:
                emp=db.table("employees").select("id,standard_hours").eq("employee_no",row["employee_no"]).limit(1).execute().data
                if not emp:
                    errors.append(f"Row {row['row_number']}: employee {row['employee_no']} not found"); continue
                result=evaluate_day(row["work_date"],row["time_in"],row["time_out"],standard_hours=emp[0].get("standard_hours") or 8)
                payload={"employee_id":emp[0]["id"],"work_date":row["work_date"].isoformat(),"time_in":row["time_in"].isoformat() if row["time_in"] else None,"time_out":row["time_out"].isoformat() if row["time_out"] else None,"status":result["status"],"payable_hours":float(result["payable_hours"]),"reason":result["reason"],"requires_review":result["requires_review"],"import_batch":batch}
                db.table("dtr_entries").upsert(payload,on_conflict="employee_id,work_date").execute(); inserted+=1; flagged += int(result["requires_review"])
            flash(f"DTR import complete: {inserted} saved, {flagged} flagged. " + (" | ".join(errors[:5]) if errors else ""),"success")
        except Exception as e: flash(f"DTR import failed: {e}","danger")
        return redirect(url_for("dtr_upload"))
    recent=db.table("dtr_entries").select("*,employees(employee_no,full_name)").order("work_date",desc=True).limit(100).execute().data or []
    return render_template("dtr_upload.html", rows=recent)

@app.route("/payroll", methods=["GET", "POST"])
@login_required
@role_required("super_admin", "hr")
def payroll():
    db=admin_client()
    if request.method == "POST":
        start=request.form["start_date"]; end=request.form["end_date"]; cutoff=int(request.form["cutoff_no"])
        run=db.table("payroll_runs").insert({"start_date":start,"end_date":end,"cutoff_no":cutoff,"status":"draft","created_by":session["user_id"]}).execute().data[0]
        emps=db.table("employees").select("*").eq("active",True).execute().data or []
        for emp in emps:
            dtr=db.table("dtr_entries").select("*").eq("employee_id",emp["id"]).gte("work_date",start).lte("work_date",end).execute().data or []
            deductions=db.table("employee_deductions").select("*").eq("employee_id",emp["id"]).eq("active",True).execute().data or []
            result=calculate(emp,start,end,cutoff,dtr,deductions)
            item=db.table("payroll_items").insert({"payroll_run_id":run["id"],"employee_id":emp["id"],"gross_pay":result["gross_pay"],"total_deductions":result["total_deductions"],"net_pay":result["net_pay"],"attendance_summary":{"days":len(dtr),"flagged":sum(1 for x in dtr if x.get("requires_review"))}}).execute().data[0]
            for ded in result["deductions"]:
                db.table("payroll_item_deductions").insert({"payroll_item_id":item["id"],"deduction_id":ded.get("id"),"name":ded.get("name") or ded.get("kind"),"amount":ded["applied_amount"]}).execute()
        flash("Payroll draft generated.","success"); return redirect(url_for("payroll"))
    runs=db.table("payroll_runs").select("*").order("created_at",desc=True).limit(20).execute().data or []
    return render_template("payroll.html", runs=runs)

@app.get("/payroll/<run_id>")
@login_required
@role_required("super_admin", "hr")
def payroll_run(run_id):
    db=admin_client(); run=db.table("payroll_runs").select("*").eq("id",run_id).single().execute().data
    items=db.table("payroll_items").select("*,employees(employee_no,full_name,employee_type)").eq("payroll_run_id",run_id).execute().data or []
    return render_template("payroll_run.html", run=run, items=items)

@app.post("/payroll/<run_id>/approve")
@login_required
@role_required("super_admin")
def approve_payroll(run_id):
    admin_client().table("payroll_runs").update({"status":"approved","approved_by":session["user_id"],"approved_at":datetime.utcnow().isoformat()}).eq("id",run_id).execute()
    flash("Payroll approved.","success"); return redirect(url_for("payroll_run",run_id=run_id))

@app.route("/complaints", methods=["GET", "POST"])
@login_required
def complaints():
    db=admin_client(); p=current_profile(); emp=None
    if p["role"]=="employee":
        e=db.table("employees").select("*").eq("profile_id",p["id"]).limit(1).execute().data; emp=e[0] if e else None
    if request.method=="POST":
        employee_id=request.form.get("employee_id") if p["role"] in ("super_admin","hr") else (emp["id"] if emp else None)
        if not employee_id: flash("No employee profile is linked to this account.","danger"); return redirect(request.url)
        row=db.table("complaints").insert({"employee_id":employee_id,"submitted_by":p["id"],"subject":request.form["subject"],"complaint_text":request.form["complaint_text"],"status":"submitted"}).execute().data[0]
        try:
            assessment,docs,facts=assess_complaint(row["complaint_text"],employee_id)
            db.table("complaint_assessments").insert({"complaint_id":row["id"],"assessment":assessment,"retrieved_knowledge_ids":[d.get("id") for d in docs],"model":os.getenv("OPENAI_CHAT_MODEL","gpt-5.6-luna")}).execute()
            db.table("complaints").update({"status":"assessed"}).eq("id",row["id"]).execute()
        except Exception as e:
            flash(f"Complaint saved, but AI assessment could not run: {e}","warning")
        return redirect(url_for("complaints"))
    q=db.table("complaints").select("*,employees(employee_no,full_name),complaint_assessments(assessment,created_at)").order("created_at",desc=True)
    if p["role"]=="employee" and emp: q=q.eq("employee_id",emp["id"])
    rows=q.limit(100).execute().data or []
    emps=db.table("employees").select("id,employee_no,full_name").eq("active",True).execute().data or [] if p["role"] in ("super_admin","hr") else []
    return render_template("complaints.html", complaints=rows, employees=emps, employee=emp)

if __name__ == "__main__":
    app.run(debug=True)
