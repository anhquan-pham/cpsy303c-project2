"""
Auth HTTP routes - MERGE these @app.route blocks directly into Keith's
function_app.py (do not import this as a separate module - it needs to
share the same `app = func.FunctionApp(...)` instance and reuse his
_sql_connect helper, which already lives in that file).

Merge steps:
  1. Copy the auth/ folder (security.py, user_store.py, oauth_github.py)
     next to function_app.py.
  2. Add the imports below to the top of function_app.py.
  3. Paste the @app.route functions below into function_app.py (skip the
     `app = func.FunctionApp()` line here - function_app.py already has one).
  4. Every get_user_store(_sql_connect) call below already passes Keith's
     _sql_connect, so auth reads/writes dbo.users through his existing
     managed-identity connection (with its auto-pause retry built in).

New endpoints:
    POST /api/register              email/password signup
    POST /api/login                 email/password login
    GET  /api/auth/github/login     redirect to GitHub
    GET  /api/auth/github/callback  GitHub sends the user back here
    GET  /api/me                    return the logged-in user's name/email (used by requireAuth)

Frontend usage (Adan's app.js):
    - store the JWT (e.g. in a JS variable / sessionStorage) after login
    - send it as `Authorization: Bearer <token>` on every fetchInsights /
      recipes call
    - requireAuth() = call GET /api/me with the stored token; true if 200
"""

# --- add these to function_app.py's existing imports (harmless if already there) ---
import json
import os
import secrets
import logging
import azure.functions as func
from auth.security import hash_password, verify_password, create_session_token, verify_session_token, get_bearer_token
from auth.user_store import get_user_store
from auth import oauth_github
# ------------------------------------------------------------------------------

# NOTE: no `app = func.FunctionApp()` here - function_app.py already defines
# `app` above (with http_auth_level=func.AuthLevel.ANONYMOUS). These routes
# register onto that same instance.

# In-memory CSRF state for the OAuth flow. Fine for a class project with one
# Function instance; for production you'd put this in the DB too.
_pending_oauth_states = set()

# -----------------------------------------------------------------------------
# LOCAL DEV CORS ONLY. Azure Functions Core Tools' Host.CORS setting in
# local.settings.json doesn't reliably answer OPTIONS preflight requests, so
# the browser blocks POST /api/register etc. with "PreflightMissingAllowOrigin".
#
# This only does anything when LOCAL_DEV_CORS_ORIGIN is set - which should ONLY
# ever be in local.settings.json, never in the real Function App's settings.
# In production LOCAL_DEV_CORS_ORIGIN is unset, _cors_headers() returns {}, and
# CORS is handled by Azure's portal-level CORS config as before (no duplicate
# header, matches Keith's existing approach for `insights` etc.).
# -----------------------------------------------------------------------------
LOCAL_DEV_CORS_ORIGIN = os.environ.get("LOCAL_DEV_CORS_ORIGIN")


def _cors_headers() -> dict:
    if not LOCAL_DEV_CORS_ORIGIN:
        return {}
    return {
        "Access-Control-Allow-Origin": LOCAL_DEV_CORS_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    }


@app.route(route="{*path}", methods=["OPTIONS"])
def cors_preflight(req: func.HttpRequest) -> func.HttpResponse:
    """Answers every OPTIONS preflight. Only adds real headers locally (see above)."""
    return func.HttpResponse(status_code=204, headers=_cors_headers())


def _json_response(body: dict, status: int = 200) -> func.HttpResponse:
    # No Access-Control-Allow-Origin header here in production - function_app.py's
    # own comment above `insights` warns that CORS is handled in the Azure
    # portal (Function App > API > CORS), and setting it here too would cause
    # a duplicate header that browsers reject the response for.
    # Locally, _cors_headers() adds it (see LOCAL_DEV_CORS_ORIGIN above);
    # in production it returns {} and this is a no-op.
    return func.HttpResponse(
        json.dumps(body),
        status_code=status,
        mimetype="application/json",
        headers=_cors_headers(),
    )


@app.route(route="register", methods=["POST"])
def register(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Invalid JSON body"}, 400)

    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not name or not email or not password:
        return _json_response({"error": "name, email, and password are required"}, 400)
    if len(password) < 8:
        return _json_response({"error": "Password must be at least 8 characters"}, 400)

    store = get_user_store(_sql_connect)
    if store.get_by_email(email):
        return _json_response({"error": "An account with that email already exists"}, 409)

    user = store.create_user(name=name, email=email, password_hash=hash_password(password))
    token = create_session_token(user["id"], user["name"], user["email"])
    return _json_response({"token": token, "name": user["name"], "email": user["email"]}, 201)


@app.route(route="login", methods=["POST"])
def login(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Invalid JSON body"}, 400)

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    store = get_user_store(_sql_connect)
    user = store.get_by_email(email)

    # Same error for "no such user" and "wrong password" - don't leak which one
    if not user or not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return _json_response({"error": "Invalid email or password"}, 401)

    token = create_session_token(user["id"], user["name"], user["email"])
    return _json_response({"token": token, "name": user["name"], "email": user["email"]})


@app.route(route="auth/github/login", methods=["GET"])
def github_login(req: func.HttpRequest) -> func.HttpResponse:
    state = secrets.token_urlsafe(16)
    _pending_oauth_states.add(state)
    redirect_url = oauth_github.build_authorize_url(state)
    return func.HttpResponse(status_code=302, headers={"Location": redirect_url})


@app.route(route="auth/github/callback", methods=["GET"])
def github_callback(req: func.HttpRequest) -> func.HttpResponse:
    code = req.params.get("code")
    state = req.params.get("state")

    if not state or state not in _pending_oauth_states:
        return _json_response({"error": "Invalid or expired OAuth state"}, 400)
    _pending_oauth_states.discard(state)

    if not code:
        return _json_response({"error": "Missing code from GitHub"}, 400)

    try:
        access_token = oauth_github.exchange_code_for_token(code)
        profile = oauth_github.fetch_github_profile(access_token)
    except Exception as exc:
        logging.exception("GitHub OAuth failed")
        return _json_response({"error": "GitHub login failed"}, 502)

    store = get_user_store(_sql_connect)
    user = store.get_by_oauth("github", profile["oauth_id"])
    if not user:
        # If an email/password account already exists with this email, this
        # simple version creates a separate OAuth account - fine for the
        # class project; call this out in the demo as a known simplification.
        user = store.create_user(
            name=profile["name"],
            email=profile["email"],
            oauth_provider="github",
            oauth_id=profile["oauth_id"],
        )

    token = create_session_token(user["id"], user["name"], user["email"])

    # Configurable so the same code works for local testing (DASHBOARD_URL=
    # http://localhost:5500) and the real deployment (DASHBOARD_URL=the
    # Static Web App URL). Falls back to the deployed URL if unset.
    dashboard_url = os.environ.get(
        # "DASHBOARD_URL", "https://zealous-sea-0a752020f.7.azurestaticapps.net"
        "DASHBOARD_URL", "http://localhost:5500"
    )
    return func.HttpResponse(
        status_code=302,
        headers={"Location": f"{dashboard_url}/#token={token}"},
    )


@app.route(route="me", methods=["GET"])
def me(req: func.HttpRequest) -> func.HttpResponse:
    token = get_bearer_token(dict(req.headers))
    if not token:
        return _json_response({"error": "Missing token"}, 401)

    payload = verify_session_token(token)
    if not payload:
        return _json_response({"error": "Invalid or expired token"}, 401)

    return _json_response({"name": payload["name"], "email": payload["email"]})