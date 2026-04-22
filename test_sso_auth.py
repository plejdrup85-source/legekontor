import os
import time
os.environ["SSO_SECRET"] = "testsecret_32_bytes_minimum_ok_ok_ok"
os.environ["SSO_EXPECTED_AUD"] = "legekontor"
os.environ["SSO_DASHBOARD_URL"] = "https://dashboard.test"

import jwt
import pytest
from fastapi import HTTPException

import sso_auth

SECRET = os.environ["SSO_SECRET"]


def _mk_token(aud="legekontor", iss="onemed-dashboard", exp_offset=60, secret=SECRET):
    now = int(time.time())
    return jwt.encode({
        "sub": "user-123", "email": "t@test.no", "name": "T Test", "role": "user",
        "aud": aud, "iss": iss, "iat": now, "exp": now + exp_offset,
    }, secret, algorithm="HS256")


def test_valid_token_returns_user():
    user = sso_auth.verify_handoff_token(_mk_token())
    assert user.sub == "user-123"
    assert user.email == "t@test.no"


def test_wrong_audience_rejected():
    with pytest.raises(HTTPException) as e:
        sso_auth.verify_handoff_token(_mk_token(aud="tender"))
    assert e.value.status_code == 401


def test_wrong_issuer_rejected():
    with pytest.raises(HTTPException) as e:
        sso_auth.verify_handoff_token(_mk_token(iss="evil-issuer"))
    assert e.value.status_code == 401


def test_expired_token_rejected():
    with pytest.raises(HTTPException) as e:
        sso_auth.verify_handoff_token(_mk_token(exp_offset=-1))
    assert e.value.status_code == 401


def test_wrong_secret_rejected():
    with pytest.raises(HTTPException) as e:
        sso_auth.verify_handoff_token(_mk_token(secret="wrongsecret_32_bytes_minimum_ok_x"))
    assert e.value.status_code == 401


def test_open_redirect_blocked():
    assert sso_auth._validate_redirect_path("//evil.com") == "/"
    assert sso_auth._validate_redirect_path("https://evil.com") == "/"
    assert sso_auth._validate_redirect_path("/valid/path") == "/valid/path"
    assert sso_auth._validate_redirect_path(None) == "/"
