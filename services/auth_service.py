from functools import wraps
from flask import session, redirect, url_for, flash
from services.supabase_service import admin_client


def current_profile():
    uid = session.get("user_id")
    if not uid:
        return None

    try:
        result = (
            admin_client()
            .table("profiles")
            .select("id,full_name,role,approved")
            .eq("id", uid)
            .limit(1)
            .execute()
        )

        if result.data:
            profile = result.data[0]
            session["full_name"] = profile.get("full_name") or ""
            session["role"] = profile.get("role") or "employee"
            return profile

    except Exception as e:
        print("CURRENT PROFILE ERROR:", repr(e))

    # Fallback to the signed Flask session.
    if session.get("role"):
        return {
            "id": uid,
            "full_name": session.get("full_name") or "",
            "role": session.get("role") or "employee",
            "approved": True,
        }

    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in first.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please sign in first.", "warning")
                return redirect(url_for("login"))

            profile = current_profile()

            if not profile:
                session.clear()
                flash("Your profile could not be loaded.", "error")
                return redirect(url_for("login"))

            if profile.get("role") not in roles:
                flash("You do not have permission to access that page.", "error")
                return redirect(url_for("dashboard"))

            return fn(*args, **kwargs)

        return wrapper
    return decorator
