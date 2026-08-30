import os
import uuid
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
)

from dotenv import load_dotenv

from services.supabase_service import public_client, admin_client
from services.auth_service import login_required, role_required, current_profile
from services.dtr_service import parse_csv, evaluate_day
from services.payroll_service import calculate

# ---------------------------------------------------------
# RAG
# ---------------------------------------------------------

try:
    from services.rag_service import assess_complaint
except Exception as e:
    print("RAG IMPORT ERROR:", repr(e))
    assess_complaint = None


# ---------------------------------------------------------
# APP SETUP
# ---------------------------------------------------------

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "dev-only-change-this-secret"
)


# ---------------------------------------------------------
# PROFILE CONTEXT
# ---------------------------------------------------------

@app.context_processor
def inject_profile():
    try:
        profile = current_profile() if session.get("user_id") else None
    except Exception:
        profile = None

    return {
        "current_profile": profile
    }


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "message": "BulSU Payroll System is running"
    })


# ---------------------------------------------------------
# ENVIRONMENT TEST
# ---------------------------------------------------------

@app.route("/test-env")
def test_env():

    return jsonify({
        "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
        "SUPABASE_ANON_KEY": bool(os.getenv("SUPABASE_ANON_KEY")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        ),
        "FLASK_SECRET_KEY": bool(os.getenv("FLASK_SECRET_KEY")),
        "HF_TOKEN": bool(os.getenv("HF_TOKEN")),
    })


# ---------------------------------------------------------
# DATABASE TEST
# ---------------------------------------------------------

@app.route("/test-db")
def test_db():

    try:
        db = admin_client()

        db.table("profiles").select("id").limit(1).execute()

        return jsonify({
            "status": "success",
            "database": "connected",
            "message": "Supabase database connection is working."
        }), 200

    except Exception as e:

        print("DATABASE TEST ERROR:", repr(e))

        return jsonify({
            "status": "error",
            "database": "not connected",
            "error": str(e)
        }), 500


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.route("/")
def index():

    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# ---------------------------------------------------------
# LOGIN
# ---------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("login.html")

        try:
            # Login to Supabase
            supabase = public_client()

            auth_response = supabase.auth.sign_in_with_password({
    "email": email,
    "password": password
})

if not auth_response.user:
    flash("Invalid email or password.", "error")
    return render_template("login.html")

user_id = str(auth_response.user.id)

# PUT IT HERE ↓↓↓

profile_response = (
    supabase.table("profiles")
    .select("id,full_name,role,approved")
    .eq("id", user_id)
    .execute()
)

profiles = profile_response.data or []

if len(profiles) == 0:
    flash(
        f"Profile not found for user ID: {user_id}",
        "error"
    )
    return render_template("login.html")

profile = profiles[0]

            if not profile.get("approved", False):
                flash(
                    "Your account is still waiting for Super Admin approval.",
                    "warning"
                )
                return render_template("login.html")

            # Successful login
            session.clear()

            session["user_id"] = user_id
            session["email"] = auth_response.user.email
            session["full_name"] = profile.get("full_name", "")
            session["role"] = profile.get("role", "employee")

            flash("Login successful.", "success")

            return redirect(url_for("dashboard"))

        except Exception as e:
            print("LOGIN ERROR:", repr(e))
            flash(f"Login error: {str(e)}", "error")

    return render_template("login.html")

# ---------------------------------------------------------
# REGISTER
# ---------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not full_name:

            flash(
                "Please enter your full name.",
                "danger"
            )

            return render_template("register.html")

        if not email:

            flash(
                "Please enter your email address.",
                "danger"
            )

            return render_template("register.html")

        if len(password) < 6:

            flash(
                "Password must contain at least 6 characters.",
                "danger"
            )

            return render_template("register.html")

        if password != confirm_password:

            flash(
                "Passwords do not match.",
                "danger"
            )

            return render_template("register.html")

        try:

            supabase = public_client()

            result = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "full_name": full_name
                    }
                }
            })

            if not result.user:

                flash(
                    "Supabase did not create the account.",
                    "danger"
                )

                return render_template("register.html")

            user_id = str(result.user.id)

            # Create profile if database trigger did not.
            try:

                db = admin_client()

                existing_profile = (
                    db.table("profiles")
                    .select("id")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )

                if not existing_profile:

                    db.table("profiles").insert({
                        "id": user_id,
                        "full_name": full_name,
                        "role": "employee",
                        "approved": False
                    }).execute()

            except Exception as profile_error:

                print(
                    "PROFILE CREATION WARNING:",
                    repr(profile_error)
                )

            flash(
                "Registration successful. "
                "Please confirm your email if required, "
                "then wait for Super Admin approval.",
                "success"
            )

            return redirect(url_for("login"))

        except Exception as e:

            print("REGISTER ERROR:", repr(e))

            flash(
                f"Registration error: {str(e)}",
                "danger"
            )

    return render_template("register.html")


# ---------------------------------------------------------
# FORGOT PASSWORD
# ---------------------------------------------------------

@app.route(
    "/forgot-password",
    methods=["GET", "POST"]
)
def forgot_password():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        if not email:

            flash(
                "Please enter your email address.",
                "danger"
            )

            return render_template(
                "forgot_password.html"
            )

        try:

            reset_url = url_for(
                "reset_password",
                _external=True,
                _scheme="https"
            )

            public_client().auth.reset_password_for_email(
                email,
                {
                    "redirect_to": reset_url
                }
            )

            # Generic response is better than revealing
            # whether an email address exists.
            flash(
                "If that email is registered, "
                "a password reset link has been sent.",
                "success"
            )

            return redirect(
                url_for("login")
            )

        except Exception as e:

            print(
                "FORGOT PASSWORD ERROR:",
                repr(e)
            )

            flash(
                f"Could not send the reset link: {str(e)}",
                "danger"
            )

    return render_template(
        "forgot_password.html"
    )


# ---------------------------------------------------------
# RESET PASSWORD
# ---------------------------------------------------------

@app.route(
    "/reset-password",
    methods=["GET", "POST"]
)
def reset_password():

    if request.method == "GET":

        return render_template(
            "reset_password.html"
        )

    password = request.form.get(
        "password",
        ""
    )

    confirm_password = request.form.get(
        "confirm_password",
        ""
    )

    access_token = request.form.get(
        "access_token",
        ""
    )

    refresh_token = request.form.get(
        "refresh_token",
        ""
    )

    if len(password) < 6:

        flash(
            "Password must contain at least 6 characters.",
            "danger"
        )

        return render_template(
            "reset_password.html"
        )

    if password != confirm_password:

        flash(
            "Passwords do not match.",
            "danger"
        )

        return render_template(
            "reset_password.html"
        )

    if not access_token or not refresh_token:

        flash(
            "The password reset link is invalid or expired. "
            "Please request another reset link.",
            "danger"
        )

        return redirect(
            url_for("forgot_password")
        )

    try:

        supabase = public_client()

        # Establish the recovery user's Supabase session.
        supabase.auth.set_session(
            access_token,
            refresh_token
        )

        supabase.auth.update_user({
            "password": password
        })

        flash(
            "Your password has been changed successfully. "
            "You may now sign in.",
            "success"
        )

        return redirect(
            url_for("login")
        )

    except Exception as e:

        print(
            "RESET PASSWORD ERROR:",
            repr(e)
        )

        flash(
            f"Could not reset password: {str(e)}",
            "danger"
        )

        return redirect(
            url_for("forgot_password")
        )


# ---------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():

    try:

        db = admin_client()
        profile = current_profile()

        if not profile:

            session.clear()

            flash(
                "Profile could not be found.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        role = profile.get("role")

        stats = {}

        if role in (
            "super_admin",
            "hr"
        ):

            employees_result = (
                db.table("employees")
                .select("id")
                .execute()
            )

            review_result = (
                db.table("dtr_entries")
                .select("id")
                .eq(
                    "requires_review",
                    True
                )
                .execute()
            )

            complaint_result = (
                db.table("complaints")
                .select("id")
                .in_(
                    "status",
                    [
                        "submitted",
                        "assessed"
                    ]
                )
                .execute()
            )

            stats["employees"] = len(
                employees_result.data or []
            )

            stats["review_dtr"] = len(
                review_result.data or []
            )

            stats["complaints"] = len(
                complaint_result.data or []
            )

        else:

            employee_result = (
                db.table("employees")
                .select("*")
                .eq(
                    "profile_id",
                    profile["id"]
                )
                .limit(1)
                .execute()
            )

            stats["employee"] = (
                employee_result.data[0]
                if employee_result.data
                else None
            )

        return render_template(
            "dashboard.html",
            stats=stats
        )

    except Exception as e:

        print(
            "DASHBOARD ERROR:",
            repr(e)
        )

        flash(
            f"Dashboard error: {str(e)}",
            "danger"
        )

        return redirect(
            url_for("login")
        )


# ---------------------------------------------------------
# EMPLOYEE MANAGEMENT
# ---------------------------------------------------------

@app.route(
    "/employees",
    methods=["GET", "POST"]
)
@login_required
@role_required(
    "super_admin",
    "hr"
)
def employees():

    db = admin_client()

    if request.method == "POST":

        payload = {
            "employee_no":
                request.form["employee_no"],

            "full_name":
                request.form["full_name"],

            "employee_type":
                request.form["employee_type"],

            "department":
                request.form.get("department"),

            "monthly_salary":
                float(
                    request.form.get(
                        "monthly_salary"
                    )
                    or 0
                ),

            "hourly_rate":
                float(
                    request.form.get(
                        "hourly_rate"
                    )
                    or 0
                ),

            "standard_hours":
                float(
                    request.form.get(
                        "standard_hours"
                    )
                    or 8
                ),

            "workdays_per_month":
                float(
                    request.form.get(
                        "workdays_per_month"
                    )
                    or 22
                ),

            "active":
                True
        }

        try:

            db.table(
                "employees"
            ).insert(
                payload
            ).execute()

            flash(
                "Employee added.",
                "success"
            )

        except Exception as e:

            print(
                "EMPLOYEE ERROR:",
                repr(e)
            )

            flash(
                f"Could not add employee: {str(e)}",
                "danger"
            )

        return redirect(
            url_for("employees")
        )

    rows = (
        db.table("employees")
        .select("*")
        .order("employee_no")
        .execute()
        .data
        or []
    )

    return render_template(
        "employees.html",
        employees=rows
    )


# ---------------------------------------------------------
# DTR UPLOAD
# ---------------------------------------------------------

@app.route(
    "/dtr/upload",
    methods=["GET", "POST"]
)
@login_required
@role_required(
    "super_admin",
    "hr"
)
def dtr_upload():

    db = admin_client()

    if request.method == "POST":

        uploaded_file = request.files.get(
            "file"
        )

        if not uploaded_file:

            flash(
                "Choose a CSV file.",
                "danger"
            )

            return redirect(
                request.url
            )

        try:

            rows = parse_csv(
                uploaded_file.read()
            )

            batch = str(
                uuid.uuid4()
            )

            inserted = 0
            flagged = 0
            errors = []

            for row in rows:

                employee = (
                    db.table("employees")
                    .select(
                        "id,standard_hours"
                    )
                    .eq(
                        "employee_no",
                        row["employee_no"]
                    )
                    .limit(1)
                    .execute()
                    .data
                    or []
                )

                if not employee:

                    errors.append(
                        f"Row {row['row_number']}: "
                        f"employee {row['employee_no']} not found"
                    )

                    continue

                evaluation = evaluate_day(
                    row["work_date"],
                    row["time_in"],
                    row["time_out"],
                    standard_hours=(
                        employee[0].get(
                            "standard_hours"
                        )
                        or 8
                    )
                )

                payload = {
                    "employee_id":
                        employee[0]["id"],

                    "work_date":
                        row["work_date"].isoformat(),

                    "time_in":
                        (
                            row["time_in"].isoformat()
                            if row["time_in"]
                            else None
                        ),

                    "time_out":
                        (
                            row["time_out"].isoformat()
                            if row["time_out"]
                            else None
                        ),

                    "status":
                        evaluation["status"],

                    "payable_hours":
                        float(
                            evaluation[
                                "payable_hours"
                            ]
                        ),

                    "reason":
                        evaluation["reason"],

                    "requires_review":
                        evaluation[
                            "requires_review"
                        ],

                    "import_batch":
                        batch
                }

                (
                    db.table("dtr_entries")
                    .upsert(
                        payload,
                        on_conflict=(
                            "employee_id,"
                            "work_date"
                        )
                    )
                    .execute()
                )

                inserted += 1

                flagged += int(
                    evaluation[
                        "requires_review"
                    ]
                )

            message = (
                f"DTR import complete: "
                f"{inserted} saved, "
                f"{flagged} flagged."
            )

            if errors:

                message += (
                    " "
                    + " | ".join(
                        errors[:5]
                    )
                )

            flash(
                message,
                "success"
            )

        except Exception as e:

            print(
                "DTR ERROR:",
                repr(e)
            )

            flash(
                f"DTR import failed: {str(e)}",
                "danger"
            )

        return redirect(
            url_for("dtr_upload")
        )

    recent = (
        db.table("dtr_entries")
        .select(
            "*,employees("
            "employee_no,"
            "full_name"
            ")"
        )
        .order(
            "work_date",
            desc=True
        )
        .limit(100)
        .execute()
        .data
        or []
    )

    return render_template(
        "dtr_upload.html",
        rows=recent
    )


# ---------------------------------------------------------
# PAYROLL
# ---------------------------------------------------------

@app.route(
    "/payroll",
    methods=["GET", "POST"]
)
@login_required
@role_required(
    "super_admin",
    "hr"
)
def payroll():

    db = admin_client()

    if request.method == "POST":

        try:

            start = request.form[
                "start_date"
            ]

            end = request.form[
                "end_date"
            ]

            cutoff = int(
                request.form[
                    "cutoff_no"
                ]
            )

            run = (
                db.table(
                    "payroll_runs"
                )
                .insert({
                    "start_date":
                        start,

                    "end_date":
                        end,

                    "cutoff_no":
                        cutoff,

                    "status":
                        "draft",

                    "created_by":
                        session["user_id"]
                })
                .execute()
                .data[0]
            )

            employee_rows = (
                db.table("employees")
                .select("*")
                .eq(
                    "active",
                    True
                )
                .execute()
                .data
                or []
            )

            for employee in employee_rows:

                dtr = (
                    db.table("dtr_entries")
                    .select("*")
                    .eq(
                        "employee_id",
                        employee["id"]
                    )
                    .gte(
                        "work_date",
                        start
                    )
                    .lte(
                        "work_date",
                        end
                    )
                    .execute()
                    .data
                    or []
                )

                deductions = (
                    db.table(
                        "employee_deductions"
                    )
                    .select("*")
                    .eq(
                        "employee_id",
                        employee["id"]
                    )
                    .eq(
                        "active",
                        True
                    )
                    .execute()
                    .data
                    or []
                )

                result = calculate(
                    employee,
                    start,
                    end,
                    cutoff,
                    dtr,
                    deductions
                )

                item = (
                    db.table(
                        "payroll_items"
                    )
                    .insert({
                        "payroll_run_id":
                            run["id"],

                        "employee_id":
                            employee["id"],

                        "gross_pay":
                            result["gross_pay"],

                        "total_deductions":
                            result[
                                "total_deductions"
                            ],

                        "net_pay":
                            result["net_pay"],

                        "attendance_summary":
                            {
                                "days":
                                    len(dtr),

                                "flagged":
                                    sum(
                                        1
                                        for row
                                        in dtr
                                        if row.get(
                                            "requires_review"
                                        )
                                    )
                            }
                    })
                    .execute()
                    .data[0]
                )

                for deduction in result[
                    "deductions"
                ]:

                    (
                        db.table(
                            "payroll_item_deductions"
                        )
                        .insert({
                            "payroll_item_id":
                                item["id"],

                            "deduction_id":
                                deduction.get(
                                    "id"
                                ),

                            "name":
                                (
                                    deduction.get(
                                        "name"
                                    )
                                    or
                                    deduction.get(
                                        "kind"
                                    )
                                ),

                            "amount":
                                deduction[
                                    "applied_amount"
                                ]
                        })
                        .execute()
                    )

            flash(
                "Payroll draft generated.",
                "success"
            )

        except Exception as e:

            print(
                "PAYROLL ERROR:",
                repr(e)
            )

            flash(
                f"Payroll generation failed: {str(e)}",
                "danger"
            )

        return redirect(
            url_for("payroll")
        )

    runs = (
        db.table("payroll_runs")
        .select("*")
        .order(
            "created_at",
            desc=True
        )
        .limit(20)
        .execute()
        .data
        or []
    )

    return render_template(
        "payroll.html",
        runs=runs
    )


# ---------------------------------------------------------
# PAYROLL DETAILS
# ---------------------------------------------------------

@app.route(
    "/payroll/<run_id>"
)
@login_required
@role_required(
    "super_admin",
    "hr"
)
def payroll_run(run_id):

    db = admin_client()

    run = (
        db.table("payroll_runs")
        .select("*")
        .eq(
            "id",
            run_id
        )
        .single()
        .execute()
        .data
    )

    items = (
        db.table("payroll_items")
        .select(
            "*,employees("
            "employee_no,"
            "full_name,"
            "employee_type"
            ")"
        )
        .eq(
            "payroll_run_id",
            run_id
        )
        .execute()
        .data
        or []
    )

    return render_template(
        "payroll_run.html",
        run=run,
        items=items
    )


# ---------------------------------------------------------
# APPROVE PAYROLL
# ---------------------------------------------------------

@app.route(
    "/payroll/<run_id>/approve",
    methods=["POST"]
)
@login_required
@role_required(
    "super_admin"
)
def approve_payroll(run_id):

    try:

        (
            admin_client()
            .table("payroll_runs")
            .update({
                "status":
                    "approved",

                "approved_by":
                    session["user_id"],

                "approved_at":
                    datetime.utcnow()
                    .isoformat()
            })
            .eq(
                "id",
                run_id
            )
            .execute()
        )

        flash(
            "Payroll approved.",
            "success"
        )

    except Exception as e:

        print(
            "APPROVAL ERROR:",
            repr(e)
        )

        flash(
            f"Could not approve payroll: {str(e)}",
            "danger"
        )

    return redirect(
        url_for(
            "payroll_run",
            run_id=run_id
        )
    )


# ---------------------------------------------------------
# COMPLAINTS
# ---------------------------------------------------------

@app.route(
    "/complaints",
    methods=["GET", "POST"]
)
@login_required
def complaints():

    db = admin_client()

    profile = current_profile()

    employee = None

    if profile["role"] == "employee":

        employee_rows = (
            db.table("employees")
            .select("*")
            .eq(
                "profile_id",
                profile["id"]
            )
            .limit(1)
            .execute()
            .data
            or []
        )

        if employee_rows:

            employee = employee_rows[0]

    if request.method == "POST":

        if profile["role"] in (
            "super_admin",
            "hr"
        ):

            employee_id = (
                request.form.get(
                    "employee_id"
                )
            )

        else:

            employee_id = (
                employee["id"]
                if employee
                else None
            )

        if not employee_id:

            flash(
                "No employee profile is linked to this account.",
                "danger"
            )

            return redirect(
                request.url
            )

        try:

            complaint = (
                db.table("complaints")
                .insert({
                    "employee_id":
                        employee_id,

                    "submitted_by":
                        profile["id"],

                    "subject":
                        request.form[
                            "subject"
                        ],

                    "complaint_text":
                        request.form[
                            "complaint_text"
                        ],

                    "status":
                        "submitted"
                })
                .execute()
                .data[0]
            )

            # ---------------------------------
            # RAG ASSESSMENT
            # ---------------------------------

            if assess_complaint:

                try:

                    result = assess_complaint(
                        complaint[
                            "complaint_text"
                        ],
                        employee_id
                    )

                    if isinstance(
                        result,
                        tuple
                    ):

                        assessment = result[0]

                        docs = (
                            result[1]
                            if len(result) > 1
                            else []
                        )

                    else:

                        assessment = result
                        docs = []

                    if isinstance(
                        assessment,
                        str
                    ):

                        assessment_data = {
                            "assessment":
                                assessment
                        }

                    else:

                        assessment_data = (
                            assessment
                        )

                    db.table(
                        "complaint_assessments"
                    ).insert({
                        "complaint_id":
                            complaint["id"],

                        "assessment":
                            assessment_data,

                        "retrieved_knowledge_ids":
                            [
                                doc.get("id")
                                for doc in docs
                                if isinstance(
                                    doc,
                                    dict
                                )
                            ],

                        "model":
                            os.getenv(
                                "HF_MODEL",
                                "huggingface"
                            )
                    }).execute()

                    (
                        db.table("complaints")
                        .update({
                            "status":
                                "assessed"
                        })
                        .eq(
                            "id",
                            complaint["id"]
                        )
                        .execute()
                    )

                except Exception as e:

                    print(
                        "RAG ERROR:",
                        repr(e)
                    )

                    flash(
                        "Complaint saved, but AI assessment failed: "
                        f"{str(e)}",
                        "warning"
                    )

            flash(
                "Complaint submitted.",
                "success"
            )

        except Exception as e:

            print(
                "COMPLAINT ERROR:",
                repr(e)
            )

            flash(
                f"Could not submit complaint: {str(e)}",
                "danger"
            )

        return redirect(
            url_for("complaints")
        )

    query = (
        db.table("complaints")
        .select(
            "*,"
            "employees("
            "employee_no,"
            "full_name"
            "),"
            "complaint_assessments("
            "assessment,"
            "created_at"
            ")"
        )
        .order(
            "created_at",
            desc=True
        )
    )

    if (
        profile["role"] == "employee"
        and employee
    ):

        query = query.eq(
            "employee_id",
            employee["id"]
        )

    rows = (
        query.limit(100)
        .execute()
        .data
        or []
    )

    if profile["role"] in (
        "super_admin",
        "hr"
    ):

        employee_list = (
            db.table("employees")
            .select(
                "id,"
                "employee_no,"
                "full_name"
            )
            .eq(
                "active",
                True
            )
            .execute()
            .data
            or []
        )

    else:

        employee_list = []

    return render_template(
        "complaints.html",
        complaints=rows,
        employees=employee_list,
        employee=employee
    )

# ---------------------------------------------------------
# INITIAL SUPER ADMIN SETUP
# ---------------------------------------------------------

@app.route(
    "/setup-super-admin",
    methods=["GET", "POST"]
)
def setup_super_admin():

    # We don't want people directly browsing to this URL.
    if request.method == "GET":
        return redirect(url_for("login"))

    try:
        db = admin_client()

        # -------------------------------------------------
        # Check whether a Super Admin already exists
        # -------------------------------------------------

        existing_admin = (
            db.table("profiles")
            .select("id")
            .eq("role", "super_admin")
            .limit(1)
            .execute()
            .data
            or []
        )

        if existing_admin:

            flash(
                "Super Admin setup is already disabled "
                "because a Super Admin account exists.",
                "warning"
            )

            return redirect(
                url_for("login")
            )

        # -------------------------------------------------
        # Read submitted information
        # -------------------------------------------------

        full_name = request.form.get(
            "admin_full_name",
            ""
        ).strip()

        email = request.form.get(
            "admin_email",
            ""
        ).strip()

        password = request.form.get(
            "admin_password",
            ""
        )

        confirm_password = request.form.get(
            "admin_confirm_password",
            ""
        )

        setup_secret = request.form.get(
            "admin_setup_secret",
            ""
        )

        # -------------------------------------------------
        # Verify setup secret
        # -------------------------------------------------

        expected_secret = os.getenv(
            "ADMIN_SETUP_SECRET"
        )

        if not expected_secret:

            flash(
                "Super Admin setup is not configured.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        # Use constant-time comparison for secret
        import hmac

        if not hmac.compare_digest(
            setup_secret,
            expected_secret
        ):

            flash(
                "Invalid Super Admin setup code.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        # -------------------------------------------------
        # Validation
        # -------------------------------------------------

        if not full_name:

            flash(
                "Please enter the administrator's full name.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        if not email:

            flash(
                "Please enter an email address.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        if len(password) < 8:

            flash(
                "Super Admin password must contain "
                "at least 8 characters.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        if password != confirm_password:

            flash(
                "Super Admin passwords do not match.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        # -------------------------------------------------
        # Create Supabase Auth account
        # -------------------------------------------------

        result = (
            public_client()
            .auth
            .sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "full_name": full_name
                    }
                }
            })
        )

        if not result.user:

            flash(
                "Could not create the Super Admin account.",
                "danger"
            )

            return redirect(
                url_for("login")
            )

        user_id = str(
            result.user.id
        )

        # -------------------------------------------------
        # Create / update profile
        # -------------------------------------------------

        db.table("profiles").upsert({
            "id": user_id,
            "full_name": full_name,
            "role": "super_admin",
            "approved": True
        }).execute()

        flash(
            "Super Admin account created successfully. "
            "You may now sign in.",
            "success"
        )

        return redirect(
            url_for("login")
        )

    except Exception as e:

        print(
            "SUPER ADMIN SETUP ERROR:",
            repr(e)
        )

        flash(
            f"Super Admin setup failed: {str(e)}",
            "danger"
        )

        return redirect(
            url_for("login")
        )
# ---------------------------------------------------------
# LOCAL RUN
# ---------------------------------------------------------

# Vercel finds this top-level variable:
# app = Flask(__name__)

@app.route("/debug-supabase")
def debug_supabase():
    try:
        db = admin_client()

        result = (
            db.table("profiles")
            .select("id,full_name,role,approved")
            .eq("id", "d51bc6e5-a078-49f8-a4d0-769f80e87472")
            .execute()
        )

        return jsonify({
            "success": True,
            "profile_found": bool(result.data),
            "profiles": result.data
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
