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
