import os
import uuid
import hmac
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
from services.auth_service import (
    login_required,
    role_required,
    current_profile,
)
from services.dtr_service import parse_csv, evaluate_day
from services.payroll_service import calculate


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()


# =========================================================
# RAG SERVICE
# =========================================================

try:
    from services.rag_service import assess_complaint
except Exception as e:
    print("RAG IMPORT ERROR:", repr(e))
    assess_complaint = None


# =========================================================
# FLASK APP
# IMPORTANT: Vercel needs this top-level variable.
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "dev-only-change-this-secret"
)


# =========================================================
# HEALTH / DEBUG
# =========================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "app": "BulSU Payroll Portal"
    })


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
        "ADMIN_SETUP_SECRET": bool(
            os.getenv("ADMIN_SETUP_SECRET")
        ),
    })


@app.route("/test-db")
def test_db():
    try:
        db = admin_client()

        result = (
            db.table("profiles")
            .select("id,full_name,role,approved")
            .limit(5)
            .execute()
        )

        return jsonify({
            "success": True,
            "count": len(result.data or []),
            "data": result.data or []
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# =========================================================
# TEMPORARY SUPABASE DEBUG
# =========================================================

@app.route("/debug-supabase")
def debug_supabase():
    """
    Temporary diagnostic route.

    Remove this route after login/profile troubleshooting
    is finished.
    """

    try:
        db = admin_client()

        target_id = "d51bc6e5-a078-49f8-a4d0-769f80e87472"

        result = (
            db.table("profiles")
            .select("id,full_name,role,approved")
            .eq("id", target_id)
            .execute()
        )

        return jsonify({
            "success": True,
            "profile_found": bool(result.data),
            "profiles": result.data or []
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash(
                "Email and password are required.",
                "error"
            )
            return render_template("login.html")

        try:
            # -------------------------------------------------
            # Authenticate with Supabase
            # -------------------------------------------------

            supabase = public_client()

            auth_response = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            user = auth_response.user

            if not user:
                flash(
                    "Invalid email or password.",
                    "error"
                )
                return render_template("login.html")

            user_id = str(user.id)

            print("LOGIN AUTH USER:", user_id)
            print("LOGIN AUTH EMAIL:", user.email)

            # -------------------------------------------------
            # Read profile using ADMIN CLIENT
            # -------------------------------------------------

            db = admin_client()

            profile_result = (
                db.table("profiles")
                .select("id,full_name,role,approved")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )

            print(
                "LOGIN PROFILE RESULT:",
                profile_result.data
            )

            profiles = profile_result.data or []

            if not profiles:
                flash(
                    "Your authentication account exists, "
                    "but no profile record was found.",
                    "error"
                )
                return render_template("login.html")

            profile = profiles[0]

            # -------------------------------------------------
            # Approval check
            # -------------------------------------------------

            if not profile.get("approved", False):
                flash(
                    "Your account is still waiting for "
                    "Super Admin approval.",
                    "warning"
                )
                return render_template("login.html")

            # -------------------------------------------------
            # Save session
            # -------------------------------------------------

            session.clear()

            session["user_id"] = user_id
            session["email"] = user.email
            session["full_name"] = (
                profile.get("full_name") or ""
            )
            session["role"] = (
                profile.get("role") or "employee"
            )

            flash(
                "Login successful.",
                "success"
            )

            return redirect(url_for("dashboard"))

        except Exception as e:
            print("LOGIN ERROR:", repr(e))

            flash(
                f"Login error: {str(e)}",
                "error"
            )

    return render_template("login.html")


# =========================================================
# REGISTER
# =========================================================

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
                "Full name is required.",
                "error"
            )
            return render_template("register.html")

        if not email:
            flash(
                "Email is required.",
                "error"
            )
            return render_template("register.html")

        if not password:
            flash(
                "Password is required.",
                "error"
            )
            return render_template("register.html")

        if password != confirm_password:
            flash(
                "Passwords do not match.",
                "error"
            )
            return render_template("register.html")

        if len(password) < 6:
            flash(
                "Password must contain at least 6 characters.",
                "error"
            )
            return render_template("register.html")

        try:
            supabase = public_client()

            response = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "full_name": full_name
                    }
                }
            })

            user = response.user

            if not user:
                flash(
                    "Could not create the account.",
                    "error"
                )
                return render_template("register.html")

            user_id = str(user.id)

            print(
                "REGISTER USER ID:",
                user_id
            )

            # -------------------------------------------------
            # Ensure profile exists
            # -------------------------------------------------

            try:
                db = admin_client()

                existing = (
                    db.table("profiles")
                    .select("id")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                )

                if not existing.data:

                    db.table("profiles").insert({
                        "id": user_id,
                        "full_name": full_name,
                        "role": "employee",
                        "approved": False
                    }).execute()

            except Exception as profile_error:
                print(
                    "PROFILE CREATE ERROR:",
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
            print(
                "REGISTER ERROR:",
                repr(e)
            )

            flash(
                f"Registration error: {str(e)}",
                "error"
            )

    return render_template("register.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    try:
        supabase = public_client()
        supabase.auth.sign_out()
    except Exception:
        pass

    session.clear()

    flash(
        "You have been logged out.",
        "success"
    )

    return redirect(url_for("login"))


# =========================================================
# ONE-TIME SUPER ADMIN SETUP
# =========================================================

@app.route(
    "/setup-super-admin",
    methods=["GET", "POST"]
)
def setup_super_admin():

    if request.method == "GET":
        return render_template(
            "setup_super_admin.html"
        )

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

    setup_secret = request.form.get(
        "setup_secret",
        ""
    )

    expected_secret = os.getenv(
        "ADMIN_SETUP_SECRET"
    )

    if not expected_secret:

        flash(
            "Super Admin setup is not configured.",
            "error"
        )

        return render_template(
            "setup_super_admin.html"
        )

    if not hmac.compare_digest(
        setup_secret,
        expected_secret
    ):

        flash(
            "Invalid setup secret.",
            "error"
        )

        return render_template(
            "setup_super_admin.html"
        )

    if not full_name or not email or not password:

        flash(
            "All fields are required.",
            "error"
        )

        return render_template(
            "setup_super_admin.html"
        )

    if password != confirm_password:

        flash(
            "Passwords do not match.",
            "error"
        )

        return render_template(
            "setup_super_admin.html"
        )

    try:
        db = admin_client()

        # -------------------------------------------------
        # Only allow initial Super Admin
        # -------------------------------------------------

        existing_admin = (
            db.table("profiles")
            .select("id")
            .eq("role", "super_admin")
            .limit(1)
            .execute()
        )

        if existing_admin.data:

            flash(
                "A Super Admin already exists.",
                "error"
            )

            return redirect(
                url_for("login")
            )

        # -------------------------------------------------
        # Create Supabase Auth account
        # -------------------------------------------------

        supabase = public_client()

        auth_response = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "full_name": full_name
                }
            }
        })

        user = auth_response.user

        if not user:

            flash(
                "Could not create "
                "Super Admin authentication account.",
                "error"
            )

            return render_template(
                "setup_super_admin.html"
            )

        user_id = str(user.id)

        # -------------------------------------------------
        # Create/update profile
        # -------------------------------------------------

        db.table("profiles").upsert({
            "id": user_id,
            "full_name": full_name,
            "role": "super_admin",
            "approved": True
        }).execute()

        flash(
            "Super Admin created successfully.",
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
            "error"
        )

        return render_template(
            "setup_super_admin.html"
        )


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard")
@login_required
def dashboard():

    profile = current_profile()

    if not profile:

        session.clear()

        flash(
            "Your profile could not be loaded. "
            "Please sign in again.",
            "error"
        )

        return redirect(
            url_for("login")
        )

    role = profile.get(
        "role",
        "employee"
    )

    stats = {}

    try:
        db = admin_client()

        if role in (
            "super_admin",
            "hr"
        ):

            employees_result = (
                db.table("employees")
                .select(
                    "id",
                    count="exact"
                )
                .execute()
            )

            stats["employees"] = (
                employees_result.count or 0
            )

            flagged_result = (
                db.table("dtr_entries")
                .select(
                    "id",
                    count="exact"
                )
                .eq(
                    "requires_review",
                    True
                )
                .execute()
            )

            stats["flagged_dtr"] = (
                flagged_result.count or 0
            )

            complaints_result = (
                db.table("complaints")
                .select(
                    "id",
                    count="exact"
                )
                .execute()
            )

            stats["complaints"] = (
                complaints_result.count or 0
            )

    except Exception as e:
        print(
            "DASHBOARD ERROR:",
            repr(e)
        )

    return render_template(
        "dashboard.html",
        profile=profile,
        stats=stats
    )


# =========================================================
# EMPLOYEES
# =========================================================

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

        try:
            employee_no = request.form.get(
                "employee_no",
                ""
            ).strip()

            full_name = request.form.get(
                "full_name",
                ""
            ).strip()

            employee_type = request.form.get(
                "employee_type",
                ""
            ).strip()

            department = request.form.get(
                "department",
                ""
            ).strip()

            monthly_salary_raw = (
                request.form.get(
                    "monthly_salary"
                )
            )

            hourly_rate_raw = (
                request.form.get(
                    "hourly_rate"
                )
            )

            standard_hours_raw = (
                request.form.get(
                    "standard_hours"
                )
            )

            workdays_raw = (
                request.form.get(
                    "workdays_per_month"
                )
            )

            monthly_salary = (
                float(monthly_salary_raw)
                if monthly_salary_raw
                else None
            )

            hourly_rate = (
                float(hourly_rate_raw)
                if hourly_rate_raw
                else None
            )

            standard_hours = (
                float(standard_hours_raw)
                if standard_hours_raw
                else 8
            )

            workdays_per_month = (
                int(workdays_raw)
                if workdays_raw
                else 22
            )

            db.table("employees").insert({
                "employee_no": employee_no,
                "full_name": full_name,
                "employee_type": employee_type,
                "department": department,
                "monthly_salary": monthly_salary,
                "hourly_rate": hourly_rate,
                "standard_hours": standard_hours,
                "workdays_per_month": workdays_per_month,
                "active": True,
            }).execute()

            flash(
                "Employee added successfully.",
                "success"
            )

            return redirect(
                url_for("employees")
            )

        except Exception as e:

            print(
                "EMPLOYEE CREATE ERROR:",
                repr(e)
            )

            flash(
                f"Could not add employee: {str(e)}",
                "error"
            )

    try:
        result = (
            db.table("employees")
            .select("*")
            .order(
                "full_name"
            )
            .execute()
        )

        employee_rows = (
            result.data or []
        )

    except Exception as e:

        print(
            "EMPLOYEE LIST ERROR:",
            repr(e)
        )

        employee_rows = []

    return render_template(
        "employees.html",
        employees=employee_rows
    )


# =========================================================
# DTR UPLOAD
# =========================================================

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

    if request.method == "POST":

        uploaded_file = request.files.get(
            "file"
        )

        if not uploaded_file:

            flash(
                "Please select a CSV file.",
                "error"
            )

            return render_template(
                "dtr_upload.html"
            )

        try:
            db = admin_client()

            batch_id = str(
                uuid.uuid4()
            )

            rows = parse_csv(
                uploaded_file
            )

            imported = 0
            skipped = 0

            for row in rows:

                employee_no = str(
                    row.get(
                        "employee_no",
                        ""
                    )
                ).strip()

                if not employee_no:
                    skipped += 1
                    continue

                employee_result = (
                    db.table("employees")
                    .select("*")
                    .eq(
                        "employee_no",
                        employee_no
                    )
                    .limit(1)
                    .execute()
                )

                if not employee_result.data:
                    skipped += 1
                    continue

                employee = (
                    employee_result.data[0]
                )

                evaluation = evaluate_day(
                    row
                )

                payload = {
                    "employee_id":
                        employee["id"],

                    "work_date":
                        row.get(
                            "work_date"
                        ),

                    "time_in":
                        row.get(
                            "time_in"
                        ),

                    "time_out":
                        row.get(
                            "time_out"
                        ),

                    "status":
                        evaluation.get(
                            "status"
                        ),

                    "payable_hours":
                        evaluation.get(
                            "payable_hours",
                            0
                        ),

                    "reason":
                        evaluation.get(
                            "reason"
                        ),

                    "requires_review":
                        evaluation.get(
                            "requires_review",
                            False
                        ),

                    "import_batch":
                        batch_id,
                }

                db.table(
                    "dtr_entries"
                ).upsert(
                    payload,
                    on_conflict=(
                        "employee_id,"
                        "work_date"
                    )
                ).execute()

                imported += 1

            flash(
                f"DTR import completed. "
                f"{imported} imported, "
                f"{skipped} skipped.",
                "success"
            )

            return redirect(
                url_for("dtr_upload")
            )

        except Exception as e:

            print(
                "DTR IMPORT ERROR:",
                repr(e)
            )

            flash(
                f"DTR upload failed: {str(e)}",
                "error"
            )

    return render_template(
        "dtr_upload.html"
    )


# =========================================================
# PAYROLL
# =========================================================

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

        start_date = request.form.get(
            "start_date"
        )

        end_date = request.form.get(
            "end_date"
        )

        cutoff_no = request.form.get(
            "cutoff_no"
        )

        if (
            not start_date
            or not end_date
            or not cutoff_no
        ):

            flash(
                "Start date, end date and "
                "cutoff are required.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )

        try:
            run_response = (
                db.table("payroll_runs")
                .insert({
                    "start_date":
                        start_date,

                    "end_date":
                        end_date,

                    "cutoff_no":
                        int(cutoff_no),

                    "status":
                        "draft",

                    "created_by":
                        session.get(
                            "user_id"
                        ),
                })
                .execute()
            )

            if not run_response.data:
                raise RuntimeError(
                    "Could not create payroll run."
                )

            payroll_run = (
                run_response.data[0]
            )

            run_id = payroll_run["id"]

            employee_result = (
                db.table("employees")
                .select("*")
                .eq(
                    "active",
                    True
                )
                .execute()
            )

            for employee in (
                employee_result.data or []
            ):

                dtr_result = (
                    db.table("dtr_entries")
                    .select("*")
                    .eq(
                        "employee_id",
                        employee["id"]
                    )
                    .gte(
                        "work_date",
                        start_date
                    )
                    .lte(
                        "work_date",
                        end_date
                    )
                    .execute()
                )

                deduction_result = (
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
                )

                calculation = calculate(
                    employee,
                    dtr_result.data or [],
                    deduction_result.data or [],
                    int(cutoff_no)
                )

                item_response = (
                    db.table("payroll_items")
                    .insert({
                        "payroll_run_id":
                            run_id,

                        "employee_id":
                            employee["id"],

                        "gross_pay":
                            calculation.get(
                                "gross_pay",
                                0
                            ),

                        "total_deductions":
                            calculation.get(
                                "total_deductions",
                                0
                            ),

                        "net_pay":
                            calculation.get(
                                "net_pay",
                                0
                            ),

                        "attendance_summary":
                            calculation.get(
                                "attendance_summary",
                                {}
                            ),
                    })
                    .execute()
                )

                if (
                    item_response.data
                    and calculation.get(
                        "deductions"
                    )
                ):

                    payroll_item_id = (
                        item_response
                        .data[0]["id"]
                    )

                    for deduction in (
                        calculation[
                            "deductions"
                        ]
                    ):

                        db.table(
                            "payroll_item_deductions"
                        ).insert({
                            "payroll_item_id":
                                payroll_item_id,

                            "deduction_id":
                                deduction.get(
                                    "id"
                                ),

                            "name":
                                deduction.get(
                                    "name",
                                    ""
                                ),

                            "amount":
                                deduction.get(
                                    "amount",
                                    0
                                ),
                        }).execute()

            flash(
                "Payroll draft generated.",
                "success"
            )

            return redirect(
                url_for(
                    "payroll_detail",
                    run_id=run_id
                )
            )

        except Exception as e:

            print(
                "PAYROLL GENERATION ERROR:",
                repr(e)
            )

            flash(
                f"Payroll generation failed: {str(e)}",
                "error"
            )

    try:
        runs_result = (
            db.table("payroll_runs")
            .select("*")
            .order(
                "created_at",
                desc=True
            )
            .execute()
        )

        runs = (
            runs_result.data or []
        )

    except Exception as e:

        print(
            "PAYROLL LIST ERROR:",
            repr(e)
        )

        runs = []

    return render_template(
        "payroll.html",
        runs=runs
    )


# =========================================================
# PAYROLL DETAIL
# =========================================================

@app.route("/payroll/<run_id>")
@login_required
@role_required(
    "super_admin",
    "hr"
)
def payroll_detail(run_id):

    try:
        db = admin_client()

        run_result = (
            db.table("payroll_runs")
            .select("*")
            .eq(
                "id",
                run_id
            )
            .limit(1)
            .execute()
        )

        if not run_result.data:

            flash(
                "Payroll run not found.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )

        payroll_run = (
            run_result.data[0]
        )

        items_result = (
            db.table("payroll_items")
            .select(
                "*,employees(*)"
            )
            .eq(
                "payroll_run_id",
                run_id
            )
            .execute()
        )

        return render_template(
            "payroll_detail.html",
            payroll_run=payroll_run,
            items=items_result.data or []
        )

    except Exception as e:

        print(
            "PAYROLL DETAIL ERROR:",
            repr(e)
        )

        flash(
            f"Could not load payroll: {str(e)}",
            "error"
        )

        return redirect(
            url_for("payroll")
        )


# =========================================================
# PAYROLL APPROVAL
# =========================================================

@app.route(
    "/payroll/<run_id>/approve",
    methods=["POST"]
)
@login_required
@role_required("super_admin")
def approve_payroll(run_id):

    try:
        db = admin_client()

        db.table(
            "payroll_runs"
        ).update({
            "status": "approved"
        }).eq(
            "id",
            run_id
        ).execute()

        flash(
            "Payroll approved successfully.",
            "success"
        )

    except Exception as e:

        print(
            "PAYROLL APPROVAL ERROR:",
            repr(e)
        )

        flash(
            f"Could not approve payroll: {str(e)}",
            "error"
        )

    return redirect(
        url_for(
            "payroll_detail",
            run_id=run_id
        )
    )


# =========================================================
# COMPLAINTS + RAG
# =========================================================

@app.route(
    "/complaints",
    methods=["GET", "POST"]
)
@login_required
def complaints():

    db = admin_client()

    if request.method == "POST":

        complaint_text = request.form.get(
            "complaint",
            ""
        ).strip()

        if not complaint_text:

            flash(
                "Please enter your complaint.",
                "error"
            )

            return redirect(
                url_for("complaints")
            )

        try:
            assessment = None

            # ---------------------------------------------
            # RAG assessment
            # ---------------------------------------------

            if assess_complaint:

                try:
                    assessment = (
                        assess_complaint(
                            complaint_text
                        )
                    )

                except Exception as rag_error:

                    print(
                        "RAG ERROR:",
                        repr(rag_error)
                    )

                    assessment = (
                        "RAG assessment is "
                        "temporarily unavailable."
                    )

            else:

                assessment = (
                    "RAG service is unavailable."
                )

            db.table(
                "complaints"
            ).insert({
                "user_id":
                    session.get(
                        "user_id"
                    ),

                "complaint":
                    complaint_text,

                "assessment":
                    assessment,

                "status":
                    "assessed"
                    if assessment
                    else "submitted",

                "model":
                    os.getenv(
                        "HF_MODEL",
                        "huggingface"
                    ),
            }).execute()

            flash(
                "Complaint submitted successfully.",
                "success"
            )

            return redirect(
                url_for("complaints")
            )

        except Exception as e:

            print(
                "COMPLAINT ERROR:",
                repr(e)
            )

            flash(
                f"Complaint submission failed: {str(e)}",
                "error"
            )

    try:

        profile = current_profile()

        if (
            profile
            and profile.get("role")
            in ("super_admin", "hr")
        ):

            result = (
                db.table("complaints")
                .select("*")
                .order(
                    "created_at",
                    desc=True
                )
                .execute()
            )

        else:

            result = (
                db.table("complaints")
                .select("*")
                .eq(
                    "user_id",
                    session.get(
                        "user_id"
                    )
                )
                .order(
                    "created_at",
                    desc=True
                )
                .execute()
            )

        complaint_rows = (
            result.data or []
        )

    except Exception as e:

        print(
            "COMPLAINT LIST ERROR:",
            repr(e)
        )

        complaint_rows = []

    return render_template(
        "complaints.html",
        complaints=complaint_rows
    )


# =========================================================
# 404
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return (
        render_template(
            "404.html"
        )
        if os.path.exists(
            os.path.join(
                app.template_folder,
                "404.html"
            )
        )
        else "Page not found",
        404
    )


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                5000
            )
        ),
        debug=True
    )
