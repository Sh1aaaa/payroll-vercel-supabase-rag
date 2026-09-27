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
    jsonify
)

from dotenv import load_dotenv

from services.supabase_service import public_client, admin_client
from services.auth_service import (
    login_required,
    role_required,
    current_profile
)
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
#
# This is the hidden 3-click registration page.
#
# First account:
#     Super Admin
#     Requires ADMIN_SETUP_SECRET
#     Automatically approved
#
# After first Super Admin:
#     Payroll Clerk (hr)
#     Super Admin
#     Both require approval
#
# Employee registration is handled separately by /register.
# =========================================================

@app.route("/staff-register", methods=["GET", "POST"])
def staff_register():

    db = admin_client()

    # -----------------------------------------------------
    # Check whether ANY Super Admin already exists
    # -----------------------------------------------------

    try:

        existing_admin_result = (
            db.table("profiles")
            .select("id")
            .eq("role", "super_admin")
            .limit(1)
            .execute()
        )

        has_super_admin = bool(
            existing_admin_result.data
        )

    except Exception as e:

        print(
            "STAFF REGISTRATION ADMIN CHECK ERROR:",
            repr(e)
        )

        flash(
            f"Could not verify staff registration: {str(e)}",
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

        employee_id = request.form.get(
            "employee_id",
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


        # -------------------------------------------------
        # FIRST SUPER ADMIN
        # -------------------------------------------------

        if not has_super_admin:

            role = "super_admin"

            submitted_secret = request.form.get(
                "admin_setup_secret",
                ""
            ).strip()

            expected_secret = os.getenv(
                "ADMIN_SETUP_SECRET"
            )

            if not expected_secret:

                flash(
                    "Super Admin setup is not configured.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=False
                )

            if not hmac.compare_digest(
                submitted_secret,
                expected_secret
            ):

                flash(
                    "Invalid Admin Setup Secret.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=False
                )

        else:

            # -------------------------------------------------
            # AFTER FIRST SUPER ADMIN
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
                    has_super_admin=True
                )

            role = requested_role


        # -------------------------------------------------
        # VALIDATION
        # -------------------------------------------------

        if not full_name:

            flash(
                "Full name is required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        # School / Employee ID is required for staff
        if not employee_id:

            flash(
                "School / Employee ID is required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        if not email:

            flash(
                "Email is required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        if not password:

            flash(
                "Password is required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        if len(password) < 6:

            flash(
                "Password must contain at least 6 characters.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


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
        # CHECK EMPLOYEE ID
        # -------------------------------------------------

        try:

            existing_employee_id = (
                db.table("profiles")
                .select("id,full_name,role")
                .eq(
                    "employee_id",
                    employee_id
                )
                .limit(1)
                .execute()
            )

            if existing_employee_id.data:

                flash(
                    "That School / Employee ID is already registered.",
                    "error"
                )

                return render_template(
                    "staff_register.html",
                    has_super_admin=has_super_admin
                )

        except Exception as e:

            print(
                "EMPLOYEE ID CHECK ERROR:",
                repr(e)
            )

            flash(
                f"Could not verify Employee ID: {str(e)}",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        # -------------------------------------------------
        # CREATE AUTH ACCOUNT
        # -------------------------------------------------

        user = None

        try:

            auth_result = db.auth.admin.create_user({

                "email": email,

                "password": password,

                "email_confirm": True,

                "user_metadata": {
                    "full_name": full_name
                }

            })

            user = auth_result.user

            if not user:

                raise RuntimeError(
                    "Supabase did not return the created user."
                )


            user_id = str(
                user.id
            )


            # -------------------------------------------------
            # FIRST SUPER ADMIN = APPROVED
            #
            # Additional staff = PENDING
            # -------------------------------------------------

            approved = not has_super_admin


            # -------------------------------------------------
            # CREATE PROFILE
            # -------------------------------------------------

            db.table("profiles").upsert({

                "id": user_id,

                "full_name": full_name,

                "employee_id": employee_id,

                "role": role,

                "approved": approved

            }).execute()


            # -------------------------------------------------
            # SUCCESS MESSAGE
            # -------------------------------------------------

            if approved:

                flash(
                    "Initial Super Admin created successfully. "
                    "You can now sign in.",
                    "success"
                )

            else:

                if role == "hr":

                    role_name = "Payroll Clerk"

                else:

                    role_name = "Super Admin"


                flash(
                    f"{role_name} registration successful. "
                    "Please wait for Super Admin approval.",
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
            # If Auth user was created but profile creation
            # failed, remove the orphan Auth account.
            # -------------------------------------------------

            if user is not None:

                try:

                    db.auth.admin.delete_user(
                        str(user.id)
                    )

                except Exception as cleanup_error:

                    print(
                        "ORPHAN USER CLEANUP ERROR:",
                        repr(cleanup_error)
                    )


            flash(
                f"Staff registration failed: {str(e)}",
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
# OLD SETUP URL
#
# Your login.html currently points to:
#     url_for('setup_super_admin')
#
# Keep this route so you do NOT have to immediately change
# the login page.
#
# It now redirects to the new staff registration page.
# =========================================================

@app.route(
    "/setup-super-admin",
    methods=["GET", "POST"]
)
def setup_super_admin():

    return redirect(
        url_for("staff_register")
    )


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

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


            user_id = str(
                user.id
            )


            # -------------------------------------------------
            # Load profile
            # -------------------------------------------------

            profile_result = (

                admin_client()

                .table("profiles")

                .select(
                    "id,"
                    "full_name,"
                    "employee_id,"
                    "role,"
                    "approved"
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
                    "Your registration is waiting "
                    "for Super Admin approval.",
                    "warning"
                )

                return render_template(
                    "login.html"
                )


            # -------------------------------------------------
            # SESSION
            # -------------------------------------------------

            session.clear()

            session["user_id"] = user_id

            session["email"] = (
                user.email
                if user.email
                else email
            )

            session["full_name"] = (
                profile.get("full_name")
                or ""
            )

            session["employee_id"] = (
                profile.get("employee_id")
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

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if request.method == "POST":

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        employee_id = request.form.get(
            "employee_id",
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
        # VALIDATION
        # -------------------------------------------------

        if not full_name:

            flash(
                "Full name is required.",
                "error"
            )

            return render_template(
                "register.html"
            )


        if not employee_id:

            flash(
                "School / Employee ID is required.",
                "error"
            )

            return render_template(
                "register.html"
            )


        if not email:

            flash(
                "Email is required.",
                "error"
            )

            return render_template(
                "register.html"
            )


        if not password:

            flash(
                "Password is required.",
                "error"
            )

            return render_template(
                "register.html"
            )


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

            db = admin_client()


            # -------------------------------------------------
            # CHECK EMPLOYEE ID
            # -------------------------------------------------

            existing_employee_id = (

                db.table("profiles")

                .select(
                    "id,full_name,role"
                )

                .eq(
                    "employee_id",
                    employee_id
                )

                .limit(1)

                .execute()

            )


            if existing_employee_id.data:

                flash(
                    "That School / Employee ID "
                    "is already registered.",
                    "error"
                )

                return render_template(
                    "register.html"
                )


            # -------------------------------------------------
            # CREATE AUTH USER
            #
            # Use admin API so registration doesn't depend
            # on Supabase email rate limits.
            # -------------------------------------------------

            auth_result = db.auth.admin.create_user({

                "email": email,

                "password": password,

                "email_confirm": True,

                "user_metadata": {
                    "full_name": full_name
                }

            })


            user = auth_result.user


            if not user:

                raise RuntimeError(
                    "Supabase did not return the created user."
                )


            user_id = str(
                user.id
            )


            # -------------------------------------------------
            # CREATE EMPLOYEE PROFILE
            # -------------------------------------------------

            db.table("profiles").upsert({

                "id": user_id,

                "full_name": full_name,

                "employee_id": employee_id,

                "role": "employee",

                "approved": False

            }).execute()


            flash(
                "Registration successful. "
                "Please wait for Super Admin approval.",
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
        ).strip().lower()


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
                "PASSWORD RESET ERROR:",
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


    stats = {

        "employees": 0,

        "pending_accounts": 0,

        "flagged_dtr": 0,

        "pending_payroll": 0

    }


    pending_users = []

    payroll_runs = []


    # =====================================================
    # SUPER ADMIN
    # =====================================================

    if role == "super_admin":

        # ---------------------------------------------
        # Employee count
        # ---------------------------------------------

        try:

            result = (
                db.table("employees")
                .select(
                    "id",
                    count="exact"
                )
                .execute()
            )

            stats["employees"] = (
                result.count
                or 0
            )

        except Exception as e:

            print(
                "EMPLOYEE COUNT ERROR:",
                repr(e)
            )


        # ---------------------------------------------
        # Pending accounts
        #
        # IMPORTANT:
        # We retrieve the role and employee_id so the
        # dashboard knows whether this is:
        #
        # Employee
        # Payroll Clerk
        # Super Admin
        # ---------------------------------------------

        try:

            result = (

                db.table("profiles")

                .select(
                    "id,"
                    "full_name,"
                    "employee_id,"
                    "role,"
                    "approved,"
                    "created_at"
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
                result.data
                or []
            )


            stats["pending_accounts"] = len(
                pending_users
            )


        except Exception as e:

            print(
                "PENDING USER ERROR:",
                repr(e)
            )


        # ---------------------------------------------
        # Flagged DTR
        # ---------------------------------------------

        try:

            result = (

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
                result.count
                or 0
            )


        except Exception as e:

            print(
                "DTR COUNT ERROR:",
                repr(e)
            )


        # ---------------------------------------------
        # Payroll awaiting approval
        # ---------------------------------------------

        try:

            result = (

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
                result.data
                or []
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
    # PAYROLL CLERK
    # =====================================================

    elif role == "hr":

        # ---------------------------------------------
        # Active employees
        # ---------------------------------------------

        try:

            result = (

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
                result.count
                or 0
            )


        except Exception as e:

            print(
                "HR EMPLOYEE COUNT ERROR:",
                repr(e)
            )


        # ---------------------------------------------
        # Flagged DTR
        # ---------------------------------------------

        try:

            result = (

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
                result.count
                or 0
            )


        except Exception as e:

            print(
                "HR DTR COUNT ERROR:",
                repr(e)
            )


        # ---------------------------------------------
        # Recent payroll
        # ---------------------------------------------

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
    # EMPLOYEE
    #
    # No special admin statistics.
    # =====================================================


    return render_template(

        "dashboard.html",

        profile=profile,

        stats=stats,

        pending_users=pending_users,

        payroll_runs=payroll_runs

    )


# =========================================================
# APPROVE USER
#
# IMPORTANT:
# DO NOT CHANGE THE ROLE.
#
# If user is:
#     hr           -> stays hr
#     super_admin  -> stays super_admin
#     employee     -> stays employee
#
# Only approved becomes TRUE.
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
                "id,"
                "full_name,"
                "employee_id,"
                "role,"
                "approved"
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


        user = found.data[0]


        # -------------------------------------------------
        # ONLY CHANGE APPROVED
        # -------------------------------------------------

        db.table("profiles").update({

            "approved": True

        }).eq(
            "id",
            user_id
        ).execute()


        role = user.get(
            "role"
        )


        if role == "hr":

            role_name = "Payroll Clerk"

        elif role == "super_admin":

            role_name = "Super Admin"

        else:

            role_name = "Employee"


        flash(
            f"{role_name} registration approved.",
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


        # Delete Auth account first
        try:

            db.auth.admin.delete_user(
                user_id
            )

        except Exception as auth_error:

            print(
                "AUTH DELETE ERROR:",
                repr(auth_error)
            )


        # Delete profile
        try:

            db.table("profiles") \
                .delete() \
                .eq(
                    "id",
                    user_id
                ) \
                .execute()

        except Exception as profile_error:

            print(
                "PROFILE DELETE ERROR:",
                repr(profile_error)
            )


        flash(
            "Registration rejected.",
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
# EMPLOYEE MANAGEMENT
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
                    float(monthly_salary)
                    if monthly_salary
                    else None,

                "hourly_rate":
                    float(hourly_rate)
                    if hourly_rate
                    else None,

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

                "active":
                    True

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


    try:

        rows = (

            db.table("employees")

            .select("*")

            .order(
                "full_name"
            )

            .execute()

            .data

            or []

        )

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
#
# ONLY SUPER ADMIN
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


                # -----------------------------------------
                # Find employee
                # -----------------------------------------

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


                # -----------------------------------------
                # Evaluate DTR
                #
                # NO_LOGOUT_NO_PAY is handled inside
                # evaluate_day().
                # -----------------------------------------

                evaluation = evaluate_day(
                    row
                )


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
                        batch_id

                }, on_conflict="employee_id,work_date") \
                .execute()


                imported += 1


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
#
# Super Admin:
#     Can view payroll
#
# Payroll Clerk:
#     Can generate payroll
#
# Employee:
#     Cannot generate payroll
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


    if role not in (
        "super_admin",
        "hr"
    ):

        flash(
            "You do not have permission "
            "to access payroll processing.",
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

        # ONLY HR
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

            # ---------------------------------------------
            # CREATE PAYROLL RUN
            # ---------------------------------------------

            run_result = (

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


            if not run_result.data:

                raise RuntimeError(
                    "Could not create payroll run."
                )


            run_id = (
                run_result.data[0]["id"]
            )


            # ---------------------------------------------
            # ACTIVE EMPLOYEES
            # ---------------------------------------------

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


            # ---------------------------------------------
            # PROCESS EACH EMPLOYEE
            # ---------------------------------------------

            for employee in employee_rows:


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

                    .data

                    or []

                )


                # -----------------------------------------
                # ACTIVE DEDUCTIONS
                # -----------------------------------------

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


                # -----------------------------------------
                # CALCULATE
                # -----------------------------------------

                calculation = calculate(

                    employee,

                    dtr_rows,

                    deductions,

                    int(cutoff_no)

                )


                # -----------------------------------------
                # PAYROLL ITEM
                # -----------------------------------------

                item_result = (

                    db.table(
                        "payroll_items"
                    )

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


                # -----------------------------------------
                # DEDUCTIONS
                # -----------------------------------------

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
                                )

                        }).execute()


            flash(

                "Payroll generated successfully. "
                "It is now waiting for Super Admin approval.",

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
    # GET PAYROLL LIST
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

            .data

            or []

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

            .data

            or []

        )


        return render_template(

            "payroll_detail.html",

            payroll_run=
                run_result.data[0],

            items=items,

            profile=
                current_profile()

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
#
# ONLY SUPER ADMIN
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

                "status":
                    "approved"

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
                        "RAG assessment is "
                        "temporarily unavailable."
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
                    "assessed"

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


        if (
            not profile
            or profile.get("role")
            not in (
                "super_admin",
                "hr"
            )
        ):

            query = query.eq(
                "user_id",
                session.get("user_id")
            )


        rows = (

            query

            .order(
                "created_at",
                desc=True
            )

            .execute()

            .data

            or []

        )


    except Exception as e:

        print(
            "COMPLAINT LIST ERROR:",
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
