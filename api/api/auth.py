"""Google Sign-In (web OAuth code flow) + cookie sessions.

Env: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (falls back to GMAIL_CLIENT_ID /
GMAIL_CLIENT_SECRET). Prod needs a "Web application" OAuth client with
<base>/auth/google/callback registered as a redirect URI.

Trust model: the id_token arrives directly from Google's token endpoint over
TLS in a server-side exchange, so its payload is read without signature
verification (standard for the code flow). Sessions: random 256-bit token in an
HttpOnly cookie; only sha256(token) is stored (common.repo). Session lookup and
user upsert run on the admin plane — the only queries that do.
"""

from __future__ import annotations

import base64
import json
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from api import db
from common import repo

SESSION_COOKIE = "__session"
_STATE_COOKIE = "__oauth_state"
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"

router = APIRouter()


def _client_creds() -> tuple[str, str]:
    cid = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GMAIL_CLIENT_ID")
    secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("GMAIL_CLIENT_SECRET")
    if not cid or not secret:
        raise HTTPException(500, "Google OAuth not configured (GOOGLE_CLIENT_ID/SECRET)")
    return cid, secret


def _cookie_secure() -> bool:
    # Secure cookies break plain-http dev; default on, opt out explicitly.
    return os.getenv("API_COOKIE_SECURE", "1") not in ("0", "false", "no")


def _redirect_uri(request: Request) -> str:
    base = os.getenv("API_BASE_URL") or str(request.base_url).rstrip("/")
    return f"{base}/auth/google/callback"


def _decode_id_token(id_token: str) -> dict:
    """Payload of a JWT from Google's token endpoint (transport-trusted, unverified)."""
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


async def _exchange_code(code: str, redirect_uri: str) -> dict:
    """code -> id_token claims (sub/email/name). Split out so tests can stub it."""
    cid, secret = _client_creds()
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            _GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Google token exchange failed: {resp.text[:200]}")
    return _decode_id_token(resp.json()["id_token"])


@router.get("/auth/google/login")
async def google_login(request: Request) -> RedirectResponse:
    cid, _ = _client_creds()
    state = secrets.token_urlsafe(16)
    params = httpx.QueryParams(
        client_id=cid,
        redirect_uri=_redirect_uri(request),
        response_type="code",
        scope="openid email profile",
        state=state,
    )
    resp = RedirectResponse(f"{_GOOGLE_AUTH}?{params}", status_code=302)
    resp.set_cookie(
        _STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax", secure=_cookie_secure(),
    )
    return resp


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str, state: str) -> RedirectResponse:
    if not secrets.compare_digest(state, request.cookies.get(_STATE_COOKIE, "")):
        raise HTTPException(400, "OAuth state mismatch")
    claims = await _exchange_code(code, _redirect_uri(request))
    if not claims.get("sub") or not claims.get("email"):
        raise HTTPException(502, "Google id_token missing sub/email")

    with db.admin_conn() as conn:
        user = repo.upsert_google_user(
            conn, claims["sub"], claims["email"], claims.get("name")
        )
        token = repo.create_session(conn, user["user_id"])
        repo.audit(conn, user["user_id"], "auth.login", "user", str(user["user_id"]))

    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(_STATE_COOKIE)
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(repo.SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax", secure=_cookie_secure(),
    )
    return resp


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.admin_conn() as conn:
            repo.revoke_session(conn, token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: the session's user row, 401 without a live session."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not signed in")
    with db.admin_conn() as conn:
        user = repo.get_session(conn, token)
    if user is None:
        raise HTTPException(401, "session expired or revoked")
    request.state.user_id = str(user["user_id"])  # for the audit middleware
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    return user
