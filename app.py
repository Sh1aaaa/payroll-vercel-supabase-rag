import os
import uuid
import hmac

from flask import (
    Flask,
    render_template,
    render_template_string,
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
# RAG
# =========================================================

try:
    from services.rag_service import assess_complaint
except Exception as e:
    print("RAG IMPORT ERROR:", repr(e))
    assess_complaint = None


# =========================================================
# FLASK APP
# IMPORTANT: Vercel needs this at TOP LEVEL
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "dev-only-change-this-secret"
)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "app": "BulSU Payroll Portal"
    })


# =========================================================
# ENVIRONMENT TEST
# Does NOT reveal secret values
# =========================================================

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


# =========================================================
# DATABASE TEST
# =========================================================

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
            "profiles": result.data or []
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# =========================================================
# DEBUG SUPABASE PROFILE
# TEMPORARY - REMOVE AFTER TROUBLESHOOTING
# =========================================================

@app.route("/debug-supabase")
def debug_supabase():
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
# VERSION TEST
# =========================================================

@app.route("/version-test")
def version_test():
    return "BulSU Payroll - NEW APP VERSION"


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
            # AUTHENTICATE USER
            # -------------------------------------------------

            supabase = public_client()

            auth_response = (
                supabase.auth.sign_in_with_password({
                    "email": email,
                    "password": password
                })
            )

            user = auth_response.user

            if not user:
                flash(
                    "Invalid email or password.",
                    "error"
                )
                return render_template("login.html")

            user_id = str(user.id)

            print("====================================")
            print("AUTH LOGIN SUCCESS")
            print("USER ID:", user_id)
            print("EMAIL:", user.email)
            print("====================================")

            # -------------------------------------------------
            # READ PROFILE
            # Use admin client to avoid RLS problems.
            # -------------------------------------------------

            db = admin_client()

            profile_response = (
                db.table("profiles")
                .select("id,full_name,role,approved")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )

            print(
                "PROFILE QUERY RESULT:",
                profile_response.data
            )

            profiles = profile_response.data or []

            if not profiles:
                flash(
                    "Your authentication account exists, "
                    "but no profile record was found.",
                    "error"
                )

                return render_template("login.html")

            profile = profiles[0]

            # -------------------------------------------------
            # APPROVAL CHECK
            # -------------------------------------------------

            if not profile.get("approved", False):

                flash(
                    "Your account is still waiting for "
                    "Super Admin approval.",
                    "warning"
                )

                return render_template("login.html")

            # -------------------------------------------------
            # SESSION
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

            print("LOGIN ROLE:", session["role"])

            flash(
                "Login successful.",
                "success"
            )

            return redirect(
                url_for("dashboard")
            )

        except Exception as e:

            print(
                "LOGIN ERROR:",
                repr(e)
            )

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

        if len(password) < 6:
            flash(
                "Password must contain at least 6 characters.",
                "error"
            )
            return render_template("register.html")

        if password != confirm_password:
            flash(
                "Passwords do not match.",
                "error"
            )
            return render_template("register.html")

        try:
            supabase = public_client()

            auth_response = (
                supabase.auth.sign_up({
                    "email": email,
                    "password": password,
                    "options": {
                        "data": {
                            "full_name": full_name
                        }
                    }
                })
            )

            user = auth_response.user

            if not user:
                flash(
                    "Could not create your account.",
                    "error"
                )
                return render_template("register.html")

            user_id = str(user.id)

            print(
                "REGISTERED AUTH USER:",
                user_id
            )

            # -------------------------------------------------
            # ENSURE PROFILE EXISTS
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
                    "PROFILE CREATION ERROR:",
                    repr(profile_error)
                )

            flash(
                "Registration successful. "
                "Please confirm your email if required, "
                "then wait for Super Admin approval.",
                "success"
            )

            return redirect(
                url_for("login")
            )

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
# FORGOT PASSWORD
# This endpoint prevents login.html from crashing when
# it contains url_for('forgot_password').
# =========================================================

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
                "error"
            )

            return redirect(
                url_for("forgot_password")
            )

        try:
            supabase = public_client()

            # Recovery email
            supabase.auth.reset_password_for_email(
                email,
                options={
                    "redirect_to": request.url_root.rstrip("/")
                    + "/reset-password"
                }
            )

            flash(
                "If the email exists, password reset "
                "instructions have been sent.",
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
                f"Could not send reset email: {str(e)}",
                "error"
            )

    # If forgot_password.html exists, use it.
    try:
        return render_template(
            "forgot_password.html"
        )

    except Exception:
        # Safe fallback so missing template will NOT cause 500.
        return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Forgot Password</title>
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: #f4f6fa;
                    margin: 0;
                    min-height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                }

                .card {
                    background: white;
                    width: 360px;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow: 0 8px 30px
                        rgba(0,0,0,.08);
                }

                h2 {
                    text-align: center;
                    color: #101a44;
                }

                input {
                    width: 100%;
                    box-sizing: border-box;
                    padding: 12px;
                    margin-top: 8px;
                    margin-bottom: 18px;
                }

                button {
                    width: 100%;
                    padding: 12px;
                    background: #414fb3;
                    color: white;
                    border: 0;
                    border-radius: 6px;
                    cursor: pointer;
                }

                a {
                    display: block;
                    text-align: center;
                    margin-top: 18px;
                    text-decoration: none;
                }
            </style>
        </head>

        <body>

            <div class="card">

                <h2>BulSU Payroll Portal</h2>

                {% with messages =
                    get_flashed_messages(with_categories=true) %}

                    {% for category, message in messages %}
                        <p>{{ message }}</p>
                    {% endfor %}

                {% endwith %}

                <form method="POST">

                    <label>Email</label>

                    <input
                        type="email"
                        name="email"
                        required
                    >

                    <button type="submit">
                        Send Reset Email
                    </button>

                </form>

                <a href="{{ url_for('login') }}">
                    Back to Login
                </a>

            </div>

        </body>
        </html>
        """)


# =========================================================
# RESET PASSWORD
# Placeholder endpoint so recovery redirect will not 404.
# =========================================================

@app.route(
    "/reset-password",
    methods=["GET", "POST"]
)
def reset_password():

    return render_template_string("""
    <!DOCTYPE html>

    <html>

    <head>

        <title>Reset Password</title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

    </head>

    <body>

        <h2>BulSU Payroll Portal</h2>

        <p>
            Password recovery link received.
        </p>

        <p>
            Return to the login page after updating your
            password through the Supabase recovery process.
        </p>

        <a href="{{ url_for('login') }}">
            Return to Login
        </a>

    </body>

    </html>
    """)


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# INITIAL SUPER ADMIN
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

    submitted_secret = request.form.get(
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
        submitted_secret,
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
                "warning"
            )

            return redirect(
                url_for("login")
            )

        supabase = public_client()

        response = (
            supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "full_name": full_name
                    }
                }
            })
        )

        user = response.user

        if not user:

            flash(
                "Could not create Super Admin account.",
                "error"
            )

            return render_template(
                "setup_super_admin.html"
            )

        user_id = str(user.id)

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
            "SUPER ADMIN ERROR:",
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

    try:
        profile = current_profile()

        if not profile:

            session.clear()

            flash(
                "Your profile could not be loaded.",
                "error"
            )

            return redirect(
                url_for("login")
            )

        role = profile.get(
            "role",
            "employee"
        )

        stats = {
            "employees": 0,
            "flagged_dtr": 0,
            "complaints": 0
        }

        db = admin_client()

        if role in (
            "super_admin",
            "hr"
        ):

            try:
                response = (
                    db.table("employees")
                    .select(
                        "id",
                        count="exact"
                    )
                    .execute()
                )

                stats["employees"] = (
                    response.count or 0
                )

            except Exception as e:
                print(
                    "EMPLOYEE COUNT ERROR:",
                    repr(e)
                )

            try:
                response = (
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
                    response.count or 0
                )

            except Exception as e:
                print(
                    "DTR COUNT ERROR:",
                    repr(e)
                )

            try:
                response = (
                    db.table("complaints")
                    .select(
                        "id",
                        count="exact"
                    )
                    .execute()
                )

                stats["complaints"] = (
                    response.count or 0
                )

            except Exception as e:
                print(
                    "COMPLAINT COUNT ERROR:",
                    repr(e)
                )

        return render_template(
            "dashboard.html",
            profile=profile,
            stats=stats
        )

    except Exception as e:

        print(
            "DASHBOARD ERROR:",
            repr(e)
        )

        flash(
            f"Dashboard error: {str(e)}",
            "error"
        )

        return redirect(
            url_for("login")
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
            payload = {
                "employee_no":
                    request.form.get(
                        "employee_no",
                        ""
                    ).strip(),

                "full_name":
                    request.form.get(
                        "full_name",
                        ""
                    ).strip(),

                "employee_type":
                    request.form.get(
                        "employee_type",
                        ""
                    ).strip(),

                "department":
                    request.form.get(
                        "department",
                        ""
                    ).strip(),

                "active":
                    True
            }

            monthly_salary = request.form.get(
                "monthly_salary"
            )

            hourly_rate = request.form.get(
                "hourly_rate"
            )

            standard_hours = request.form.get(
                "standard_hours"
            )

            workdays = request.form.get(
                "workdays_per_month"
            )

            if monthly_salary:
                payload["monthly_salary"] = float(
                    monthly_salary
                )

            if hourly_rate:
                payload["hourly_rate"] = float(
                    hourly_rate
                )

            payload["standard_hours"] = (
                float(standard_hours)
                if standard_hours
                else 8
            )

            payload["workdays_per_month"] = (
                int(workdays)
                if workdays
                else 22
            )

            db.table("employees").insert(
                payload
            ).execute()

            flash(
                "Employee added successfully.",
                "success"
            )

            return redirect(
                url_for("employees")
            )

        except Exception as e:

            print(
                "ADD EMPLOYEE ERROR:",
                repr(e)
            )

            flash(
                f"Could not add employee: {str(e)}",
                "error"
            )

    try:
        response = (
            db.table("employees")
            .select("*")
            .order("full_name")
            .execute()
        )

        rows = response.data or []

    except Exception as e:

        print(
            "EMPLOYEE LIST ERROR:",
            repr(e)
        )

        rows = []

    return render_template(
        "employees.html",
        employees=rows
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

            rows = parse_csv(
                uploaded_file
            )

            batch_id = str(
                uuid.uuid4()
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

                result = (
                    db.table("employees")
                    .select("*")
                    .eq(
                        "employee_no",
                        employee_no
                    )
                    .limit(1)
                    .execute()
                )

                if not result.data:
                    skipped += 1
                    continue

                employee = result.data[0]

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
                        batch_id
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
                f"DTR import complete. "
                f"{imported} imported, "
                f"{skipped} skipped.",
                "success"
            )

        except Exception as e:

            print(
                "DTR ERROR:",
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
# PAYROLL LIST / GENERATE
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

        if not start_date or not end_date or not cutoff_no:

            flash(
                "Start date, end date and cutoff are required.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )

        try:
            response = (
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
                        )
                })
                .execute()
            )

            if not response.data:

                raise RuntimeError(
                    "Could not create payroll run."
                )

            run_id = response.data[0]["id"]

            employees_result = (
                db.table("employees")
                .select("*")
                .eq("active", True)
                .execute()
            )

            for employee in (
                employees_result.data or []
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

                deductions_result = (
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
                    deductions_result.data or [],
                    int(cutoff_no)
                )

                item_result = (
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
                            )
                    })
                    .execute()
                )

                if item_result.data:

                    payroll_item_id = (
                        item_result.data[0]["id"]
                    )

                    for deduction in calculation.get(
                        "deductions",
                        []
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
                                )
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
        result = (
            db.table("payroll_runs")
            .select("*")
            .order(
                "created_at",
                desc=True
            )
            .execute()
        )

        runs = result.data or []

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
            .eq("id", run_id)
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
            payroll_run=run_result.data[0],
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
# APPROVE PAYROLL
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
            "PAYROLL APPROVE ERROR:",
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

            if assess_complaint:

                try:
                    assessment = assess_complaint(
                        complaint_text
                    )

                except Exception as rag_error:

                    print(
                        "RAG ASSESSMENT ERROR:",
                        repr(rag_error)
                    )

                    assessment = (
                        "RAG assessment is temporarily "
                        "unavailable."
                    )

            else:

                assessment = (
                    "RAG service is currently unavailable."
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
                    "assessed",

                "model":
                    os.getenv(
                        "HF_MODEL",
                        "huggingface"
                    )
            }).execute()

            flash(
                "Complaint submitted.",
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
# LOCAL DEVELOPMENT
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
