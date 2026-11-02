import os
import uuid
import hmac
from io import BytesIO
from datetime import datetime, timezone

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
    send_file,
    abort
)

from dotenv import load_dotenv

from services.supabase_service import (
    public_client,
    admin_client
)

from services.auth_service import (
    login_required,
    role_required,
    current_profile
)

from services.dtr_service import (
    parse_csv,
    evaluate_day,
    payroll_attendance_summary
)

from services.payroll_service import calculate
from services.payslip_service import generate_payslip_pdf


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
# FLASK APPLICATION
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "change-this-secret"
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
        return redirect(
            url_for("dashboard")
        )

    return redirect(
        url_for("login")
    )


# =========================================================
# STAFF REGISTRATION
# =========================================================

@app.route(
    "/staff-register",
    methods=["GET", "POST"]
)
def staff_register():

    db = admin_client()

    # -----------------------------------------------------
    # CHECK IF SUPER ADMIN EXISTS
    # -----------------------------------------------------

    try:

        result = (
            db.table("profiles")
            .select("id")
            .eq(
                "role",
                "super_admin"
            )
            .limit(1)
            .execute()
        )

        has_super_admin = bool(
            result.data
        )

    except Exception as e:

        print(
            "STAFF ADMIN CHECK ERROR:",
            repr(e)
        )

        flash(
            f"Could not check Super Admin status: {str(e)}",
            "error"
        )

        return redirect(
            url_for("login")
        )


    # =====================================================
    # POST
    # =====================================================

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


        # =================================================
        # FIRST SUPER ADMIN
        # =================================================

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
                    "ADMIN_SETUP_SECRET is not configured.",
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

            # =================================================
            # AFTER FIRST SUPER ADMIN
            # =================================================

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


        # =================================================
        # VALIDATION
        # =================================================

        if not full_name:

            flash(
                "Full name is required.",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


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


        # =================================================
        # DUPLICATE EMPLOYEE ID
        # =================================================

        try:

            existing_id = (
                db.table("profiles")
                .select(
                    "id,"
                    "full_name,"
                    "role"
                )
                .eq(
                    "employee_id",
                    employee_id
                )
                .limit(1)
                .execute()
            )


            if existing_id.data:

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
                f"Could not check Employee ID: {str(e)}",
                "error"
            )

            return render_template(
                "staff_register.html",
                has_super_admin=has_super_admin
            )


        # =================================================
        # CREATE AUTH ACCOUNT
        # =================================================

        user = None

        try:

            auth_result = (
                db.auth.admin.create_user({
                    "email": email,
                    "password": password,
                    "email_confirm": True,

                    "user_metadata": {
                        "full_name": full_name
                    }
                })
            )

            user = auth_result.user


            if not user:

                raise RuntimeError(
                    "Supabase did not return the created user."
                )


            user_id = str(
                user.id
            )


            # First Super Admin is automatically approved.

            approved = not has_super_admin


            # =================================================
            # CREATE PROFILE
            # =================================================

            db.table("profiles").upsert({

                "id":
                    user_id,

                "full_name":
                    full_name,

                "employee_id":
                    employee_id,

                "role":
                    role,

                "approved":
                    approved

            }).execute()


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


            if user is not None:

                try:

                    db.auth.admin.delete_user(
                        str(user.id)
                    )

                except Exception as cleanup_error:

                    print(
                        "CLEANUP ERROR:",
                        repr(cleanup_error)
                    )


            flash(
                f"Staff registration failed: {str(e)}",
                "error"
            )


    return render_template(
        "staff_register.html",
        has_super_admin=has_super_admin
    )


# =========================================================
# OLD SETUP URL
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

                "email":
                    email,

                "password":
                    password

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


            # =================================================
            # GET PROFILE
            # =================================================

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


            profile = (
                profile_result.data[0]
            )


            # =================================================
            # APPROVAL CHECK
            # =================================================

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


            # =================================================
            # CREATE SESSION
            # =================================================

            session.clear()

            session["user_id"] = (
                user_id
            )

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
# NORMAL EMPLOYEE REGISTRATION
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


        # =================================================
        # VALIDATION
        # =================================================

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


            # =================================================
            # CHECK DUPLICATE EMPLOYEE ID
            # =================================================

            existing_id = (

                db.table("profiles")

                .select("id")

                .eq(
                    "employee_id",
                    employee_id
                )

                .limit(1)

                .execute()

            )


            if existing_id.data:

                flash(
                    "That School / Employee ID is already registered.",
                    "error"
                )

                return render_template(
                    "register.html"
                )


            # =================================================
            # CREATE AUTH USER
            # =================================================

            auth_result = (
                db.auth.admin.create_user({

                    "email":
                        email,

                    "password":
                        password,

                    "email_confirm":
                        True,

                    "user_metadata": {
                        "full_name":
                            full_name
                    }

                })
            )


            user = (
                auth_result.user
            )


            if not user:

                raise RuntimeError(
                    "Supabase did not return the created user."
                )


            user_id = str(
                user.id
            )


            # =================================================
            # CREATE PROFILE
            # =================================================

            db.table("profiles").upsert({

                "id":
                    user_id,

                "full_name":
                    full_name,

                "employee_id":
                    employee_id,

                "role":
                    "employee",

                "approved":
                    False

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
        return redirect(url_for("login"))

    db = admin_client()
    role = profile.get("role", "employee")
    stats = {
        "employees": 0,
        "pending_accounts": 0,
        "flagged_dtr": 0,
        "pending_payroll": 0,
    }
    pending_users = []
    payroll_runs = []
    dtr_batches = []
    selected_dtr_batch = session.get("selected_dtr_batch")
    employee = None
    attendance_records = []
    employee_payroll = []

    if role in ("super_admin", "hr"):
        try:
            result = (
                db.table("employees").select("id", count="exact")
                .eq("active", True).execute()
            )
            stats["employees"] = result.count or 0
        except Exception:
            app.logger.exception("EMPLOYEE COUNT ERROR")

        try:
            result = (
                db.table("dtr_entries").select("id", count="exact")
                .eq("requires_review", True).execute()
            )
            stats["flagged_dtr"] = result.count or 0
        except Exception:
            app.logger.exception("FLAGGED DTR ERROR")

    if role == "super_admin":
        try:
            pending_users = (
                db.table("profiles")
                .select("id,full_name,employee_id,role,approved,created_at")
                .eq("approved", False)
                .order("created_at").execute().data or []
            )
            stats["pending_accounts"] = len(pending_users)
        except Exception:
            app.logger.exception("PENDING USER ERROR")

        try:
            payroll_runs = (
                db.table("payroll_runs").select("*")
                .eq("status", "draft")
                .order("created_at", desc=True).execute().data or []
            )
            stats["pending_payroll"] = len(payroll_runs)
        except Exception:
            app.logger.exception("PENDING PAYROLL ERROR")

    elif role == "hr":
        try:
            dtr_rows = (
                db.table("dtr_entries")
                .select("import_batch,work_date,requires_review")
                .order("work_date").execute().data or []
            )
            grouped = {}
            for row in dtr_rows:
                batch_id = row.get("import_batch")
                work_date = row.get("work_date")
                if not batch_id:
                    continue
                if batch_id not in grouped:
                    grouped[batch_id] = {
                        "id": batch_id,
                        "start_date": work_date,
                        "end_date": work_date,
                        "records": 0,
                        "flagged": 0,
                    }
                batch = grouped[batch_id]
                batch["records"] += 1
                if row.get("requires_review"):
                    batch["flagged"] += 1
                if work_date:
                    if not batch["start_date"] or work_date < batch["start_date"]:
                        batch["start_date"] = work_date
                    if not batch["end_date"] or work_date > batch["end_date"]:
                        batch["end_date"] = work_date
            dtr_batches = sorted(
                grouped.values(),
                key=lambda batch: batch.get("start_date") or "",
                reverse=True,
            )
        except Exception:
            app.logger.exception("DTR BATCH LOAD ERROR")

        try:
            payroll_runs = (
                db.table("payroll_runs").select("*")
                .order("created_at", desc=True).limit(10)
                .execute().data or []
            )
        except Exception:
            app.logger.exception("CLERK PAYROLL ERROR")

    else:
        try:
            result = (
                db.table("employees").select("*")
                .eq("profile_id", profile["id"])
                .limit(1).execute()
            )
            if result.data:
                employee = result.data[0]

            if employee:
                attendance_records = (
                    db.table("dtr_entries").select("*")
                    .eq("employee_id", employee["id"])
                    .order("work_date", desc=True)
                    .execute().data or []
                )

                # Retrieve only this employee's payroll items.
                payroll_items = (
                    db.table("payroll_items").select("*")
                    .eq("employee_id", employee["id"])
                    .execute().data or []
                )

                # Publish only payroll that the Super Admin approved.
                # The employee cannot access the staff payroll detail route.
                for item in payroll_items:
                    run_id = item.get("payroll_run_id")
                    if not run_id:
                        continue
                    run_result = (
                        db.table("payroll_runs")
                        .select("id,start_date,end_date,cutoff_no,status")
                        .eq("id", run_id)
                        .eq("status", "approved")
                        .limit(1).execute()
                    )
                    if run_result.data:
                        approved_item = dict(item)
                        approved_item["payroll_run"] = run_result.data[0]

                        # Fetch the itemized deductions saved during payroll generation.
                        # Never substitute assumed statutory contribution amounts.
                        approved_item["deduction_details"] = []
                        approved_item["deductions_loaded"] = False
                        try:
                            deduction_result = (
                                db.table("payroll_item_deductions")
                                .select("name,amount")
                                .eq("payroll_item_id", item["id"])
                                .execute()
                            )
                            approved_item["deduction_details"] = (
                                deduction_result.data or []
                            )
                            approved_item["deductions_loaded"] = True
                        except Exception:
                            app.logger.exception(
                                "PAYROLL DEDUCTION DETAILS ERROR for item %s",
                                item.get("id"),
                            )

                        employee_payroll.append(approved_item)

                employee_payroll.sort(
                    key=lambda item: (
                        item["payroll_run"].get("end_date") or "",
                        item["payroll_run"].get("start_date") or "",
                    ),
                    reverse=True,
                )
        except Exception:
            app.logger.exception("EMPLOYEE DASHBOARD ERROR")
            flash("Some attendance or payroll records could not be loaded.", "error")

    return render_template(
        "dashboard.html",
        profile=profile,
        stats=stats,
        pending_users=pending_users,
        payroll_runs=payroll_runs,
        dtr_batches=dtr_batches,
        selected_dtr_batch=selected_dtr_batch,
        employee=employee,
        attendance_records=attendance_records,
        employee_payroll=employee_payroll,
    )


# =========================================================
# APPROVE REGISTRATION
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


        # =================================================
        # GET USER PROFILE
        # =================================================

        result = (

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


        if not result.data:

            flash(
                "Registration not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )


        user = (
            result.data[0]
        )


        user_role = (
            user.get(
                "role"
            )
        )


        full_name = (
            user.get(
                "full_name"
            )
            or ""
        )


        employee_id = (
            user.get(
                "employee_id"
            )
            or ""
        )


        # =================================================
        # APPROVE PROFILE
        # =================================================

        db.table("profiles").update({

            "approved":
                True

        }).eq(
            "id",
            user_id
        ).execute()


        # =================================================
        # NORMAL EMPLOYEE
        # =================================================

        if user_role == "employee":

            if not employee_id:

                flash(
                    "Employee approved, but no School / Employee ID was provided.",
                    "warning"
                )

                return redirect(
                    url_for("dashboard")
                )


            # -------------------------------------------------
            # CHECK EMPLOYEE RECORD
            # -------------------------------------------------

            existing_employee = (

                db.table("employees")

                .select(
                    "id,"
                    "employee_no"
                )

                .eq(
                    "employee_no",
                    employee_id
                )

                .limit(1)

                .execute()

            )


            # -------------------------------------------------
            # CREATE EMPLOYEE RECORD
            # -------------------------------------------------

            if not existing_employee.data:

                db.table(
                    "employees"
                ).insert({

                    "profile_id":
                        user_id,

                    "employee_no":
                        employee_id,

                    "full_name":
                        full_name,

                    # IMPORTANT:
                    # Database constraint requires lowercase.
                    "employee_type":
                        "regular",

                    "department":
                        "",

                    "monthly_salary":
                        0,

                    "hourly_rate":
                        0,

                    "standard_hours":
                        8,

                    "workdays_per_month":
                        22,

                    "active":
                        True

                }).execute()


                flash(
                    f"{full_name} approved and added "
                    f"to Employee Master Records.",
                    "success"
                )

            else:

                flash(
                    f"{full_name} approved successfully. "
                    f"Employee record already exists.",
                    "success"
                )


        # =================================================
        # PAYROLL CLERK
        # =================================================

        elif user_role == "hr":

            flash(
                f"{full_name} approved as Payroll Clerk.",
                "success"
            )


        # =================================================
        # SUPER ADMIN
        # =================================================

        elif user_role == "super_admin":

            flash(
                f"{full_name} approved as Super Admin.",
                "success"
            )


        else:

            flash(
                f"{full_name} approved successfully.",
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


        # -------------------------------------------------
        # DELETE AUTH ACCOUNT
        # -------------------------------------------------

        try:

            db.auth.admin.delete_user(
                user_id
            )

        except Exception as e:

            print(
                "AUTH DELETE ERROR:",
                repr(e)
            )


        # -------------------------------------------------
        # DELETE PROFILE
        # -------------------------------------------------

        try:

            (
                db.table("profiles")

                .delete()

                .eq(
                    "id",
                    user_id
                )

                .execute()
            )


        except Exception as e:

            print(
                "PROFILE DELETE ERROR:",
                repr(e)
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
# EMPLOYEE MASTER RECORDS
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


    # =====================================================
    # MANUAL EMPLOYEE ADD
    # =====================================================

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


            employee_type = request.form.get(
                "employee_type",
                ""
            ).strip().lower()


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
                    employee_type,

                "department":
                    request.form.get(
                        "department",
                        ""
                    ).strip(),

                "monthly_salary":
                    float(
                        monthly_salary
                    )
                    if monthly_salary
                    else 0,

                "hourly_rate":
                    float(
                        hourly_rate
                    )
                    if hourly_rate
                    else 0,

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


    # =====================================================
    # LOAD EMPLOYEES
    # =====================================================

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
# MASTER DTR UPLOAD
# SUPER ADMIN ONLY
# =========================================================

@app.route("/dtr/upload", methods=["GET", "POST"])
@login_required
@role_required("super_admin")
def dtr_upload():
    """Create a DTR batch or append only missing entries to an existing batch."""
    db = admin_client()

    def list_batches():
        records = (db.table("dtr_entries")
                   .select("import_batch,work_date")
                   .order("work_date", desc=True).execute().data or [])
        grouped = {}
        for record in records:
            key, day = record.get("import_batch"), record.get("work_date")
            if not key:
                continue
            if key not in grouped:
                grouped[key] = {"id": key, "count": 0, "start": day, "end": day}
            group = grouped[key]
            group["count"] += 1
            if day:
                group["start"] = min(group["start"] or day, day)
                group["end"] = max(group["end"] or day, day)
        return sorted(grouped.values(), key=lambda g: g["end"] or "", reverse=True)

    try:
        batches = list_batches()
    except Exception:
        app.logger.exception("DTR BATCH LIST ERROR")
        flash("Could not load DTR batches. Please try again.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("dtr_upload.html", dtr_batches=batches)

    upload = request.files.get("file")
    mode = request.form.get("upload_mode", "new")
    batch_id = request.form.get("batch_id", "").strip()
    if not upload or not upload.filename:
        flash("Please select a CSV file.", "error")
        return redirect(url_for("dtr_upload"))
    if mode not in ("new", "append"):
        flash("Invalid upload mode.", "error")
        return redirect(url_for("dtr_upload"))
    if mode == "append" and batch_id not in {b["id"] for b in batches}:
        flash("Select an existing DTR batch.", "error")
        return redirect(url_for("dtr_upload"))

    try:
        file_bytes = upload.read(2 * 1024 * 1024 + 1)
        if len(file_bytes) > 2 * 1024 * 1024:
            raise ValueError("CSV exceeds the 2 MB upload limit.")
        parsed = parse_csv(file_bytes)
        if not parsed:
            raise ValueError("The CSV contains no attendance rows.")
        # Avoid duplicate employee/date entries in the same CSV.
        keys = [(r["employee_no"], r["work_date"].isoformat()) for r in parsed]
        if len(keys) != len(set(keys)):
            raise ValueError("CSV contains duplicate employee/date rows. Remove duplicates before uploading.")

        # Preload employee UUIDs. A missing employee is skipped, as in the old importer.
        employee_numbers = sorted({r["employee_no"] for r in parsed if r["employee_no"]})
        employee_map = {}
        for number in employee_numbers:
            result = (db.table("employees").select("*")
                      .eq("employee_no", number).limit(1).execute())
            if result.data:
                employee_map[number] = result.data[0]

        # Disallow append to any batch whose date range overlaps approved payroll.
        # Conservative, because historical payroll_runs has no direct batch_id column.
        if mode == "append":
            target = next(b for b in batches if b["id"] == batch_id)
            existing_days = (db.table("dtr_entries")
                             .select("employee_id,work_date")
                             .eq("import_batch", batch_id).execute().data or [])
            old_dates = [r["work_date"] for r in existing_days if r.get("work_date")]
            incoming_dates = [r["work_date"].isoformat() for r in parsed]
            low = min(old_dates + incoming_dates)
            high = max(old_dates + incoming_dates)
            approved = (db.table("payroll_runs").select("id")
                        .eq("status", "approved")
                        .lte("start_date", high).gte("end_date", low)
                        .limit(1).execute().data or [])
            if approved:
                raise ValueError("This batch overlaps an approved payroll period. Append is blocked to protect finalized records.")
        else:
            batch_id = str(uuid.uuid4())

        # Preflight: duplicates are SKIPPED, never silently overwritten or reassigned.
        # Any existing employee/date row, in any batch, stays untouched.
        prepared = []
        skipped_missing = 0
        skipped_existing = 0
        for row in parsed:
            employee = employee_map.get(row["employee_no"])
            if not employee:
                skipped_missing += 1
                continue
            day = row["work_date"].isoformat()
            prior = (db.table("dtr_entries").select("id")
                     .eq("employee_id", employee["id"])
                     .eq("work_date", day).limit(1).execute().data or [])
            if prior:
                skipped_existing += 1
                continue
            check = evaluate_day(row["work_date"], row["time_in"], row["time_out"],
                                 standard_hours=employee.get("standard_hours") or 8)
            prepared.append({
                "employee_id": employee["id"], "work_date": day,
                "time_in": row["time_in"].isoformat() if row["time_in"] else None,
                "time_out": row["time_out"].isoformat() if row["time_out"] else None,
                "status": check.get("status"),
                "payable_hours": float(check.get("payable_hours") or 0),
                "reason": check.get("reason"),
                "requires_review": check.get("requires_review", False),
                "import_batch": batch_id
            })

        if not prepared:
            flash(f"No new records: {skipped_existing} already exist; {skipped_missing} unmatched employees.", "warning")
            return redirect(url_for("dtr_upload"))

        # The DB unique constraint employee_id,work_date protects against races.
        # No upsert: collisions are reported rather than overwriting existing records.
        for item in prepared:
            db.table("dtr_entries").insert(item).execute()
        flash(f"DTR saved: {len(prepared)} new; {skipped_existing} duplicate dates skipped; "
              f"{skipped_missing} unmatched employees. Batch: {batch_id}", "success")
        return redirect(url_for("dashboard"))
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        app.logger.exception("DTR IMPORT / APPEND ERROR")
        flash("DTR import failed. Some records may already have been saved; review the batch before retrying.", "error")
    return redirect(url_for("dtr_upload"))



# =========================================================
# SUPER ADMIN DTR REVIEW AND CORRECTION
# =========================================================

@app.route("/dtr/review/<batch_id>")
@login_required
@role_required("super_admin")
def dtr_review(batch_id):
    db = admin_client()
    try:
        rows = (db.table("dtr_entries")
                .select("id,employee_id,work_date,time_in,time_out,status,reason,requires_review,payable_hours,import_batch,employees(employee_no,full_name)")
                .eq("import_batch", batch_id)
                .order("work_date")
                .execute().data or [])
        if not rows:
            flash("DTR batch not found.", "error")
            return redirect(url_for("dtr_upload"))
        flagged = [row for row in rows if row.get("requires_review")]
        return render_template("dtr_review.html", batch_id=batch_id, records=rows,
                               flagged=flagged, total=len(rows))
    except Exception:
        app.logger.exception("DTR REVIEW ERROR")
        flash("Could not load DTR review.", "error")
        return redirect(url_for("dtr_upload"))


@app.route("/dtr/review/<batch_id>/correct/<entry_id>", methods=["POST"])
@login_required
@role_required("super_admin")
def dtr_correct(batch_id, entry_id):
    db = admin_client()
    # Sensitive payroll edits require a fresh DB-verified, approved Super Admin.
    verified = (db.table("profiles").select("id,role,approved")
                .eq("id", session.get("user_id"))
                .eq("role", "super_admin").eq("approved", True)
                .limit(1).execute().data or [])
    if not verified:
        abort(403)
    try:
        current = (db.table("dtr_entries").select("*")
                   .eq("id", entry_id).eq("import_batch", batch_id)
                   .limit(1).execute().data or [])
        if not current:
            flash("Attendance entry not found in that batch.", "error")
            return redirect(url_for("dtr_review", batch_id=batch_id))
        old = current[0]
        work_date = old["work_date"]
        # Conservative protection: do not edit dates covered by any approved/released payroll.
        protected = (db.table("payroll_runs").select("id")
                     .in_("status", ["approved", "released"])
                     .lte("start_date", work_date).gte("end_date", work_date)
                     .limit(1).execute().data or [])
        if protected:
            raise ValueError("This date overlaps an approved or released payroll. Correction is blocked; follow a formal adjustment workflow.")
        explanation = request.form.get("correction_reason", "").strip()
        if len(explanation) < 5:
            raise ValueError("Enter a correction reason (at least 5 characters).")
        from datetime import date as _date
        from services.dtr_service import parse_datetime_on_date
        work_day = _date.fromisoformat(work_date)
        ti = parse_datetime_on_date(request.form.get("time_in", ""), work_day)
        to = parse_datetime_on_date(request.form.get("time_out", ""), work_day)
        emp = (db.table("employees").select("standard_hours")
               .eq("id", old["employee_id"]).limit(1).execute().data or [])
        if not emp:
            raise ValueError("Employee record was not found.")
        check = evaluate_day(work_day, ti, to,
                             standard_hours=emp[0].get("standard_hours") or 8)
        if check.get("requires_review"):
            raise ValueError("The corrected times are still invalid: " + str(check.get("reason")))
        new = {
            "time_in": ti.isoformat() if ti else None,
            "time_out": to.isoformat() if to else None,
            "status": check["status"],
            "payable_hours": float(check["payable_hours"]),
            "requires_review": False,
            "reason": check["reason"],
            "reviewed_by": verified[0]["id"],
            "reviewed_at": datetime.now(timezone.utc).isoformat()
        }
        old_values = {key: old.get(key) for key in ("time_in","time_out","status","payable_hours","requires_review","reason")}
        # Audit insert first: no attendance mutation if audit storage is unavailable.
        audit = (db.table("dtr_correction_audit").insert({
            "dtr_entry_id": entry_id,
            "employee_id": old["employee_id"],
            "work_date": work_date,
            "import_batch": batch_id,
            "before_values": old_values,
            "after_values": {key: new[key] for key in old_values},
            "correction_reason": explanation,
            "corrected_by": verified[0]["id"]
        }).execute())
        if not audit.data:
            raise RuntimeError("Audit log could not be saved; attendance was not modified.")
        db.table("dtr_entries").update(new).eq("id", entry_id).eq("import_batch", batch_id).execute()
        flash("Attendance corrected, revalidated and logged.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        app.logger.exception("DTR CORRECTION ERROR")
        flash("Correction failed; check audit/history and database status before retrying.", "error")
    return redirect(url_for("dtr_review", batch_id=batch_id))

# =========================================================
# SELECT MASTER DTR
# PAYROLL CLERK ONLY
# =========================================================

@app.route(
    "/dtr/select/<batch_id>",
    methods=["POST"]
)
@login_required
@role_required("hr")
def select_dtr_batch(batch_id):

    try:

        db = admin_client()


        # -------------------------------------------------
        # VERIFY BATCH EXISTS
        # -------------------------------------------------

        result = (

            db.table(
                "dtr_entries"
            )

            .select(
                "import_batch,"
                "work_date"
            )

            .eq(
                "import_batch",
                batch_id
            )

            .execute()

        )


        rows = (
            result.data
            or []
        )


        if not rows:

            flash(
                "The selected Master DTR batch was not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )


        # -------------------------------------------------
        # GET BATCH DATE RANGE
        # -------------------------------------------------

        work_dates = [

            row.get(
                "work_date"
            )

            for row in rows

            if row.get(
                "work_date"
            )

        ]


        work_dates.sort()


        # -------------------------------------------------
        # SAVE SELECTION TO SESSION
        # -------------------------------------------------

        session[
            "selected_dtr_batch"
        ] = batch_id


        if work_dates:

            session[
                "selected_dtr_start_date"
            ] = work_dates[0]

            session[
                "selected_dtr_end_date"
            ] = work_dates[-1]


        flash(
            "Master DTR selected successfully. "
            "You can now generate payroll.",
            "success"
        )


        return redirect(
            url_for("dashboard")
        )


    except Exception as e:

        print(
            "SELECT DTR BATCH ERROR:",
            repr(e)
        )

        flash(
            f"Could not select Master DTR: {str(e)}",
            "error"
        )


        return redirect(
            url_for("dashboard")
        )


# =========================================================
# PAYROLL
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
    # ONLY SUPER ADMIN AND HR
    # -----------------------------------------------------

    if role not in (
        "super_admin",
        "hr"
    ):

        flash(
            "You do not have permission to access payroll.",
            "error"
        )

        return redirect(
            url_for("dashboard")
        )


    db = admin_client()


    # =====================================================
    # CURRENTLY SELECTED MASTER DTR
    # =====================================================

    selected_dtr_batch = session.get(
        "selected_dtr_batch"
    )


    selected_dtr_start_date = session.get(
        "selected_dtr_start_date"
    )


    selected_dtr_end_date = session.get(
        "selected_dtr_end_date"
    )


    # =====================================================
    # GENERATE PAYROLL
    # =====================================================

    if request.method == "POST":

        # -------------------------------------------------
        # ONLY PAYROLL CLERK CAN GENERATE
        # -------------------------------------------------

        if role != "hr":

            flash(
                "Only the Payroll Clerk can generate payroll.",
                "error"
            )

            return redirect(
                url_for("payroll")
            )


        # -------------------------------------------------
        # MASTER DTR MUST BE SELECTED
        # -------------------------------------------------

        if not selected_dtr_batch:

            flash(
                "Select a Master DTR from the Payroll Clerk "
                "dashboard before generating payroll.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )


        # -------------------------------------------------
        # FORM DATA
        # -------------------------------------------------

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

            cutoff_no = int(
                cutoff_no
            )


            # -------------------------------------------------
            # VERIFY SELECTED DTR BATCH
            # -------------------------------------------------

            batch_check = (

                db.table(
                    "dtr_entries"
                )

                .select("id")

                .eq(
                    "import_batch",
                    selected_dtr_batch
                )

                .limit(1)

                .execute()

            )


            if not batch_check.data:

                session.pop(
                    "selected_dtr_batch",
                    None
                )

                session.pop(
                    "selected_dtr_start_date",
                    None
                )

                session.pop(
                    "selected_dtr_end_date",
                    None
                )


                flash(
                    "The selected Master DTR no longer exists. "
                    "Please select another DTR batch.",
                    "error"
                )


                return redirect(
                    url_for("dashboard")
                )


            # Validate DTR batch and payroll dates BEFORE creating any payroll records.
            # Do not silently pay a full regular cutoff on empty attendance.
            from datetime import date as _date
            if _date.fromisoformat(start_date) > _date.fromisoformat(end_date):
                raise ValueError("Payroll start date must be on or before end date.")
            selected_rows = (db.table("dtr_entries")
                             .select("employee_id,work_date,requires_review")
                             .eq("import_batch", selected_dtr_batch).execute().data or [])
            if not selected_rows:
                raise ValueError("Selected DTR batch is empty.")
            all_days = [row["work_date"] for row in selected_rows]
            if min(all_days) != start_date or max(all_days) != end_date:
                raise ValueError("Payroll dates must match the selected DTR batch date range: "
                                 + min(all_days) + " to " + max(all_days))
            if any(row.get("requires_review") for row in selected_rows):
                raise ValueError("Selected DTR batch contains unresolved flagged attendance. Ask Super Admin to correct it first.")
            prior_runs = (db.table("payroll_runs").select("id,status")
                          .eq("start_date", start_date).eq("end_date", end_date)
                          .eq("cutoff_no", cutoff_no).execute().data or [])
            if prior_runs:
                raise ValueError("A payroll run for this exact period and cutoff already exists. Review it rather than generating a duplicate.")
            active_employees = (db.table("employees").select("id")
                                .eq("active", True).execute().data or [])
            covered_ids = {row["employee_id"] for row in selected_rows}
            missing_ids = [row["id"] for row in active_employees if row["id"] not in covered_ids]
            if missing_ids:
                raise ValueError(f"DTR batch is missing attendance for {len(missing_ids)} active employees. Complete the batch first.")

            # -------------------------------------------------
            # CREATE PAYROLL RUN
            # -------------------------------------------------

            run_result = (

                db.table(
                    "payroll_runs"
                )

                .insert({

                    "start_date":
                        start_date,

                    "end_date":
                        end_date,

                    "cutoff_no":
                        cutoff_no,

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
                run_result.data[0][
                    "id"
                ]
            )


            # -------------------------------------------------
            # GET ACTIVE EMPLOYEES
            # -------------------------------------------------

            employee_rows = (

                db.table(
                    "employees"
                )

                .select("*")

                .eq(
                    "active",
                    True
                )

                .execute()

                .data

                or []

            )


            # -------------------------------------------------
            # PROCESS EVERY EMPLOYEE
            # -------------------------------------------------

            for employee in employee_rows:


                # =============================================
                # GET DTR FOR THIS EMPLOYEE
                # ONLY FROM SELECTED MASTER DTR
                # =============================================

                dtr_rows = (

                    db.table(
                        "dtr_entries"
                    )

                    .select("*")

                    .eq(
                        "employee_id",
                        employee["id"]
                    )

                    .eq(
                        "import_batch",
                        selected_dtr_batch
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


                # =============================================
                # GET ACTIVE EMPLOYEE DEDUCTIONS
                # =============================================

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


                # =============================================
                # CALCULATE PAYROLL
                #
                # IMPORTANT:
                # calculate() requires SIX arguments.
                # =============================================

                calculation = calculate(

                    employee,

                    start_date,

                    end_date,

                    cutoff_no,

                    dtr_rows,

                    deductions

                )


                # =============================================
                # SAVE PAYROLL ITEM
                # =============================================

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

                        "attendance_summary": {
                            "dtr_batch":
                                selected_dtr_batch,

                            "start_date":
                                start_date,

                            "end_date":
                                end_date,

                            "dtr_records":
                                len(dtr_rows),

                            # Read-only attendance breakdown for future payslips.
                            # No shift schedule means no invented late/undertime.
                            **payroll_attendance_summary(
                                employee,
                                dtr_rows,
                                os.getenv("PAYROLL_SHIFT_START") or None,
                                os.getenv("PAYROLL_SHIFT_END") or None
                            )
                        }

                    })

                    .execute()

                )


                # =============================================
                # SAVE APPLIED DEDUCTIONS
                # =============================================

                if item_result.data:

                    item_id = (
                        item_result.data[0][
                            "id"
                        ]
                    )


                    for deduction in calculation.get(
                        "deductions",
                        []
                    ):


                        applied_amount = deduction.get(
                            "applied_amount",
                            0
                        )


                        if not applied_amount:
                            continue


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
                                applied_amount

                        }).execute()


            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

            flash(
                "Payroll generated successfully. "
                "It is waiting for Super Admin approval.",
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

            db.table(
                "payroll_runs"
            )

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


    # =====================================================
    # PAYROLL PAGE
    # =====================================================

    return render_template(

        "payroll.html",

        runs=
            runs,

        profile=
            profile,

        selected_dtr_batch=
            selected_dtr_batch,

        selected_dtr_start_date=
            selected_dtr_start_date,

        selected_dtr_end_date=
            selected_dtr_end_date

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


        run_result = (

            db.table(
                "payroll_runs"
            )

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


        items = (

            db.table(
                "payroll_items"
            )

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

            items=
                items,

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


        result = (

            db.table(
                "payroll_runs"
            )

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


        if not result.data:

            flash(
                "Payroll run not found.",
                "error"
            )

            return redirect(
                url_for("dashboard")
            )


        current_status = (
            result.data[0].get(
                "status"
            )
        )


        if current_status == "approved":

            flash(
                "This payroll is already approved.",
                "warning"
            )

        else:

            (

                db.table(
                    "payroll_runs"
                )

                .update({

                    "status":
                        "approved"

                })

                .eq(
                    "id",
                    run_id
                )

                .execute()

            )


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

@app.route("/complaints", methods=["GET", "POST"])
@login_required
def complaints():
    profile = current_profile()
    if not profile or not profile.get("id"):
        session.clear()
        return redirect(url_for("login"))

    db = admin_client()
    profile_id = str(profile["id"])
    role = profile.get("role", "employee")

    if request.method == "POST":
        complaint_text = request.form.get("complaint", "").strip()
        if not complaint_text:
            flash("Enter your complaint.", "error")
            return redirect(url_for("complaints"))

        try:
            # complaints.employee_id is a UUID referencing employees.id,
            # not the school employee number stored in the session.
            employee_result = (
                db.table("employees")
                .select("id")
                .eq("profile_id", profile_id)
                .limit(1)
                .execute()
            )
            employee_uuid = (
                employee_result.data[0].get("id")
                if employee_result.data else None
            )

            assessment = (
                "AI assessment is unavailable. "
                "The complaint requires HR review."
            )
            if assess_complaint:
                try:
                    rag_result = assess_complaint(complaint_text)
                    if isinstance(rag_result, str) and rag_result.strip():
                        assessment = rag_result
                except Exception:
                    app.logger.exception("RAG ASSESSMENT ERROR")

            # submitted_by is a UUID referencing the logged-in profile.
            # Both UUID fields must be valid UUIDs or None (never an empty string).
            db.table("complaints").insert({
                "employee_id": employee_uuid or None,
                "submitted_by": profile_id,
                "complaint": complaint_text,
                "assessment": assessment,
                "status": "assessed",
            }).execute()

            flash("Complaint submitted successfully.", "success")
            return redirect(url_for("complaints"))

        except Exception as exc:
            app.logger.exception("COMPLAINT SUBMISSION ERROR")
            flash(f"Complaint submission failed: {exc}", "error")

    try:
        query = db.table("complaints").select("*")
        if role not in ("super_admin", "hr"):
            # The database has submitted_by, not user_id.
            query = query.eq("submitted_by", profile_id)
        rows = (
            query.order("created_at", desc=True)
            .execute().data or []
        )
    except Exception:
        app.logger.exception("COMPLAINT LIST ERROR")
        rows = []
        flash("Could not load complaints.", "error")

    return render_template("complaints.html", complaints=rows)


# =========================================================
# EMPLOYEE PAYSLIP PDF DOWNLOAD
# =========================================================

@app.route('/employee/payslip/<item_id>/download')
@login_required
def download_payslip(item_id):
    profile = current_profile()
    if not profile or profile.get('role') != 'employee':
        abort(403)

    db = admin_client()
    owned = (db.table('employees').select('id,full_name,employee_no,employee_type,department')
             .eq('profile_id', profile['id']).limit(1).execute().data or [])
    if not owned:
        abort(404)
    employee = owned[0]

    items = (db.table('payroll_items').select('*')
             .eq('id', item_id).eq('employee_id', employee['id'])
             .limit(1).execute().data or [])
    if not items:
        abort(404)
    item = items[0]

    runs = (db.table('payroll_runs').select('*')
            .eq('id', item['payroll_run_id']).eq('status', 'approved')
            .limit(1).execute().data or [])
    if not runs:
        abort(404)

    deductions = (db.table('payroll_item_deductions').select('name,amount')
                  .eq('payroll_item_id', item['id']).execute().data or [])
    try:
        pdf = generate_payslip_pdf(employee, item, runs[0], deductions)
    except ValueError:
        app.logger.exception('PAYSLIP DATA VALIDATION ERROR')
        abort(409, description='Approved payslip data is incomplete or inconsistent.')
    response = send_file(BytesIO(pdf), mimetype='application/pdf', as_attachment=True,
                         download_name=f"payslip-{item['id']}.pdf")
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


# =========================================================
# RUN LOCALLY
# =========================================================

if __name__ == "__main__":

    app.run(

        host=
            "0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                5000
            )
        ),

        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"

    )
