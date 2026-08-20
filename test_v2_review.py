import os
import tempfile
import time
import uuid
import multiprocessing
import threading
from io import BytesIO
from pathlib import Path

import jwt
import pandas as pd
import pytest
from starlette.testclient import TestClient

os.environ.setdefault("SSO_SECRET", "testsecret_32_bytes_minimum_ok_ok_ok")
os.environ.setdefault("SSO_EXPECTED_AUD", "legekontor")
os.environ.setdefault("SSO_DASHBOARD_URL", "https://dashboard.test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import app as appmod
from v2 import persistence as rv
from v2 import router as vr
from v2.catalog_rules import catalog_product_metadata
from v2.export import build_export_rows


class _ReviewClient(TestClient):
    def post(self, url, *args, **kwargs):
        parts = str(url).split("?")[0].strip("/").split("/")
        if len(parts) >= 4 and parts[:2] == ["v2", "review"]:
            job_id = parts[2]
            action = parts[3]
            if action in {"select", "batch-select", "decide", "undo", "bulk-decide", "replace-candidate", "extras", "delete", "restore", "add-row"}:
                revision = rv.get_review_revision(job_id)
                headers = dict(kwargs.pop("headers", {}) or {})
                headers.setdefault("If-Match", str(revision))
                kwargs["headers"] = headers
                if action == "decide" and isinstance(kwargs.get("json"), dict):
                    body = dict(kwargs["json"])
                    body.setdefault("revision", revision)
                    idx = body.get("dedup_idx")
                    rows = rv.apply_overrides(rv.load_review(job_id) or [], job_id)
                    row = next((item for item in rows if item.get("dedup_idx") == idx), None)
                    candidate = None
                    if row:
                        candidate_idx = row.get("selected_candidate_idx")
                        if candidate_idx is None:
                            candidate_idx = row.get("suggested_candidate_idx", row.get("best_candidate_idx"))
                        candidate = next((item for item in row.get("candidates", []) if item.get("candidate_idx") == candidate_idx), None)
                    body.setdefault("candidate_identity", vr._candidate_identity(candidate))
                    kwargs["json"] = body
        return super().post(url, *args, **kwargs)


def _session(sub="reviewer", role="user"):
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "email": f"{sub}@test.no",
            "name": sub,
            "role": role,
            "aud": "legekontor",
            "iss": "onemed-dashboard-session",
            "iat": now,
            "exp": now + 600,
            "jti": f"review-{sub}-{now}",
        },
        os.environ["SSO_SECRET"],
        algorithm="HS256",
    )


@pytest.fixture()
def review_env(tmp_path, monkeypatch):
    reviews = tmp_path / "reviews"
    uploads = tmp_path / "uploads"
    reviews.mkdir()
    uploads.mkdir()
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", reviews)
    monkeypatch.setattr(vr, "V2_UPLOADS_DIR", uploads)
    monkeypatch.setattr(vr, "V2_JOBS_INDEX", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(vr, "_LEARNING_FILE", tmp_path / "learnings.jsonl")
    monkeypatch.setattr(
        vr,
        "_get_catalog_bundle",
        lambda: _Bundle(
            [_CatalogItem(str(artnr), "Sellable", 2.5) for artnr in range(10000, 10005)]
        ),
    )
    vr.V2_TASKS.clear()
    return _ReviewClient(appmod.app)


def _candidate(artnr="10001", *, eligible=True):
    return {
        "candidate_idx": 0,
        "our_artnr": artnr,
        "our_description": "Produkt",
        "our_unit_price": 10.0,
        "item_status": "Sellable" if eligible else "Obsolete",
        "alc": 2.5 if eligible else 0.0,
        "eligible": eligible,
        "masterdata_error": "" if eligible else "Produktet er ikke salgbart",
    }


def _create_job(rows):
    job_id = uuid.uuid4().hex
    (vr.V2_UPLOADS_DIR / job_id).mkdir(parents=True)
    vr._append_v2_job(
        {
            "job_id": job_id,
            "created_at": "2026",
            "status": "review",
            "owner_sub": "owner",
        }
    )
    rv.save_review(job_id, rows)
    return job_id


def test_review_init_keeps_suggestion_separate_from_user_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    rows = rv.init_review(
        "job",
        [{"dedup_idx": 0, "best_candidate_idx": 0, "candidates": [_candidate()]}],
    )

    assert rows[0]["suggested_candidate_idx"] == 0
    assert rows[0]["selected_candidate_idx"] is None
    assert rows[0]["candidate_status"] == "suggested"
    assert rows[0]["review_status"] == "pending"

    rv.save_review("job", rows)
    rv.save_selection("job", 0, 0)
    applied = rv.apply_overrides(rows, "job")

    assert applied[0]["selected_candidate_idx"] == 0
    assert applied[0]["candidate_status"] == "selected"


def test_legacy_rejected_decision_is_read_as_not_same(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    rows = [{"dedup_idx": 0, "review_status": "rejected", "candidates": []}]
    rv.save_decisions("job", {"0": {"status": "rejected"}})

    assert rv.apply_overrides(rows, "job")[0]["review_status"] == "not_same"


@pytest.mark.parametrize(
    ("status", "alc", "eligible"),
    [
        (" Sellable ", "2,50", True),
        ("Obsolete", 2.5, False),
        ("Sellable", 0, False),
        ("Sellable", "", False),
    ],
)
def test_catalog_eligibility_requires_sellable_and_positive_alc(status, alc, eligible):
    metadata = catalog_product_metadata({"Item Status": status, "ALC": alc})

    assert metadata["eligible"] is eligible
    assert bool(metadata["masterdata_error"]) is (not eligible)


def test_individual_approval_requires_ack_for_critical_row(review_env):
    row = {
        "dedup_idx": 0,
        "description": "Vare",
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "selected_candidate_idx": None,
        "candidates": [_candidate()],
        "merge_warning": True,
    }
    job_id = _create_job([row])
    cookies = {"sso_session": _session()}

    blocked = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    approved = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={
            "dedup_idx": 0,
            "status": "approved",
            "acknowledge_critical": True,
        },
        cookies=cookies,
    )

    assert blocked.status_code == 409
    assert approved.status_code == 200
    assert approved.json()["previous_status"] == "pending"
    assert rv.load_selections(job_id)["0"]["candidate_status"] == "selected"


def test_approval_rejects_historical_ineligible_candidate(review_env, monkeypatch):
    row = {
        "dedup_idx": 0,
        "description": "Vare",
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate(eligible=False)],
    }
    job_id = _create_job([row])
    monkeypatch.setattr(
        vr,
        "_get_catalog_bundle",
        lambda: _Bundle([_CatalogItem("10001", "Obsolete", 0)]),
    )

    response = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 409
    review = review_env.get(
        f"/v2/review/{job_id}", cookies={"sso_session": _session()}
    ).json()
    assert review["rows"][0]["candidates"][0]["masterdata_error"]


def test_selecting_new_candidate_resets_approved_decision(review_env):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "suggested_candidate_idx": 0,
        "selected_candidate_idx": 0,
        "candidate_status": "selected",
        "candidates": candidates,
    }
    job_id = _create_job([row])
    rv.save_selection(job_id, 0, 0)
    rv.save_decision(job_id, 0, "approved")

    response = review_env.post(
        f"/v2/review/{job_id}/select",
        json={"dedup_idx": 0, "candidate_idx": 1},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 200
    assert response.json()["previous_status"] == "approved"
    assert rv.apply_overrides([row], job_id)[0]["review_status"] == "pending"


class _CatalogItem:
    def __init__(self, artnr, status, alc, description=None):
        self.artnr = artnr
        self.text = "testprodukt"
        self.row = {
            "Katalog: Artikkelnummer": artnr,
            "Katalog: Item Description": description or f"Produkt {artnr}",
            "Item Status": status,
            "ALC": alc,
        }


class _Bm25:
    def __init__(self, docs):
        self.docs = docs

    def top_n(self, query, n=10):
        return [(item, 1.0) for item in self.docs[:n]]


class _Catalog:
    def __init__(self, items):
        self.items = items
        self.bm25_index = _Bm25(items)


class _Bundle:
    def __init__(self, items, prices=None):
        self.lk = _Catalog(items)
        self.full = _Catalog([])
        self.prices = prices or {}

    def price_for_artnr(self, artnr, source=None):
        return self.prices.get((source, str(artnr)), 10.0), source or "test"


def test_catalog_search_returns_only_eligible_products_sorted_by_alc(
    review_env, monkeypatch
):
    job_id = _create_job([{"dedup_idx": 0, "review_status": "pending"}])
    bundle = _Bundle(
        [
            _CatalogItem("10003", "Obsolete", 1),
            _CatalogItem("10002", "Sellable", 4),
            _CatalogItem("10001", "Sellable", 2),
            _CatalogItem("10000", "Sellable", 0),
        ]
    )
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: bundle)

    response = review_env.post(
        f"/v2/review/{job_id}/search-catalog",
        json={"query": "test", "limit": 10},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 200
    assert [item["our_artnr"] for item in response.json()["results"]] == [
        "10001",
        "10002",
    ]


def test_replace_candidate_rejects_obsolete_product(review_env, monkeypatch):
    job_id = _create_job([{"dedup_idx": 0, "review_status": "pending", "candidates": []}])
    monkeypatch.setattr(
        vr,
        "_get_catalog_bundle",
        lambda: _Bundle([_CatalogItem("10003", "Obsolete", 1)]),
    )

    response = review_env.post(
        f"/v2/review/{job_id}/replace-candidate",
        json={"dedup_idx": 0, "our_artnr": "10003"},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 409


def test_bulk_approval_is_atomic_and_uses_only_explicit_ids(review_env):
    rows = [
        {
            "dedup_idx": 0,
            "review_status": "pending",
            "suggested_candidate_idx": 0,
            "candidates": [_candidate("10000")],
            "merge_warning": False,
        },
        {
            "dedup_idx": 1,
            "review_status": "pending",
            "suggested_candidate_idx": 0,
            "candidates": [_candidate("10001")],
            "merge_warning": True,
        },
        {
            "dedup_idx": 2,
            "review_status": "pending",
            "suggested_candidate_idx": 0,
            "candidates": [_candidate("10002")],
            "merge_warning": False,
        },
    ]
    job_id = _create_job(rows)
    cookies = {"sso_session": _session()}

    blocked = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0, 1], "status": "approved"},
        cookies=cookies,
    )
    assert blocked.status_code == 409
    assert rv.load_decisions(job_id) == {}

    success = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0], "status": "approved"},
        cookies=cookies,
    )
    assert success.status_code == 200
    decisions = rv.load_decisions(job_id)
    assert set(decisions) == {"0"}


def test_bulk_approval_binds_suggested_candidate_not_manual_selection(review_env):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    row = {
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": candidates,
    }
    job_id = _create_job([row])
    rv.save_selection(job_id, 0, 1)

    response = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0], "status": "approved"},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 200
    assert rv.load_selections(job_id)["0"]["candidate_idx"] == 0


def test_bulk_decision_never_creates_learning_signal(review_env):
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate()],
    }])

    response = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0], "status": "approved"},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 200
    assert vr.load_learnings() == []


def test_undo_restores_decision_selection_and_removes_approval_learning(review_env):
    row = {
        "dedup_idx": 0,
        "description": "Vare",
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate()],
    }
    job_id = _create_job([row])
    cookies = {"sso_session": _session()}

    approved = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    assert approved.status_code == 200
    undo_token = approved.json()["undo_token"]
    assert rv.load_decisions(job_id)["0"]["status"] == "approved"
    assert "0" in rv.load_selections(job_id)
    assert [entry["type"] for entry in vr.load_learnings()] == ["approval"]

    undone = review_env.post(
        f"/v2/review/{job_id}/undo",
        json={"undo_token": undo_token},
        cookies=cookies,
    )

    assert undone.status_code == 200
    assert "0" not in rv.load_decisions(job_id)
    assert "0" not in rv.load_selections(job_id)
    assert vr.load_learnings() == []


def test_legacy_approved_row_rehydrates_explicit_selected_candidate(review_env):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 1,
        "best_candidate_idx": 0,
        "candidates": candidates,
        "total_units": 2,
    }
    job_id = _create_job([row])

    response = review_env.get(
        f"/v2/review/{job_id}", cookies={"sso_session": _session()}
    )

    assert response.status_code == 200
    hydrated = response.json()["rows"][0]
    assert hydrated["selected_candidate_idx"] == 1
    assert rv.load_selections(job_id)["0"]["candidate_idx"] == 1
    assert hydrated["review_status"] == "approved"


def test_legacy_rehydrate_retries_after_concurrent_cas_selection(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    legacy_row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "candidates": [_candidate("10000")],
    }
    rv.save_review("job", [legacy_row])
    initial_revision = rv.get_review_revision("job")
    migration_read = threading.Event()
    continue_migration = threading.Event()
    real_load = rv._load_workspace_unlocked

    def paused_load(job_id):
        workspace = real_load(job_id)
        if threading.current_thread().name == "legacy-migrator" and not migration_read.is_set():
            migration_read.set()
            assert continue_migration.wait(timeout=5)
        return workspace

    monkeypatch.setattr(rv, "_load_workspace_unlocked", paused_load)
    migration = threading.Thread(
        name="legacy-migrator",
        target=rv.rehydrate_legacy_approved_selections,
        args=("job", [legacy_row]),
    )
    migration.start()
    assert migration_read.wait(timeout=5)

    def concurrent_writer():
        expected_revision = initial_revision
        while True:
            state = rv.load_review_state("job")
            selections = dict(state["selections"])
            selections["1"] = {"candidate_idx": 1}
            try:
                rv.save_review_state(
                    "job",
                    selections=selections,
                    decisions=state["decisions"],
                    expected_revision=expected_revision,
                )
                return
            except rv.StaleRevisionError:
                expected_revision = rv.get_review_revision("job")

    writer = threading.Thread(target=concurrent_writer)
    writer.start()
    continue_migration.set()
    migration.join(timeout=5)
    writer.join(timeout=5)
    assert not migration.is_alive()
    assert not writer.is_alive()

    selections = rv.load_selections("job")
    assert selections["0"]["candidate_idx"] == 0
    assert selections["1"]["candidate_idx"] == 1


def test_legacy_rehydrate_never_silently_abandons_approved_selection_after_five_stales(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    legacy_row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "candidates": [_candidate("10000")],
    }
    rv.save_review("job", [legacy_row])
    attempts = {"count": 0}

    def always_stale(*_args, **_kwargs):
        attempts["count"] += 1
        raise rv.StaleRevisionError("kontinuerlig konflikt")

    monkeypatch.setattr(rv, "save_review_state", always_stale)
    rv.rehydrate_legacy_approved_selections("job", [legacy_row])

    projected = rv.apply_overrides(rv.load_review("job"), "job")[0]
    assert projected["review_status"] == "approved"
    assert projected["selected_candidate_idx"] == 0
    assert "0" in rv.load_selections("job")


def test_catalog_source_identity_resolves_conflicting_same_artnr(review_env, monkeypatch):
    lk_item = _CatalogItem("10001", "Obsolete", 0)
    full_item = _CatalogItem("10001", "Sellable", 7)
    bundle = _Bundle([lk_item])
    bundle.full = _Catalog([full_item])
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: bundle)
    row = {
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [{**_candidate("10001"), "matched_from": "full"}],
    }
    job_id = _create_job([row])
    cookies = {"sso_session": _session()}

    review = review_env.get(f"/v2/review/{job_id}", cookies=cookies)
    search = review_env.post(
        f"/v2/review/{job_id}/search-catalog",
        json={"query": "10001"},
        cookies=cookies,
    )
    replace = review_env.post(
        f"/v2/review/{job_id}/replace-candidate",
        json={"dedup_idx": 0, "our_artnr": "10001", "matched_from": "full"},
        cookies=cookies,
    )

    assert review.json()["rows"][0]["candidates"][0]["eligible"] is True
    assert search.json()["results"][0]["matched_from"] == "full"
    assert search.json()["results"][0]["alc"] == 7
    assert replace.status_code == 200


def test_current_source_catalog_projects_price_and_product_fields_everywhere(
    review_env, monkeypatch, tmp_path
):
    bundle = _Bundle(
        [_CatalogItem("10001", "Sellable", 4, "LK nå")],
        prices={("lk", "10001"): 55.0, ("full", "10001"): 99.0},
    )
    bundle.full = _Catalog([
        _CatalogItem("10001", "Sellable", 7, "Full nå"),
    ])
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: bundle)
    monkeypatch.setattr(vr.pricedb, "V2_PRICEDB_PATH", tmp_path / "pricedb.jsonl")
    row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "suggested_candidate_idx": 0,
        "total_units": 2,
        "competitor_line_amount": 250,
        "candidates": [{
            **_candidate("10001"),
            "matched_from": "full",
            "our_description": "Gammelt snapshot",
            "our_unit_price": 10.0,
        }],
    }
    job_id = _create_job([row])
    rv.save_selection(job_id, 0, 0)
    rv.save_decision(job_id, 0, "approved")

    review = review_env.get(
        f"/v2/review/{job_id}", cookies={"sso_session": _session()}
    )
    assert review.status_code == 200
    projected = review.json()["rows"][0]
    assert projected["candidates"][0]["our_description"] == "Full nå"
    assert projected["candidates"][0]["our_unit_price"] == 99.0
    assert projected["our_unit_price"] == 99.0
    assert projected["our_comparable_line_price"] == 198.0

    exported = build_export_rows([projected])[0]
    assert exported["Vår pris/enhet"] == 99.0
    committed = vr.pricedb.commit_job(job_id, [projected])
    assert committed == 1
    assert vr.pricedb.load_all()[0]["our_unit_price"] == 99.0


def test_get_export_and_pricedb_project_one_atomic_workspace_snapshot(
    review_env, monkeypatch, tmp_path
):
    candidate_0 = _candidate("10000")
    candidate_1 = {**_candidate("10001"), "candidate_idx": 1}
    row_a = {
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [candidate_0],
    }
    row_b = {
        **row_a,
        "candidates": [candidate_0, candidate_1],
    }
    identity_1 = vr._candidate_identity(candidate_1)
    workspace_a = {
        "review": [row_a],
        "extras": {},
        "deletions": {},
        "state": {
            "selections": {},
            "decisions": {},
            "undo_tokens": {},
            "revoked_learning_tokens": [],
            "revision": 1,
        },
    }
    workspace_b = {
        "review": [row_b],
        "extras": {},
        "deletions": {},
        "state": {
            "selections": {"0": {"candidate_idx": 1, "candidate_identity": identity_1}},
            "decisions": {"0": {"status": "approved", "candidate_identity": identity_1}},
            "undo_tokens": {},
            "revoked_learning_tokens": [],
            "revision": 2,
        },
    }
    job_id = _create_job([row_a])
    cookies = {"sso_session": _session()}
    calls = {"count": 0}

    def changing_workspace(_job_id):
        calls["count"] += 1
        return workspace_a if calls["count"] == 1 else workspace_b

    monkeypatch.setattr(rv, "_load_workspace_unlocked", changing_workspace)
    captured = {}
    monkeypatch.setattr(vr, "V2_EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        vr,
        "generate_export_xlsx",
        lambda rows, show_line_prices=True: captured.setdefault("export", rows) and b"xlsx",
    )
    monkeypatch.setattr(
        vr.pricedb,
        "commit_job",
        lambda _job_id, rows: captured.setdefault("pricedb", rows) and 1,
    )

    review = review_env.get(f"/v2/review/{job_id}", cookies=cookies)
    assert review.status_code == 200
    assert review.json()["rows"][0]["selected_candidate_idx"] == 1
    assert review.json()["rows"][0]["review_status"] == "approved"

    calls["count"] = 0
    exported = review_env.get(
        f"/v2/export/{job_id}?format=xlsx", cookies=cookies
    )
    assert exported.status_code == 200
    assert captured["export"][0]["selected_candidate_idx"] == 1
    assert captured["export"][0]["review_status"] == "approved"

    calls["count"] = 0
    committed = review_env.post(
        f"/v2/pricedb/commit/{job_id}",
        cookies={"sso_session": _session(role="admin")},
    )
    assert committed.status_code == 200
    assert captured["pricedb"][0]["selected_candidate_idx"] == 1
    assert captured["pricedb"][0]["review_status"] == "approved"


def test_current_catalog_blocks_persisted_sellable_snapshot_everywhere(
    review_env, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        vr,
        "_get_catalog_bundle",
        lambda: _Bundle([_CatalogItem("10001", "Obsolete", 0)]),
    )
    monkeypatch.setattr(vr.pricedb, "V2_PRICEDB_PATH", tmp_path / "pricedb.jsonl")
    row = {
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "suggested_candidate_idx": 0,
        "candidates": [{**_candidate("10001"), "matched_from": "lk"}],
    }
    job_id = _create_job([row])
    rv.save_selection(job_id, 0, 0)
    rv.save_decision(job_id, 0, "approved")
    cookies = {"sso_session": _session()}

    review = review_env.get(f"/v2/review/{job_id}", cookies=cookies)
    select = review_env.post(
        f"/v2/review/{job_id}/select",
        json={"dedup_idx": 0, "candidate_idx": 0},
        cookies=cookies,
    )
    approve = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    bulk = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0], "status": "approved"},
        cookies=cookies,
    )
    commit = review_env.post(
        f"/v2/pricedb/commit/{job_id}",
        cookies={"sso_session": _session(role="admin")},
    )

    current = review.json()["rows"][0]["candidates"][0]
    assert current["eligible"] is False
    assert current["persisted_item_status"] == "Sellable"
    assert current["item_status"] == "Obsolete"
    assert select.status_code == 409
    assert approve.status_code == 409
    assert bulk.status_code == 409
    assert commit.status_code == 200
    assert commit.json()["committed"] == 0


def test_catalog_unavailable_fails_closed_for_approval(review_env, monkeypatch):
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: None)
    row = {
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate("10001")],
    }
    job_id = _create_job([row])

    response = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 409
    assert "ikke tilgjengelig" in response.json()["error"]


def test_atomic_review_state_survives_replace_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    rv.save_review_state(
        "job",
        selections={"0": {"candidate_idx": 0}},
        decisions={"0": {"status": "pending"}},
    )
    real_replace = rv.os.replace

    def crash_before_replace(_source, _target):
        raise OSError("simulert krasj")

    monkeypatch.setattr(rv.os, "replace", crash_before_replace)
    with pytest.raises(OSError, match="simulert krasj"):
        rv.save_review_state(
            "job",
            selections={"0": {"candidate_idx": 1}},
            decisions={"0": {"status": "approved"}},
        )

    monkeypatch.setattr(rv.os, "replace", real_replace)
    assert rv.load_selections("job")["0"]["candidate_idx"] == 0
    assert rv.load_decisions("job")["0"]["status"] == "pending"
    assert list((tmp_path / "job").glob("*.tmp")) == []


def test_rematch_is_blocked_once_review_data_exists(review_env):
    job_id = _create_job([{"dedup_idx": 0, "review_status": "pending"}])

    response = review_env.post(
        f"/v2/match/{job_id}", cookies={"sso_session": _session()}
    )

    assert response.status_code == 409
    assert rv.load_review(job_id)[0]["dedup_idx"] == 0


def test_stale_decision_cannot_approve_candidate_after_other_user_selects(review_env):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": candidates,
    }])
    cookies = {"sso_session": _session()}
    stale_revision = rv.get_review_revision(job_id)

    selected = review_env.post(
        f"/v2/review/{job_id}/select",
        json={"dedup_idx": 0, "candidate_idx": 1},
        headers={"If-Match": str(stale_revision)},
        cookies=cookies,
    )
    stale = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={
            "dedup_idx": 0,
            "status": "approved",
            "revision": stale_revision,
            "candidate_identity": vr._candidate_identity(candidates[0]),
        },
        headers={"If-Match": str(stale_revision)},
        cookies=cookies,
    )

    assert selected.status_code == 200
    assert stale.status_code == 409
    assert rv.load_decisions(job_id).get("0", {}).get("status") != "approved"


def test_concurrent_state_cas_allows_exactly_one_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    rv.save_review_state("job", selections={}, decisions={})
    revision = rv.get_review_revision("job")
    barrier = threading.Barrier(2)
    results = []

    def writer(candidate_idx):
        barrier.wait()
        try:
            rv.save_review_state(
                "job",
                selections={"0": {"candidate_idx": candidate_idx}},
                decisions={},
                expected_revision=revision,
            )
            results.append("ok")
        except rv.StaleRevisionError:
            results.append("stale")

    threads = [threading.Thread(target=writer, args=(idx,)) for idx in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["ok", "stale"]


def test_multiprocess_state_cas_allows_exactly_one_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "V2_REVIEWS_DIR", tmp_path)
    rv.save_review_state("job", selections={}, decisions={})
    revision = rv.get_review_revision("job")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    real_load = rv._load_review_state

    def synchronized_load(job_id):
        state = real_load(job_id)
        time.sleep(0.2)
        return state

    monkeypatch.setattr(rv, "_load_review_state", synchronized_load)

    def writer(candidate_idx):
        barrier.wait(timeout=5)
        try:
            rv.save_review_state(
                "job",
                selections={"0": {"candidate_idx": candidate_idx}},
                decisions={},
                expected_revision=revision,
            )
            results.put("ok")
        except rv.StaleRevisionError:
            results.put("stale")

    processes = [context.Process(target=writer, args=(idx,)) for idx in (0, 1)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=2) for _ in processes) == ["ok", "stale"]


def test_deleted_row_rejects_review_mutations_and_restore_preserves_state(review_env):
    row = {
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate()],
    }
    job_id = _create_job([row])
    cookies = {"sso_session": _session()}
    assert review_env.post(
        f"/v2/review/{job_id}/delete", json={"dedup_idx": 0}, cookies=cookies
    ).status_code == 200
    before = rv.load_review_state(job_id)

    select = review_env.post(
        f"/v2/review/{job_id}/select",
        json={"dedup_idx": 0, "candidate_idx": 0},
        cookies=cookies,
    )
    decide = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    bulk = review_env.post(
        f"/v2/review/{job_id}/bulk-decide",
        json={"dedup_indices": [0], "status": "not_same"},
        cookies=cookies,
    )

    assert [select.status_code, decide.status_code, bulk.status_code] == [409, 409, 409]
    assert rv.load_review_state(job_id) == before
    assert review_env.post(
        f"/v2/review/{job_id}/restore", json={"dedup_idx": 0}, cookies=cookies
    ).status_code == 200
    after_restore = rv.load_review_state(job_id)
    assert after_restore["revision"] == before["revision"] + 1
    assert {key: value for key, value in after_restore.items() if key != "revision"} == {
        key: value for key, value in before.items() if key != "revision"
    }


def test_undo_token_cannot_overwrite_a_newer_candidate_mutation(review_env):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": candidates,
    }])
    cookies = {"sso_session": _session()}
    approved = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    token = approved.json()["undo_token"]
    assert review_env.post(
        f"/v2/review/{job_id}/select",
        json={"dedup_idx": 0, "candidate_idx": 1},
        cookies=cookies,
    ).status_code == 200

    stale_undo = review_env.post(
        f"/v2/review/{job_id}/undo",
        json={"undo_token": token},
        cookies=cookies,
    )

    assert stale_undo.status_code == 409
    assert rv.load_selections(job_id)["0"]["candidate_idx"] == 1


def test_learning_tombstone_hides_undone_approval_even_without_physical_cleanup(
    review_env, monkeypatch
):
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": [_candidate()],
    }])
    cookies = {"sso_session": _session()}
    approved = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    monkeypatch.setattr(vr, "remove_learning_by_undo_token", lambda _token: None)

    undone = review_env.post(
        f"/v2/review/{job_id}/undo",
        json={"undo_token": approved.json()["undo_token"]},
        cookies=cookies,
    )

    assert undone.status_code == 200
    assert vr.load_learnings() == []


@pytest.mark.parametrize(
    "price,quantity",
    [("nan", 1), ("inf", 1), (-1, 1), (1, "nan"), (1, "inf"), (1, 0), (1, -1)],
)
def test_add_row_rejects_nonfinite_or_out_of_range_numbers(
    review_env, price, quantity
):
    job_id = _create_job([])
    response = review_env.post(
        f"/v2/review/{job_id}/add-row",
        json={"description": "Ugyldig", "price": price, "quantity": quantity},
        cookies={"sso_session": _session()},
    )
    assert response.status_code == 400
    assert rv.load_review(job_id) == []


@pytest.mark.parametrize(
    "price,quantity",
    [(True, 1), (False, 1), (1, True), (1, False)],
)
def test_add_row_rejects_json_booleans_without_changing_state(
    review_env, price, quantity
):
    job_id = _create_job([])
    revision = rv.get_review_revision(job_id)

    response = review_env.post(
        f"/v2/review/{job_id}/add-row",
        json={"description": "Bool skal avvises", "price": price, "quantity": quantity},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 400
    assert rv.load_review(job_id) == []
    assert rv.get_review_revision(job_id) == revision


@pytest.mark.parametrize("field", ["quantity_override", "quantity_override_competitor"])
@pytest.mark.parametrize("value", ["tekst", "nan", "inf", "-inf", 0, -1, None])
def test_extras_rejects_invalid_quantity_without_persisting(
    review_env, field, value
):
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "total_units": 2,
        "competitor_unit_price": 10,
        "candidates": [],
    }])

    response = review_env.post(
        f"/v2/review/{job_id}/extras",
        json={"dedup_idx": 0, field: value},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 400
    assert rv.load_extras(job_id) == {}


def test_extras_requires_existing_non_deleted_row(review_env):
    job_id = _create_job([{"dedup_idx": 0, "review_status": "pending"}])
    cookies = {"sso_session": _session()}
    assert review_env.post(
        f"/v2/review/{job_id}/extras",
        json={"dedup_idx": 99, "quantity_override": 2},
        cookies=cookies,
    ).status_code == 404
    assert review_env.post(
        f"/v2/review/{job_id}/delete",
        json={"dedup_idx": 0},
        cookies=cookies,
    ).status_code == 200
    assert review_env.post(
        f"/v2/review/{job_id}/extras",
        json={"dedup_idx": 0, "quantity_override": 2},
        cookies=cookies,
    ).status_code == 409
    assert rv.load_extras(job_id) == {}


def test_extras_normalizes_valid_quantities_for_review_and_export(review_env):
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "total_units": 2,
        "competitor_unit_price": 10,
        "competitor_line_amount": 20,
        "candidates": [],
    }])

    response = review_env.post(
        f"/v2/review/{job_id}/extras",
        json={
            "dedup_idx": 0,
            "quantity_override": "3,5",
            "quantity_override_competitor": "4",
        },
        cookies={"sso_session": _session()},
    )
    review = review_env.get(
        f"/v2/review/{job_id}", cookies={"sso_session": _session()}
    ).json()["rows"][0]
    exported = build_export_rows([review])[0]

    assert response.status_code == 200
    assert review["quantity_override"] == 3.5
    assert review["quantity_override_competitor"] == 4.0
    assert exported["Totale enheter Konkurrent"] == 4.0
    assert exported["Totale Enheter OM"] == 3.5


def test_extras_rejects_stale_revision_without_lost_comment(review_env):
    job_id = _create_job([{"dedup_idx": 0, "review_status": "pending"}])
    cookies = {"sso_session": _session()}
    revision = rv.get_review_revision(job_id)

    first = review_env.post(
        f"/v2/review/{job_id}/extras",
        json={"dedup_idx": 0, "comment": "første"},
        headers={"If-Match": str(revision)},
        cookies=cookies,
    )
    stale = review_env.post(
        f"/v2/review/{job_id}/extras",
        json={"dedup_idx": 0, "comment": "andre"},
        headers={"If-Match": str(revision)},
        cookies=cookies,
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert rv.load_extras(job_id)["0"]["comment"] == "første"


def test_rematch_is_blocked_by_material_state_without_review_json(
    review_env, monkeypatch
):
    job_id = uuid.uuid4().hex
    (vr.V2_UPLOADS_DIR / job_id).mkdir(parents=True)
    vr._append_v2_job({
        "job_id": job_id,
        "created_at": "2026",
        "status": "matched",
        "owner_sub": "owner",
    })
    rv.save_review_state(
        job_id,
        selections={"0": {"candidate_idx": 0}},
        decisions={},
    )
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: _Bundle([]))

    response = review_env.post(
        f"/v2/match/{job_id}", cookies={"sso_session": _session()}
    )

    assert response.status_code == 409


def test_catalog_health_requires_explicit_item_status_and_alc():
    for row in (
        {"Katalog: ALC": 2},
        {"Katalog: Item Status": "", "Katalog: ALC": 2},
        {"Status": "Sellable", "Katalog: ALC": 2},
        {"Katalog: Item Status": "Sellable"},
    ):
        item = _CatalogItem("10001", "Sellable", 2)
        item.row = row
        assert "obligatoriske" in vr._catalog_health_error(_Bundle([item]))


def test_review_ui_has_revision_a11y_and_bulk_clears_undo_contracts():
    html = (Path(__file__).parent / "v2" / "templates" / "review.html").read_text()
    assert 'headers["If-Match"]' in html
    assert "candidate_identity:candidateIdentity" in html
    assert '"aria-current"' in html
    assert 'node("fieldset")' in html
    assert 'event.target.closest("input,textarea,select,button")' in html
    assert "state.lastDecision=null" in html
    assert "Godkjent match" in html
    assert "ALC per enhet" in html
    assert "data.committed??0" in html
    assert 'className:"queue-open"' in html
    assert 'node("button",{type:"button",className:"queue-open"' in html
    assert 'node("li",{className:`queue-item' in html
    assert 'tabindex:"0","aria-current"' not in html
    assert "candidateCount===0" in html
    assert '$("undoButton").classList.remove("hidden")' in html
    assert "checked:selected||suggested" not in html
    assert "checked:selected,disabled:" in html
    assert 'className:`candidate${selected||suggested?" active":""}' in html


def test_review_finish_contract_keeps_recovery_responsive_and_single_flight():
    html = (Path(__file__).parent / "v2" / "templates" / "review.html").read_text(
        encoding="utf-8"
    )

    assert '.toast button.quiet { color:#fff;' in html
    assert 'setTimeout(()=>$("toast").classList.add("hidden"),7000)' not in html
    assert 'id="dismissToast"' in html
    assert 'aria-keyshortcuts="Alt+Z"' in html
    assert 'event.altKey&&event.key.toLocaleLowerCase()==="z"' in html
    assert '@media (max-width:800px)' in html
    assert '.candidate { grid-template-columns:24px minmax(0,1fr);' in html
    assert '.candidate > :nth-child(3),.candidate > :nth-child(4) { grid-column:2;' in html
    assert 'class="workspace" aria-label="Gjennomgang av produktlinjer" aria-busy="true"' in html
    assert 'mutationInFlight:false,busy:true' in html
    assert 'function setBusy(busy)' in html
    assert 'async function mutate(url,options={},reload=true)' in html
    assert 'if(state.mutationInFlight)' in html
    assert 'await load(false)' in html
    assert 'data-mutation' in html
    assert 'async function load(manageBusy=true,rethrow=false)' in html
    assert 'if(rethrow)throw error' in html
    assert 'if(reload)await load(false,true)' in html
    assert 'syncFailed:false' in html
    assert 'const effectiveBusy=busy||state.syncFailed' in html
    assert 'state.syncFailed=true' in html
    assert 'className:"search-result",disabled:state.busy||state.syncFailed||state.locked,"data-mutation":""' in html
    assert 'outline:3px solid var(--blue)' in html
    assert '#92b8d7' not in html
    assert 'className:"muted",text:`Linje ${row.dedup_idx}`' not in html


def test_review_queue_keeps_product_text_clear_of_status_and_removes_line_search():
    html = (Path(__file__).parent / "v2" / "templates" / "review.html").read_text(
        encoding="utf-8"
    )

    assert 'id="queueSearch"' not in html
    assert 'id="queueCount"' in html
    assert '<option value="done">Ferdige</option>' in html
    assert 'const status=$("statusFilter").value' in html
    assert 'status==="pending" ? !row.deleted&&row.review_status==="pending"' in html
    assert 'status==="done" ? row.deleted||row.review_status!=="pending"' in html
    assert '.queue-item { display:grid; grid-template-columns:22px minmax(0,1fr);' in html
    assert '.queue-title { display:block;' in html
    assert 'className:"queue-meta-row"' in html
    assert 'className:`queue-status ${statusClass}`' in html
    assert '"aria-label":`Åpne ${row.description||"linje"}. Status: ${statusText}`' in html
    assert 'className:"catalog-search"' in html
    assert 'text:"Finn et annet OneMed-produkt"' in html
    assert 'Beslutning: ${decisionText[row.review_status]||"Ikke vurdert"} · Linje ${row.dedup_idx}' in html
    assert 'if(state.busy||state.syncFailed)throw new Error("Gjennomgangen er ikke synkronisert.")' in html
    assert 'disabled:state.busy||state.syncFailed||state.locked,"data-mutation":""' in html
    assert 'if(!state.lastDecision||state.busy||state.syncFailed)return' in html
    assert 'state.lastDecision&&!state.busy&&!state.syncFailed' in html
    assert '.toast button:focus-visible { outline-color:#fff;' in html
    assert 'outline:3px solid var(--blue)' in html
    assert 'function clearError() { if(state.syncFailed)return;' in html
    assert 'function showSyncError(message)' in html
    assert 'if(state.syncFailed){showSyncError();return;}' in html
    assert 'state.syncFailed=false;clearError();' in html
    assert 'state.syncFailed=true;showSyncError(error.message)' in html


def test_approved_decision_is_bound_to_product_identity_after_candidate_reorder(
    review_env
):
    candidates = [_candidate("10000"), {**_candidate("10001"), "candidate_idx": 1}]
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "pending",
        "suggested_candidate_idx": 0,
        "candidates": candidates,
    }])
    cookies = {"sso_session": _session()}
    approved = review_env.post(
        f"/v2/review/{job_id}/decide",
        json={"dedup_idx": 0, "status": "approved"},
        cookies=cookies,
    )
    assert approved.status_code == 200

    persisted = rv.load_review(job_id)
    persisted[0]["candidates"] = [
        {**_candidate("10001"), "candidate_idx": 0},
        {**_candidate("10000"), "candidate_idx": 1},
    ]
    rv.save_review(job_id, persisted)

    restarted = review_env.get(f"/v2/review/{job_id}", cookies=cookies).json()["rows"][0]
    assert restarted["review_status"] == "pending"
    assert restarted["selected_candidate_idx"] is None
    assert build_export_rows([restarted])[0]["Vårt Art.Nr"] == ""
    assert vr.pricedb.commit_job(job_id, [restarted]) == 0


def test_direct_export_rehydrates_legacy_approved_selection(review_env, tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "V2_EXPORTS_DIR", tmp_path)
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "best_candidate_idx": 0,
        "candidates": [_candidate("10001")],
    }])

    response = review_env.get(
        f"/v2/export/{job_id}?format=xlsx",
        cookies={"sso_session": _session()},
    )

    workbook = pd.read_excel(BytesIO(response.content))
    assert response.status_code == 200
    assert workbook.iloc[0]["Vårt Art.Nr"] == 10001
    assert rv.load_selections(job_id)["0"]["candidate_idx"] == 0


@pytest.mark.parametrize("catalog_mode", ["obsolete", "unavailable"])
def test_direct_xlsx_and_pdf_export_hide_snapshot_when_catalog_is_not_currently_valid(
    review_env, monkeypatch, tmp_path, catalog_mode
):
    monkeypatch.setattr(vr, "V2_EXPORTS_DIR", tmp_path)
    if catalog_mode == "obsolete":
        monkeypatch.setattr(
            vr,
            "_get_catalog_bundle",
            lambda: _Bundle([_CatalogItem("10001", "Obsolete", 0)]),
        )
    else:
        monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: None)
    captured_pdf_rows = []

    def fake_pdf(rows, show_line_prices=True):
        captured_pdf_rows.extend(build_export_rows(rows))
        return b"%PDF-test"

    monkeypatch.setattr(vr, "generate_export_pdf", fake_pdf)
    job_id = _create_job([{
        "dedup_idx": 0,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "suggested_candidate_idx": 0,
        "description": "Konkurrentvare",
        "candidates": [_candidate("10001")],
    }])
    rv.save_selection(job_id, 0, 0)
    rv.save_decision(job_id, 0, "approved")

    xlsx = review_env.get(
        f"/v2/export/{job_id}?format=xlsx", cookies={"sso_session": _session()}
    )
    pdf = review_env.get(
        f"/v2/export/{job_id}?format=pdf", cookies={"sso_session": _session()}
    )

    workbook = pd.read_excel(BytesIO(xlsx.content))
    assert workbook.iloc[0]["Vårt Art.Nr"] != "10001"
    assert pdf.status_code == 200
    assert captured_pdf_rows[0]["Vårt Art.Nr"] == ""


def test_review_workbench_uses_safe_dom_rendering_for_catalog_data():
    html = (Path(__file__).parent / "v2" / "templates" / "review.html").read_text(
        encoding="utf-8"
    )

    assert '<html lang="no">' in html
    assert "onclick=" not in html
    assert "oninput=" not in html
    assert "onchange=" not in html
    assert ".innerHTML" not in html
    assert "textContent" in html
    assert "replaceChildren" in html
    assert "</script><script>" not in html
    assert "seed=b96f7840" in html
    assert html.index("<!-- impeccable:direction-contract") > html.index("<body>")
    assert html.index("<!-- impeccable:direction-contract") < html.index('<main class="shell">')
    for block in ("THESIS:", "OWN-WORLD:", "STORY:", "FIRST VIEWPORT:", "FORM:", "FINISH:"):
        assert block in html
    assert "unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance" in html
    assert "eyebrow" not in html
    assert "Ingen egnet kandidat" not in html
    assert "Ingen passende vare funnet" in html
    assert 'className:"comparison"' in html
    assert "row.confidence" in html
    assert "row.raw_text" in html


def test_add_row_keeps_automatic_match_as_suggestion(review_env, monkeypatch):
    job_id = _create_job([])
    monkeypatch.setattr(vr, "_get_catalog_bundle", lambda: object())

    def matched(row, _bundle, top_n, prefer_own_brands):
        return {
            **row,
            "candidates": [_candidate("10001")],
            "best_candidate_idx": 0,
            "match_status": "matched",
            "our_unit_price": 10,
        }

    monkeypatch.setattr("v2.matching._match_single_row", matched)

    response = review_env.post(
        f"/v2/review/{job_id}/add-row",
        json={"description": "Ny vare", "price": 20, "quantity": 1},
        cookies={"sso_session": _session()},
    )

    assert response.status_code == 200
    row = response.json()["row"]
    assert row["candidate_status"] == "suggested"
    assert row["suggested_candidate_idx"] == 0
    assert row["selected_candidate_idx"] is None
