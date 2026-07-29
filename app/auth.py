"""Simple shared-password gate (PoC).

Set APP_PASSWORD in .env to require login. A correct password mints an
HMAC-signed, HttpOnly session cookie (stdlib only — no extra dependency); a
middleware rejects unauthenticated requests. Empty APP_PASSWORD = no gate
(local dev).

This is a PoC-grade single shared password, not per-user identity. For real
auth, front the app with Azure App Service "Easy Auth" (Entra ID / SSO).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "cr_auth"
_OPEN_PREFIXES = ("/login", "/logout", "/static", "/health", "/favicon")


def _secret(password: str) -> bytes:
    # Deterministic key derived from the password → cookies survive restarts
    # without a separately-managed secret, and rotate when the password changes.
    return hashlib.sha256(("cosmos-rag::" + password).encode("utf-8")).digest()


def _sign(payload: str, password: str) -> str:
    sig = hmac.new(_secret(password), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def make_token(password: str) -> str:
    return _sign(str(int(time.time())), password)


def valid_token(token: str | None, password: str, max_age_s: int) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(_secret(password), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        issued = int(payload)
    except ValueError:
        return False
    return (time.time() - issued) <= max_age_s


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
 <h1>RFP GraphRAG</h1><p>Enter the access password to continue.</p>
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
        pw = settings.app_password
        if not pw:                                   # gate disabled
            return await call_next(request)
        path = request.url.path
        if path.startswith(_OPEN_PREFIXES):
            return await call_next(request)
        token = request.cookies.get(COOKIE)
        if valid_token(token, pw, settings.app_session_hours * 3600):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)

    @app.get("/login")
    async def login_form(next: str = "/"):
        return HTMLResponse(_LOGIN_HTML.format(err="", next=next))

    @app.post("/login")
    async def login_submit(request: Request):
        settings = get_settings()
        form = await request.form()
        password = (form.get("password") or "").strip()
        nxt = form.get("next") or "/"
        if settings.app_password and hmac.compare_digest(password, settings.app_password):
            resp = RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=302)
            resp.set_cookie(
                COOKIE, make_token(settings.app_password),
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
