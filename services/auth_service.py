from functools import wraps

from flask import (
    session,
    redirect,
    url_for,
    flash,
)

from services.supabase_service import admin_client


# =========================================================
# CURRENT PROFILE
# =========================================================

def current_profile():
    uid = session.get("user_id")

    if not uid:
        return None

    # -----------------------------------------------------
    # First try to get the latest profile from Supabase
    # -----------------------------------------------------

    try:
        db = admin_client()

        result = (
            db.table("profiles")
            .select("id,full_name,role,approved")
            .eq("id", uid)
            .limit(1)
            .execute()
        )

        if result.data:
            profile = result.data[0]

            # Keep the session synchronized
            session["full_name"] = (
                profile.get("full_name") or ""
            )

            session["role"] = (
                profile.get("role") or "employee"
            )

            return profile

    except Exception as e:
        print(
            "CURRENT PROFILE DATABASE ERROR:",
            repr(e)
        )

    # -----------------------------------------------------
    # FALLBACK
    #
    # Login already verified the profile and stored the
    # information inside Flask's signed session.
    # -----------------------------------------------------

    role = session.get("role")
    full_name = session.get("full_name")

    if uid and role:
        return {
            "id": uid,
            "full_name": full_name or "",
            "role": role,
            "approved": True,
        }

    return None


# =========================================================
# LOGIN REQUIRED
# =========================================================

def login_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):

            flash(
                "Please sign in first.",
                "warning"
            )

            return redirect(
                url_for("login")
            )

        return fn(*args, **kwargs)

    return wrapper


# =========================================================
# ROLE REQUIRED
# =========================================================

def role_required(*roles):

    def decorator(fn):

        @wraps(fn)
        def wrapper(*args, **kwargs):

            if not session.get("user_id"):

                flash(
                    "Please sign in first.",
                    "warning"
                )

                return redirect(
                    url_for("login")
                )

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

            user_role = profile.get(
                "role",
                "employee"
            )

            if user_role not in roles:

                flash(
                    "You do not have permission "
                    "to access that page.",
                    "error"
                )

                return redirect(
                    url_for("dashboard")
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator
