"""SSO-handoff-validering mot OneMed Dashboard."""
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import jwt
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

SSO_SECRET = os.environ["SSO_SECRET"]
if len(SSO_SECRET) < 32 and os.environ.get("SSO_ALLOW_WEAK_SECRET", "0") != "1":
    raise RuntimeError(
        "SSO_SECRET er for kort (< 32 tegn). HS256 krever en sterk nøkkel. "
        "Sett en lengre hemmelighet, eller SSO_ALLOW_WEAK_SECRET=1 for å overstyre bevisst."
    )
SSO_EXPECTED_AUD = os.environ.get("SSO_EXPECTED_AUD", "legekontor")
SSO_EXPECTED_ISSUER = os.environ.get("SSO_EXPECTED_ISSUER", "onemed-dashboard")
SSO_DASHBOARD_URL = os.environ["SSO_DASHBOARD_URL"]
SSO_SESSION_COOKIE_TTL = int(os.environ.get("SSO_SESSION_COOKIE_TTL", "28800"))
COOKIE_NAME = "sso_session"
_ROLE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")


def _normalize_role(role: object) -> str:
    """Return a canonical role value, or an empty value for malformed claims."""
    if not isinstance(role, str):
        return ""
    normalized = role.strip()
    if not normalized.isascii() or _ROLE_PATTERN.fullmatch(normalized) is None:
        return ""
    return normalized.lower()


# Roller som regnes som administratorer (kan endre delte ressurser, f.eks. katalog).
ADMIN_ROLES = {
    normalized
    for role in os.environ.get("ADMIN_ROLES", "admin,superadmin").split(",")
    if (normalized := _normalize_role(role))
}

# ------------------------------------------------------------------
# Sesjonstilbakekalling (denylist)
# ------------------------------------------------------------------
# Stateless JWT kan ikke tilbakekalles i seg selv. Vi holder en liten denylist
# over 'jti' fra utloggede tokens, persistert til disk, med opprydding av
# utløpte oppføringer. Tokens utløper uansett etter SSO_SESSION_COOKIE_TTL.
_DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
_DENYLIST_PATH = _DATA_DIR / "session_denylist.json"
_denylist: dict = {}


def _load_denylist() -> None:
    global _denylist
    try:
        if _DENYLIST_PATH.exists():
            data = json.loads(_DENYLIST_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                now = int(time.time())
                _denylist = {str(k): int(v) for k, v in data.items() if int(v) > now}
    except Exception as e:
        logger.warning(f"Kunne ikke lese session-denylist: {e}")


def _save_denylist() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _DENYLIST_PATH.write_text(json.dumps(_denylist), "utf-8")
    except Exception as e:
        logger.warning(f"Kunne ikke skrive session-denylist: {e}")


def _revoke(jti: str, exp: int) -> None:
    if not jti:
        return
    now = int(time.time())
    _denylist[jti] = int(exp)
    for k in [k for k, v in list(_denylist.items()) if v <= now]:
        _denylist.pop(k, None)
    _save_denylist()


_load_denylist()


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
        "iat": now, "exp": now + SSO_SESSION_COOKIE_TTL, "jti": uuid.uuid4().hex,
    }, SSO_SECRET, algorithm="HS256")


def sso_callback(token: str, redirect: str) -> Response:
    user = verify_handoff_token(token)
    safe = _validate_redirect_path(redirect)
    resp = RedirectResponse(url=safe, status_code=302)
    resp.set_cookie(COOKIE_NAME, _issue_session_cookie(user),
                    max_age=SSO_SESSION_COOKIE_TTL,
                    httponly=True, secure=True, samesite="lax")
    logger.info(f"SSO innlogging: sub={user.sub} role={user.role}")
    return resp


def require_sso(request: Request) -> User:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        logger.warning(f"Uautentisert forespørsel avvist: {request.method} {request.url.path}")
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
        logger.warning(f"Ugyldig/utløpt sesjon avvist: {request.method} {request.url.path}")
        raise HTTPException(401, "Session expired")
    jti = payload.get("jti")
    if jti and jti in _denylist:
        logger.warning(f"Tilbakekalt sesjon forsøkt brukt (jti={jti}) på {request.url.path}")
        raise HTTPException(401, "Session revoked")
    return User(sub=payload["sub"], email=payload["email"],
                name=payload["name"], role=payload.get("role", "user"))


def require_role(*allowed_roles: str) -> Callable[..., User]:
    """Dependency-fabrikk: krever at innlogget bruker har en av rollene.

    Uten argumenter kreves en av ADMIN_ROLES.
    """
    configured_roles = allowed_roles if allowed_roles else tuple(ADMIN_ROLES)
    allowed = {
        normalized
        for role in configured_roles
        if (normalized := _normalize_role(role))
    }

    def _dep(user: User = Depends(require_sso)) -> User:
        if _normalize_role(user.role) not in allowed:
            logger.warning(
                f"Autorisasjon avvist: sub={user.sub} role={user.role} krever en av {sorted(allowed)}"
            )
            raise HTTPException(403, "Krever forhøyede rettigheter")
        return user

    return _dep


def is_admin(user: User) -> bool:
    return _normalize_role(user.role) in ADMIN_ROLES


def sso_logout(request: Optional[Request] = None) -> Response:
    # Tilbakekall sesjonen server-side (denylist) i tillegg til å slette cookien.
    if request is not None:
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie:
            try:
                payload = jwt.decode(
                    cookie, SSO_SECRET, algorithms=["HS256"],
                    audience=SSO_EXPECTED_AUD, issuer=SSO_EXPECTED_ISSUER + "-session",
                    options={"verify_exp": False},
                )
                _revoke(payload.get("jti", ""), int(payload.get("exp", int(time.time()))))
                logger.info(f"SSO logout: sub={payload.get('sub')}")
            except Exception:
                pass
    resp = RedirectResponse(url=SSO_DASHBOARD_URL, status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp
