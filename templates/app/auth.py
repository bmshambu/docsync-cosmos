"""Simple shared-password gate with two roles (PoC).

Two passwords in .env:
  - APP_PASSWORD       → "admin"  role: full access (all four tabs).
  - APP_USER_PASSWORD  → "user"   role: Query + Batch Q&A tabs only; blocked
                          server-side from the Data Prep / Community APIs.

A correct password mints an HMAC-signed, HttpOnly cookie whose payload encodes
the role (stdlib only — no extra dependency). Empty APP_PASSWORD = no gate
(local dev, treated as admin). APP_USER_PASSWORD empty = admin-only.

PoC-grade shared passwords, not per-user identity. For real auth, front the app
with Azure App Service "Easy Auth" (Entra ID / SSO).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "cr_auth"
_OPEN_PREFIXES = ("/login", "/logout", "/static", "/health", "/favicon")
# The "user" role may reach Query (/api/query) + Batch (/api/batch) only; these
# admin-only prefixes are 403'd for it (defence beyond just hiding the tabs).
_ADMIN_ONLY_API_PREFIXES = ("/api/data-prep", "/api/community")

ADMIN = "admin"
USER = "user"


def _secret(password: str) -> bytes:
    # Deterministic key derived from the password → cookies survive restarts
    # without a separately-managed secret, and rotate when the password changes.
    return hashlib.sha256(("cosmos-rag::" + password).encode("utf-8")).digest()


def _password_for_role(role: str, settings) -> str:
    if role == ADMIN:
        return settings.app_password
    if role == USER:
        return settings.app_user_password
    return ""


def make_token(role: str, password: str) -> str:
    # payload = "<role>:<issued_ts>", signed with that role's password secret.
    payload = f"{role}:{int(time.time())}"
    sig = hmac.new(_secret(password), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def decode_token(token: str | None, settings, max_age_s: int) -> str | None:
    """Return the role ('admin'/'user') if the cookie is valid + unexpired, else None."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    role, sep, ts = payload.partition(":")
    if not sep or role not in (ADMIN, USER):
        return None
    pw = _password_for_role(role, settings)
    if not pw:                                    # that role isn't configured
        return None
    expected = hmac.new(_secret(pw), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        issued = int(ts)
    except ValueError:
        return None
    if (time.time() - issued) > max_age_s:
        return None
    return role


def role_for_request(request: Request) -> str:
    """Role for the current request, for the template. 'admin' when the gate is
    disabled (local dev). Defaults to least-privilege 'user' if somehow reached
    without a valid admin cookie (the middleware normally prevents that)."""
    from app.config import get_settings
    settings = get_settings()
    if not settings.app_password:
        return ADMIN
    role = decode_token(request.cookies.get(COOKIE), settings, settings.app_session_hours * 3600)
    return role or USER


_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · RFP GraphRAG</title>
<style>
 body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
   background:#0e161d;color:#e4ecf1;font-family:"Segoe UI",system-ui,sans-serif}}
 .card{{background:#15202a;border:1px solid #263542;border-radius:14px;padding:32px 30px;width:320px}}
 h1{{font-size:18px;margin:0 0 4px}} p{{color:#8fa3b0;font-size:13px;margin:0 0 20px}}
 input{{width:100%;box-sizing:border-box;background:#0e161d;border:1px solid #263542;
   border-radius:8px;padding:10px 12px;color:#e4ecf1;font-size:14px;margin-bottom:12px}}
 input:focus{{outline:none;border-color:#35b3c7}}
 button{{width:100%;background:#35b3c7;color:#06203f;border:0;border-radius:8px;
   padding:10px;font-size:14px;font-weight:600;cursor:pointer}}
 .err{{color:#ce7961;font-size:12.5px;margin-bottom:12px}}
</style></head><body>
<form class="card" method="post" action="/login">
 <h1>RFP GraphRAG</h1><p>Enter your access password to continue.</p>
 {err}
 <input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
 <input type="hidden" name="next" value="{next}">
 <button type="submit">Sign in</button>
</form></body></html>"""


def install_auth(app) -> None:
    """Wire the password gate onto the FastAPI app (no-op if APP_PASSWORD unset)."""
    from app.config import get_settings

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        settings = get_settings()
        if not settings.app_password:                # gate disabled (dev)
            return await call_next(request)
        path = request.url.path
        if path.startswith(_OPEN_PREFIXES):
            return await call_next(request)

        role = decode_token(request.cookies.get(COOKIE), settings,
                            settings.app_session_hours * 3600)
        if not role:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=302)

        # Role-based API restriction: the user role can't reach admin-only APIs
        # even by hitting the URL directly (the tabs are hidden too).
        if role == USER and path.startswith(_ADMIN_ONLY_API_PREFIXES):
            return JSONResponse(
                {"detail": "Forbidden — this action is admin-only."}, status_code=403)

        return await call_next(request)

    @app.get("/login")
    async def login_form(next: str = "/"):
        return HTMLResponse(_LOGIN_HTML.format(err="", next=next))

    @app.post("/login")
    async def login_submit(request: Request):
        settings = get_settings()
        form = await request.form()
        password = (form.get("password") or "").strip()
        nxt = form.get("next") or "/"

        role = None
        if settings.app_password and hmac.compare_digest(password, settings.app_password):
            role = ADMIN                              # admin wins if both match
        elif settings.app_user_password and hmac.compare_digest(password, settings.app_user_password):
            role = USER

        if role:
            resp = RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=302)
            resp.set_cookie(
                COOKIE, make_token(role, _password_for_role(role, settings)),
                max_age=settings.app_session_hours * 3600,
                httponly=True, samesite="lax",
                secure=request.url.scheme == "https",
            )
            return resp
        err = '<div class="err">Incorrect password.</div>'
        return HTMLResponse(_LOGIN_HTML.format(err=err, next=nxt), status_code=401)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(COOKIE)
        return resp
