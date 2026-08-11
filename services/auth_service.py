from functools import wraps
from flask import session, redirect, url_for, flash
from services.supabase_service import admin_client


def current_profile():
    uid = session.get("user_id")
    if not uid:
        return None
    res = admin_client().table("profiles").select("*").eq("id", uid).limit(1).execute()
    return res.data[0] if res.data else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in first.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            p = current_profile()
            if not p or p.get("role") not in roles:
                flash("You do not have permission to access that page.", "danger")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return deco
