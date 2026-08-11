import os
from supabase import create_client


def public_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])


def admin_client():
    # Server-side only. Never expose the service-role key in HTML/JS.
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
