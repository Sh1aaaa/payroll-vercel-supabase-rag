import os
import uuid
import hmac

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


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()


# =========================================================
# OPTIONAL RAG SERVICE
# =========================================================

try:
    from services.rag_service import assess_complaint
except Exception as e:
    print("RAG IMPORT ERROR:", repr(e))
    assess_complaint = None


# =========================================================
# FLASK APP
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
# HOME
# =========================================================

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# =========================================================
# STAFF REGISTRATION
# =========================================================
#
# This replaces the old one-time Super Admin setup.
#
# The login page should open /staff-register after the
# hidden 3-click action.
#
# Roles:
#
#   hr
#       Payroll Clerk
#
#   super_admin
#       Super Admin
#
#
# FIRST SUPER ADMIN
# -----------------
# If no approved Super Admin exists:
#
#   - Only Super Admin can be registered
#   - ADMIN_SETUP_SECRET is required
#   - Account is automatically approved
#
#
# ADDITIONAL STAFF
# ----------------
# Once an approved Super Admin exists:
#
#   - Payroll Clerk can register
#   - Additional Super Admin can register
#   - Both remain pending
#   - Existing Super Admin must approve them
#
# =========================================================

@app.route("/staff-register", methods=["GET", "POST"])
def staff_register():

    # -----------------------------------------------------
    # CHECK WHETHER AN APPROVED SUPER ADMIN ALREADY EXISTS
    # -----------------------------------------------------

    try:

        db = admin_client()

        admin_check = (
            db.table("profiles")
            .select("id")
            .eq("role", "super_admin")
            .eq("approved", True)
            .limit(1)
            .execute()
        )

        has_super_admin = bool(admin_check.data)

    except Exception as e:

        print(
            "STAFF REGISTRATION CHECK ERROR:",
            repr(e)
        )

        flash(
            f"Could not check Super Admin status: {str(e)}",
            "error"
        )

        return redirect(url_for("login"))

    # -----------------------------------------------------
    # POST
    # -----------------------------------------------------

    if request.method == "POST":

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        requested_role = request.form.get(
            "role",
            ""
        ).strip()

        setup_secret = request.form.get(
            "setup_secret",
            ""
        )

        # -------------------------------------------------
        # VALIDATE ROLE
        # -------------------------------------------------

        if requested_role not in (
            "hr",
            "super_admin"
        ):

            flash(
                "Please select Payroll Clerk or Super Admin.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )

        # -------------------------------------------------
        # REQUIRED FIELDS
        # -------------------------------------------------

        if (
            not full_name
            or not email
            or not password
        ):

            flash(
                "Full name, email and password are required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )

        # -------------------------------------------------
        # PASSWORD LENGTH
        # -------------------------------------------------

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )

        # -------------------------------------------------
        # PASSWORD CONFIRMATION
        # -------------------------------------------------

        if password != confirm_password:

            flash(
                "Passwords do not match.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )

        # -------------------------------------------------
        # FIRST SUPER ADMIN
        # -------------------------------------------------

        if not has_super_admin:

            # The first staff account MUST be Super Admin.

            if requested_role != "super_admin":

                flash(
                    "The first staff account must be a Super Admin.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=False
                )

            expected_secret = os.getenv(
                "ADMIN_SETUP_SECRET"
            )

            if not expected_secret:

                flash(
                    "ADMIN_SETUP_SECRET is not configured in Vercel.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=False
                )

            # Constant-time comparison.

            if not hmac.compare_digest(
                setup_secret,
                expected_secret
            ):

                flash(
                    "Invalid initial Super Admin setup secret.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=False
                )

        # -------------------------------------------------
        # CREATE AUTH ACCOUNT
        # -------------------------------------------------

        user_id = None

        try:

            auth_result = db.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": full_name,
                    "requested_role": requested_role
                }
            })

            user = auth_result.user

            if not user:

                raise RuntimeError(
                    "Supabase did not return the created user."
                )

            user_id = str(user.id)

            # -------------------------------------------------
            # APPROVAL STATUS
            # -------------------------------------------------
            #
            # First Super Admin:
            #     approved = True
            #
            # Everyone after that:
            #     approved = False
            #
            # This includes:
            #     Payroll Clerk
            #     Additional Super Admin
            #
            # -------------------------------------------------

            approved = not has_super_admin

            profile_result = (
                db.table("profiles")
                .upsert({
                    "id": user_id,
                    "full_name": full_name,
                    "role": requested_role,
                    "approved": approved
                })
                .execute()
            )

            if not profile_result.data:

                raise RuntimeError(
                    "The authentication account was created, "
                    "but the profile could not be created."
                )

            # -------------------------------------------------
            # SUCCESS MESSAGE
            # -------------------------------------------------

            if requested_role == "super_admin":

                if approved:

                    flash(
                        "Initial Super Admin created successfully. "
                        "You can now log in.",
                        "success"
                    )

                else:

                    flash(
                        "Super Admin registration submitted. "
                        "An existing Super Admin must approve this account.",
                        "success"
                    )

            elif requested_role == "hr":

                flash(
                    "Payroll Clerk registration submitted. "
                    "An existing Super Admin must approve this account.",
                    "success"
                )

            return redirect(
                url_for("login")
            )

        except Exception as e:

            print(
                "STAFF REGISTRATION ERROR:",
                repr(e)
            )

            # -------------------------------------------------
            # CLEAN UP ORPHAN AUTH USER
            # -------------------------------------------------

            if user_id:

                try:

                    db.auth.admin.delete_user(
                        user_id
                    )

                except Exception as cleanup_error:

                    print(
                        "AUTH CLEANUP ERROR:",
                        repr(cleanup_error)
                    )

            error_text = str(e)

            # -------------------------------------------------
            # DUPLICATE EMAIL
            # -------------------------------------------------

            if (
                "already been registered"
                in error_text.lower()
                or
                "already exists"
                in error_text.lower()
                or
                "duplicate"
                in error_text.lower()
            ):

                flash(
                    "That email already has an account. "
                    "Please use another email address.",
                    "error"
                )

            else:

                flash(
                    f"Staff registration failed: {error_text}",
                    "error"
                )

    # -----------------------------------------------------
    # GET
    # -----------------------------------------------------

    return render_template(
        "staff_register.html",
        has_super_admin=has_super_admin
    )


# =========================================================
# OLD URL COMPATIBILITY
# =========================================================
#
# If an old link still points to:
#
#     /setup-super-admin
#
# redirect it to the new staff registration page.
#
# =========================================================

@app.route("/setup-super-admin")
def setup_super_admin():

    return redirect(
        url_for("staff_register")
    )


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if not email or not password:

            flash(
                "Email and password are required.",
                "error"
            )

            return render_template(
                "login.html"
            )

        try:

            auth = public_client()

            result = auth.auth.sign_in_with_password({
                "email": email,
                "password": password
            })

            user = result.user

            if not user:

                flash(
                    "Invalid email or password.",
                    "error"
                )

                return render_template(
                    "login.html"
                )

            user_id = str(user.id)

            # -------------------------------------------------
            # LOAD PROFILE
            # -------------------------------------------------

            profile_result = (
                admin_client()
                .table("profiles")
                .select(
                    "id,full_name,role,approved"
                )
                .eq(
                    "id",
                    user_id
                )
                .limit(1)
                .execute()
            )

            if not profile_result.data:

                flash(
                    "Your authentication account exists, "
                    "but no profile record was found.",
                    "error"
                )

                return render_template(
                    "login.html"
                )

            profile = profile_result.data[0]

            # -------------------------------------------------
            # APPROVAL CHECK
            # -------------------------------------------------

            if not profile.get(
                "approved",
                False
            ):

                flash(
                    "Your registration is waiting for "
                    "Super Admin approval.",
                    "warning"
                )

                return render_template(
                    "login.html"
                )

            # -------------------------------------------------
            # CREATE SESSION
            # -------------------------------------------------

            session.clear()

            session["user_id"] = user_id

            session["email"] = user.email

            session["full_name"] = (
                profile.get("full_name")
                or ""
            )

            session["role"] = (
                profile.get("role")
                or "employee"
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

    return render_template(
        "login.html"
    )


# =========================================================
# EMPLOYEE REGISTRATION
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
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        # -------------------------------------------------
        # REQUIRED FIELDS
        # -------------------------------------------------

        if (
            not full_name
            or not email
            or not password
        ):

            flash(
                "Full name, email and password are required.",
                "error"
            )

            return render_template(
                "register.html"
            )

        # -------------------------------------------------
        # PASSWORD
        # -------------------------------------------------

        if len(password) < 6:

            flash(
                "Password must contain at least 6 characters.",
                "error"
            )

            return render_template(
                "register.html"
            )

        if password != confirm_password:

            flash(
                "Passwords do not match.",
                "error"
            )

            return render_template(
                "register.html"
            )

        try:

            auth_result = (
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

            user = auth_result.user

            if not user:

                flash(
                    "Could not create the account.",
                    "error"
                )

                return render_template(
                    "register.html"
                )

            # -------------------------------------------------
            # EMPLOYEE PROFILE
            # -------------------------------------------------

            admin_client().table(
                "profiles"
            ).upsert({
                "id": str(user.id),
                "full_name": full_name,
                "role": "employee",
                "approved": False
            }).execute()

            flash(
                "Registration successful. "
                "Wait for Super Admin approval before logging in.",
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

    return render_template(
        "register.html"
    )


# =========================================================
# FORGOT PASSWORD
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
                "Enter your email address.",
                "error"
            )

            return render_template(
                "forgot_password.html"
            )

        try:

            public_client().auth.reset_password_for_email(
                email
            )

            flash(
                "If the account exists, "
                "password recovery instructions were sent.",
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

    return render_template(
        "forgot_password.html"
    )


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

    db = admin_client()

    # -----------------------------------------------------
    # DEFAULT STATISTICS
    # -----------------------------------------------------

    stats = {
        "employees": 0,
        "pending_accounts": 0,
        "flagged_dtr": 0,
        "pending_payroll": 0,
    }

    pending_users = []

    payroll_runs = []

    # =====================================================
    # SUPER ADMIN DASHBOARD
    # =====================================================

    if role == "super_admin":

        # -------------------------------------------------
        # EMPLOYEE COUNT
        # -------------------------------------------------

        try:

            r = (
                db.table("employees")
                .select(
                    "id",
                    count="exact"
                )
                .execute()
            )

            stats["employees"] = (
                r.count or 0
            )

        except Exception as e:

            print(
                "EMPLOYEE COUNT ERROR:",
                repr(e)
            )

        # -------------------------------------------------
        # PENDING ACCOUNTS
        # -------------------------------------------------

        try:

            r = (
                db.table("profiles")
                .select(
                    "id,full_name,role,approved,created_at"
                )
                .eq(
                    "approved",
                    False
                )
                .order(
                    "created_at"
                )
                .execute()
            )

            pending_users = (
                r.data or []
            )

            stats["pending_accounts"] = len(
                pending_users
            )

        except Exception as e:

            print(
                "PENDING USER ERROR:",
                repr(e)
            )

        # -------------------------------------------------
        # FLAGGED DTR
        # -------------------------------------------------

        try:

            r = (
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
                r.count or 0
            )

        except Exception as e:

            print(
                "DTR COUNT ERROR:",
                repr(e)
            )

        # -------------------------------------------------
        # PENDING PAYROLL
        # -------------------------------------------------

        try:

            r = (
                db.table("payroll_runs")
                .select("*")
                .eq(
                    "status",
                    "draft"
                )
                .order(
                    "created_at",
                    desc=True
                )
                .execute()
            )

            payroll_runs = (
                r.data or []
            )

            stats["pending_payroll"] = len(
                payroll_runs
            )

        except Exception as e:

            print(
                "PENDING PAYROLL ERROR:",
                repr(e)
            )

    # =====================================================
    # PAYROLL CLERK DASHBOARD
    # =====================================================

    elif role == "hr":

        # -------------------------------------------------
        # ACTIVE EMPLOYEES
        # -------------------------------------------------

        try:

            r = (
                db.table("employees")
                .select(
                    "id",
                    count="exact"
                )
                .eq(
                    "active",
                    True
                )
                .execute()
            )

            stats["employees"] = (
                r.count or 0
            )

        except Exception as e:

            print(
                "HR EMPLOYEE COUNT ERROR:",
                repr(e)
            )

        # -------------------------------------------------
        # FLAGGED DTR
        # -------------------------------------------------

        try:

            r = (
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
                r.count or 0
            )

        except Exception as e:

            print(
                "HR DTR COUNT ERROR:",
                repr(e)
            )

        # -------------------------------------------------
        # PAYROLL RUNS
        # -------------------------------------------------

        try:

            payroll_runs = (
                db.table("payroll_runs")
                .select("*")
                .order(
                    "created_at",
                    desc=True
                )
                .limit(10)
                .execute()
            ).data or []

        except Exception as e:

            print(
                "HR PAYROLL ERROR:",
                repr(e)
            )

    # =====================================================
    # EMPLOYEE DASHBOARD
    # =====================================================

    elif role == "employee":

        # Employees currently don't receive admin
        # statistics.

        pass

    # =====================================================
    # RENDER DASHBOARD
    # =====================================================

    return render_template(
        "dashboard.html",
        profile=profile,
        stats=stats,
        pending_users=pending_users,
        payroll_runs=payroll_runs,
    )


# =========================================================
# APPROVE USER
# =========================================================
#
# IMPORTANT:
#
# This function DOES NOT change the user's role.
#
# Employee stays employee.
# HR stays HR.
# Super Admin stays Super Admin.
#
# =========================================================

@app.route(
    "/admin/users/<user_id>/approve",
    methods=["POST"]
)
@login_required
@role_required("super_admin")
def approve_user(user_id):

    try:

        db = admin_client()

        found = (
            db.table("profiles")
            .select(
                "id,full_name,role,approved"
            )
            .eq(
                "id",
                user_id
            )
            .limit(1)
            .execute()
        )

        if not found.data:

            flash(
                "Registration not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        profile = found.data[0]

        # -------------------------------------------------
        # ONLY CHANGE APPROVAL
        # -------------------------------------------------

        db.table("profiles").update({
            "approved": True
        }).eq(
            "id",
            user_id
        ).execute()

        role = profile.get(
            "role",
            "employee"
        )

        full_name = profile.get(
            "full_name",
            "User"
        )

        # -------------------------------------------------
        # MESSAGE
        # -------------------------------------------------

        if role == "super_admin":

            flash(
                f"{full_name} has been approved as Super Admin.",
                "success"
            )

        elif role == "hr":

            flash(
                f"{full_name} has been approved as Payroll Clerk.",
                "success"
            )

        else:

            flash(
                f"{full_name} has been approved as Employee.",
                "success"
            )

    except Exception as e:

        print(
            "APPROVE USER ERROR:",
            repr(e)
        )

        flash(
            f"Could not approve registration: {str(e)}",
            "error"
        )

    return redirect(
        url_for("dashboard")
    )


# =========================================================
# REJECT USER
# =========================================================

@app.route(
    "/admin/users/<user_id>/reject",
    methods=["POST"]
)
@login_required
@role_required("super_admin")
def reject_user(user_id):

    try:

        db = admin_client()

        found = (
            db.table("profiles")
            .select(
                "id,full_name,role"
            )
            .eq(
                "id",
                user_id
            )
            .limit(1)
            .execute()
        )

        if not found.data:

            flash(
                "Registration not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        profile = found.data[0]

        # -------------------------------------------------
        # DELETE AUTH ACCOUNT
        # -------------------------------------------------

        try:

            db.auth.admin.delete_user(
                user_id
            )

        except Exception as auth_error:

            print(
                "AUTH DELETE ERROR:",
                repr(auth_error)
            )

            # If Auth deletion fails, remove profile.
            db.table("profiles").delete().eq(
                "id",
                user_id
            ).execute()

        flash(
            f"Registration for "
            f"{profile.get('full_name', 'User')} "
            "rejected.",
            "success"
        )

    except Exception as e:

        print(
            "REJECT USER ERROR:",
            repr(e)
        )

        flash(
            f"Could not reject registration: {str(e)}",
            "error"
        )

    return redirect(
        url_for("dashboard")
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

    # -----------------------------------------------------
    # ADD EMPLOYEE
    # -----------------------------------------------------

    if request.method == "POST":

        try:

            monthly_salary = request.form.get(
                "monthly_salary",
                ""
            ).strip()

            hourly_rate = request.form.get(
                "hourly_rate",
                ""
            ).strip()

            db.table("employees").insert({

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

                "monthly_salary":
                    (
                        float(monthly_salary)
                        if monthly_salary
                        else None
                    ),

                "hourly_rate":
                    (
                        float(hourly_rate)
                        if hourly_rate
                        else None
                    ),

                "standard_hours":
                    float(
                        request.form.get(
                            "standard_hours"
                        )
                        or 8
                    ),

                "workdays_per_month":
                    int(
                        request.form.get(
                            "workdays_per_month"
                        )
                        or 22
                    ),

                "active": True,

            }).execute()

            flash(
                "Employee record added.",
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

    # -----------------------------------------------------
    # LOAD EMPLOYEES
    # -----------------------------------------------------

    try:

        rows = (
            db.table("employees")
            .select("*")
            .order("full_name")
            .execute()
            .data or []
        )

    except Exception as e:

        print(
            "LOAD EMPLOYEES ERROR:",
            repr(e)
        )

        rows = []

    return render_template(
        "employees.html",
        employees=rows
    )


# =========================================================
# DTR UPLOAD / MASTER DTR COMMIT
# =========================================================
#
# Only Super Admin can commit the Master DTR CSV.
#
# =========================================================

@app.route(
    "/dtr/upload",
    methods=["GET", "POST"]
)
@login_required
@role_required("super_admin")
def dtr_upload():

    if request.method == "POST":

        uploaded_file = request.files.get(
            "file"
        )

        if not uploaded_file:

            flash(
                "Select a CSV file first.",
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

            # -------------------------------------------------
            # PROCESS CSV
            # -------------------------------------------------

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

                # -------------------------------------------------
                # FIND EMPLOYEE
                # -------------------------------------------------

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

                # -------------------------------------------------
                # EVALUATE DTR
                # -------------------------------------------------

                evaluation = evaluate_day(
                    row
                )

                # -------------------------------------------------
                # SAVE DTR
                # -------------------------------------------------

                db.table(
                    "dtr_entries"
                ).upsert({

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

                }, on_conflict="employee_id,work_date").execute()

                imported += 1

            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

            flash(
                f"Master DTR committed. "
                f"{imported} imported; "
                f"{skipped} skipped.",
                "success"
            )

            return redirect(
                url_for("dashboard")
            )

        except Exception as e:

            print(
                "DTR UPLOAD ERROR:",
                repr(e)
            )

            flash(
                f"Could not commit master DTR: {str(e)}",
                "error"
            )

    return render_template(
        "dtr_upload.html"
    )


# =========================================================
# PAYROLL
# =========================================================
#
# GET:
#   Super Admin + Payroll Clerk can view payroll.
#
# POST:
#   Only Payroll Clerk can generate payroll.
#
# Status:
#   draft = waiting for Super Admin approval
#
# =========================================================

@app.route(
    "/payroll",
    methods=["GET", "POST"]
)
@login_required
def payroll():

    profile = current_profile()

    if not profile:

        session.clear()

        return redirect(
            url_for("login")
        )

    role = profile.get(
        "role",
        "employee"
    )

    # -----------------------------------------------------
    # PERMISSION
    # -----------------------------------------------------

    if role not in (
        "super_admin",
        "hr"
    ):

        flash(
            "You do not have permission to access payroll processing.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )

    db = admin_client()

    # =====================================================
    # GENERATE PAYROLL
    # =====================================================

    if request.method == "POST":

        # -------------------------------------------------
        # ONLY PAYROLL CLERK
        # -------------------------------------------------

        if role != "hr":

            flash(
                "Only the Payroll Clerk can generate payroll.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )

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
                "Start date, end date and cutoff are required.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )

        try:

            cutoff_number = int(
                cutoff_no
            )

            # -------------------------------------------------
            # CREATE PAYROLL RUN
            # -------------------------------------------------

            run_result = (
                db.table("payroll_runs")
                .insert({

                    "start_date":
                        start_date,

                    "end_date":
                        end_date,

                    "cutoff_no":
                        cutoff_number,

                    "status":
                        "draft",

                    "created_by":
                        session.get(
                            "user_id"
                        ),

                })
                .execute()
            )

            if not run_result.data:

                raise RuntimeError(
                    "Could not create payroll run."
                )

            run_id = (
                run_result.data[0]["id"]
            )

            # -------------------------------------------------
            # LOAD ACTIVE EMPLOYEES
            # -------------------------------------------------

            employee_rows = (
                db.table("employees")
                .select("*")
                .eq(
                    "active",
                    True
                )
                .execute()
                .data or []
            )

            # -------------------------------------------------
            # PROCESS EACH EMPLOYEE
            # -------------------------------------------------

            for employee in employee_rows:

                # ---------------------------------------------
                # DTR
                # ---------------------------------------------

                dtr_rows = (
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
                ).data or []

                # ---------------------------------------------
                # DEDUCTIONS
                # ---------------------------------------------

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
                ).data or []

                # ---------------------------------------------
                # CALCULATE
                # ---------------------------------------------

                calculation = calculate(
                    employee,
                    dtr_rows,
                    deductions,
                    cutoff_number
                )

                # ---------------------------------------------
                # PAYROLL ITEM
                # ---------------------------------------------

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
                            ),

                    })
                    .execute()
                )

                # ---------------------------------------------
                # PAYROLL DEDUCTIONS
                # ---------------------------------------------

                if item_result.data:

                    item_id = (
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
                                item_id,

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

            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

            flash(
                "Payroll generated and sent to the "
                "Super Admin for approval.",
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

    # =====================================================
    # LOAD PAYROLL RUNS
    # =====================================================

    try:

        runs = (
            db.table("payroll_runs")
            .select("*")
            .order(
                "created_at",
                desc=True
            )
            .execute()
            .data or []
        )

    except Exception as e:

        print(
            "PAYROLL LIST ERROR:",
            repr(e)
        )

        runs = []

    return render_template(
        "payroll.html",
        runs=runs,
        profile=profile
    )


# =========================================================
# PAYROLL DETAIL
# =========================================================

@app.route(
    "/payroll/<run_id>"
)
@login_required
@role_required(
    "super_admin",
    "hr"
)
def payroll_detail(run_id):

    try:

        db = admin_client()

        # -------------------------------------------------
        # PAYROLL RUN
        # -------------------------------------------------

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

        # -------------------------------------------------
        # PAYROLL ITEMS
        # -------------------------------------------------

        items = (
            db.table("payroll_items")
            .select(
                "*,employees(*)"
            )
            .eq(
                "payroll_run_id",
                run_id
            )
            .execute()
        ).data or []

        return render_template(
            "payroll_detail.html",
            payroll_run=run_result.data[0],
            items=items,
            profile=current_profile()
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
#
# ONLY SUPER ADMIN
#
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

        found = (
            db.table("payroll_runs")
            .select(
                "id,status"
            )
            .eq(
                "id",
                run_id
            )
            .limit(1)
            .execute()
        )

        if not found.data:

            flash(
                "Payroll run not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )

        current_status = (
            found.data[0].get(
                "status"
            )
        )

        if current_status == "approved":

            flash(
                "This payroll is already approved.",
                "warning"
            )

        else:

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
            "APPROVE PAYROLL ERROR:",
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
# COMPLAINTS / RAG
# =========================================================

@app.route(
    "/complaints",
    methods=["GET", "POST"]
)
@login_required
def complaints():

    db = admin_client()

    # =====================================================
    # SUBMIT COMPLAINT
    # =====================================================

    if request.method == "POST":

        complaint_text = request.form.get(
            "complaint",
            ""
        ).strip()

        if not complaint_text:

            flash(
                "Enter your complaint.",
                "error"
            )

            return redirect(
                url_for("complaints")
            )

        try:

            # -------------------------------------------------
            # RAG ASSESSMENT
            # -------------------------------------------------

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
                        "RAG assessment is temporarily unavailable."
                    )

            else:

                assessment = (
                    "RAG service is unavailable."
                )

            # -------------------------------------------------
            # SAVE COMPLAINT
            # -------------------------------------------------

            db.table("complaints").insert({

                "employee_id":
                    session.get(
                        "employee_id"
                    ),

                "complaint":
                    complaint_text,

                "assessment":
                    assessment,

                "status":
                    "assessed",

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

    # =====================================================
    # LOAD COMPLAINTS
    # =====================================================

    profile = current_profile()

    try:

        query = (
            db.table("complaints")
            .select("*")
        )

        # -------------------------------------------------
        # ADMIN / HR
        # -------------------------------------------------

        if (
            profile
            and profile.get("role")
            in (
                "super_admin",
                "hr"
            )
        ):

            pass

        # -------------------------------------------------
        # EMPLOYEE
        # -------------------------------------------------

        else:

            query = query.eq(
                "user_id",
                session.get(
                    "user_id"
                )
            )

        rows = (
            query
            .order(
                "created_at",
                desc=True
            )
            .execute()
            .data or []
        )

    except Exception as e:

        print(
            "COMPLAINT LOAD ERROR:",
            repr(e)
        )

        rows = []

    return render_template(
        "complaints.html",
        complaints=rows
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
