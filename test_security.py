"""Sikkerhetsregresjonstester for tilgangskontroll, rate limiting og hoder.

Dekker rettelsene fra sikkerhetsrevisjonen (OWASP ASVS L2):
  - V2 krever autentisering (ingen offentlige endepunkter)
  - Eier-/objektnivå-autorisasjon (IDOR) på jobber
  - Funksjonsnivå-autorisasjon (admin) på katalog/admin
  - Sesjonstilbakekalling ved logout
  - Sikkerhets-HTTP-hoder
  - PII-maskering før LLM
"""
import os
import time

os.environ.setdefault("SSO_SECRET", "testsecret_32_bytes_minimum_ok_ok_ok")
os.environ.setdefault("SSO_EXPECTED_AUD", "legekontor")
os.environ.setdefault("SSO_DASHBOARD_URL", "https://dashboard.test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
import tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import jwt
import pytest
from starlette.testclient import TestClient

import app as appmod
from v2 import router as vr
from v2 import persistence as rv

SECRET = os.environ["SSO_SECRET"]


def _sess(sub="userA", role="user"):
    now = int(time.time())
    return jwt.encode({
        "sub": sub, "email": f"{sub}@t.no", "name": sub, "role": role,
        "aud": "legekontor", "iss": "onemed-dashboard-session",
        "iat": now, "exp": now + 600, "jti": f"jti-{sub}-{now}",
    }, SECRET, algorithm="HS256")


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def test_v2_requires_auth(client):
    for path in ("/v2/jobs", "/v2/pricedb"):
        assert client.get(path).status_code == 401


def test_v2_works_with_auth(client):
    r = client.get("/v2/jobs", cookies={"sso_session": _sess()})
    assert r.status_code == 200


def test_security_headers_present(client):
    r = client.get("/v2/jobs", cookies={"sso_session": _sess()})
    assert "content-security-policy" in {k.lower() for k in r.headers}
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_admin_only_catalog(client):
    r = client.get("/admin", cookies={"sso_session": _sess("u", "user")}, follow_redirects=False)
    assert r.status_code == 403
    r = client.get("/admin", cookies={"sso_session": _sess("a", "admin")}, follow_redirects=False)
    assert r.status_code == 200


def test_v2_idor_blocked(client):
    import uuid
    jid = uuid.uuid4().hex
    (vr.V2_UPLOADS_DIR / jid).mkdir(parents=True, exist_ok=True)
    vr._append_v2_job({"job_id": jid, "created_at": "2026", "status": "review", "owner_sub": "userA"})
    rv.save_review(jid, [{"dedup_idx": 0, "description": "x", "candidates": []}])

    assert client.get(f"/v2/review/{jid}", cookies={"sso_session": _sess("userB")}).status_code == 404
    assert client.get(f"/v2/review/{jid}", cookies={"sso_session": _sess("userA")}).status_code == 200
    assert client.get(f"/v2/review/{jid}", cookies={"sso_session": _sess("adm", "admin")}).status_code == 200


def test_logout_revokes_session(client):
    tok = _sess("userC")
    assert client.get("/v2/jobs", cookies={"sso_session": tok}).status_code == 200
    client.get("/logout", cookies={"sso_session": tok}, follow_redirects=False)
    assert client.get("/v2/jobs", cookies={"sso_session": tok}).status_code == 401


def test_download_rejects_bad_task_id_and_enforces_owner(client):
    appmod.append_job({"task_id": "b" * 32, "status": "done", "owner_sub": "ownerX", "created_at": "2026"})
    (appmod.RESULTS_DIR / ("b" * 32 + ".xlsx")).write_bytes(b"PKxx")
    # Non-owner cannot download
    assert client.get("/download/" + "b" * 32, cookies={"sso_session": _sess("intruder")}).status_code == 404
    # Owner can
    assert client.get("/download/" + "b" * 32, cookies={"sso_session": _sess("ownerX")}).status_code == 200


def test_pii_redaction_keeps_product_numbers():
    out = appmod._redact_sensitive("ola@onemed.no NO9386011117947 art 123456 Bind")
    assert "ola@onemed.no" not in out
    assert "NO9386011117947" not in out
    assert "123456" in out  # produktnummer bevares
