"""SSO-handoff-validering mot OneMed Dashboard."""
import os
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

SSO_SECRET = os.environ["SSO_SECRET"]
SSO_EXPECTED_AUD = os.environ.get("SSO_EXPECTED_AUD", "legekontor")
SSO_EXPECTED_ISSUER = os.environ.get("SSO_EXPECTED_ISSUER", "onemed-dashboard")
SSO_DASHBOARD_URL = os.environ["SSO_DASHBOARD_URL"]
SSO_SESSION_COOKIE_TTL = int(os.environ.get("SSO_SESSION_COOKIE_TTL", "28800"))
COOKIE_NAME = "sso_session"


@dataclass
class User:
    sub: str
    email: str
    name: str
    role: str


def _validate_redirect_path(path: Optional[str]) -> str:
    if not path or not path.startswith("/"):
        return "/"
    if path.startswith("//") or path.startswith("/\\"):
        return "/"
    return path


def verify_handoff_token(token: str) -> User:
    try:
        payload = jwt.decode(
            token, SSO_SECRET, algorithms=["HS256"],
            audience=SSO_EXPECTED_AUD, issuer=SSO_EXPECTED_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Handoff token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid handoff token")
    return User(sub=payload["sub"], email=payload["email"],
                name=payload["name"], role=payload.get("role", "user"))


def _issue_session_cookie(user: User) -> str:
    now = int(time.time())
    return jwt.encode({
        "sub": user.sub, "email": user.email, "name": user.name, "role": user.role,
        "aud": SSO_EXPECTED_AUD, "iss": SSO_EXPECTED_ISSUER + "-session",
        "iat": now, "exp": now + SSO_SESSION_COOKIE_TTL,
    }, SSO_SECRET, algorithm="HS256")


def sso_callback(token: str, redirect: str) -> Response:
    user = verify_handoff_token(token)
    safe = _validate_redirect_path(redirect)
    resp = RedirectResponse(url=safe, status_code=302)
    resp.set_cookie(COOKIE_NAME, _issue_session_cookie(user),
                    max_age=SSO_SESSION_COOKIE_TTL,
                    httponly=True, secure=True, samesite="lax")
    return resp


def require_sso(request: Request) -> User:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(
            401,
            headers={"Location": f"{SSO_DASHBOARD_URL}/api/sso/launch/{SSO_EXPECTED_AUD}?redirect={request.url.path}"},
        )
    try:
        payload = jwt.decode(
            cookie, SSO_SECRET, algorithms=["HS256"],
            audience=SSO_EXPECTED_AUD, issuer=SSO_EXPECTED_ISSUER + "-session",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Session expired")
    return User(sub=payload["sub"], email=payload["email"],
                name=payload["name"], role=payload.get("role", "user"))


def sso_logout() -> Response:
    resp = RedirectResponse(url=SSO_DASHBOARD_URL, status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp
