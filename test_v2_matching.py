import os
import tempfile

import pytest
import pandas as pd

os.environ.setdefault("SSO_SECRET", "testsecret_32_bytes_minimum_ok_ok_ok")
os.environ.setdefault("SSO_EXPECTED_AUD", "legekontor")
os.environ.setdefault("SSO_DASHBOARD_URL", "https://dashboard.test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import app as appmod
from app import _apply_column_mapping
from v2.matching import _build_v1_compat_row, _match_single_row


def test_build_v1_compat_row_returns_expected_competitor_fields():
    row = _build_v1_compat_row(
        {
            "description": "Steril kompress",
            "specification": "10 × 10 cm",
            "competitor_artnr": "K-100",
        }
    )

    assert row == {
        "Konkurrent Item Description": "Steril kompress",
        "Konkurrent Specification": "10 × 10 cm",
        "Konkurrent Art.Nr": "K-100",
    }


class _Catalog:
    def __init__(self, result):
        self.result = result

    def match_row(self, *_args, **_kwargs):
        return self.result


class _Bundle:
    def __init__(self, lk_result, full_result=None, combined_result=None):
        empty = (None, [], None, "Ingen")
        self.lk = _Catalog(lk_result)
        self.full = _Catalog(full_result or empty)
        self.combined_result = combined_result or (
            lk_result[0], lk_result[2], lk_result[3], "lk"
        )

    def match_competitor_row(self, *_args, **_kwargs):
        return self.combined_result

    def price_for_artnr(self, _artnr, source=None):
        return 10.0, source or "ALC"


def _catalog_row(artnr, status="Sellable", alc=3, description="Produkt"):
    return {
        "Katalog: Art.Nr": artnr,
        "Katalog: Item Description": description,
        "Katalog: Item Status": status,
        "Katalog: ALC": alc,
    }


def _input_row():
    return {
        "dedup_idx": 1,
        "description": "Kompress",
        "total_units": 2,
        "competitor_line_amount": 30,
    }


def test_match_single_row_keeps_sellable_positive_alc_candidate():
    catalog_row = _catalog_row("10001")
    bundle = _Bundle(("10001", [], catalog_row, "Medium"))

    result = _match_single_row(_input_row(), bundle, top_n=5, prefer_own_brands=True)

    assert result["match_status"] == "matched"
    assert result["candidates"][0]["our_artnr"] == "10001"
    assert result["candidates"][0]["eligible"] is True


@pytest.mark.parametrize("status,alc", [("Obsolete", 3), ("Sellable", 0)])
def test_match_single_row_excludes_ineligible_candidate(status, alc):
    catalog_row = _catalog_row("10001", status=status, alc=alc)
    bundle = _Bundle(("10001", [], catalog_row, "Medium"))

    result = _match_single_row(_input_row(), bundle, top_n=5, prefer_own_brands=True)

    assert result["match_status"] == "no_match"
    assert result["candidates"] == []


def test_match_single_row_sorts_by_quality_then_alc():
    lk_row = _catalog_row("10001", alc=1, description="Lav ALC")
    full_row = _catalog_row("10002", alc=9, description="Bedre match")
    bundle = _Bundle(
        ("10001", [], lk_row, "Lav"),
        ("10002", [], full_row, "Medium"),
        ("10002", full_row, "Medium", "full"),
    )

    result = _match_single_row(_input_row(), bundle, top_n=5, prefer_own_brands=True)

    assert [candidate["our_artnr"] for candidate in result["candidates"]] == [
        "10002",
        "10001",
    ]


def test_equal_quality_suggests_lowest_positive_alc_after_sort():
    lk_row = _catalog_row("10001", alc=2, description="Lav ALC")
    full_row = _catalog_row("10002", alc=8, description="Høy ALC")
    bundle = _Bundle(
        ("10001", [], lk_row, "Medium"),
        ("10002", [], full_row, "Medium"),
        ("10002", full_row, "Medium", "full"),
    )

    result = _match_single_row(_input_row(), bundle, top_n=5, prefer_own_brands=True)

    assert result["candidates"][0]["our_artnr"] == "10001"
    assert result["best_candidate_idx"] == 0


def test_embedding_reload_reuses_saved_custom_mapping(monkeypatch):
    calls = []
    mapping = {
        "Item Status": "Salgsstatus",
        "ALC": "Kostpris",
        "Artikkelnummer": "Varenr",
    }

    class CurrentCatalog:
        embed_index = None

    class Bundle:
        lk = CurrentCatalog()
        full = CurrentCatalog()

        def embeddings_available(self):
            return self.lk.embed_index is not None and self.full.embed_index is not None

    class Loaded:
        embed_index = object()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    def from_excel(path, **kwargs):
        calls.append(kwargs)
        return Loaded()

    monkeypatch.setattr(appmod, "CATALOG_BUNDLE", Bundle())
    monkeypatch.setattr(appmod, "_load_saved_mapping", lambda: mapping)
    monkeypatch.setattr(appmod.Catalog, "from_excel", from_excel)
    monkeypatch.setattr(appmod.threading, "Thread", ImmediateThread)
    appmod.EMBEDDINGS_STATUS["state"] = "idle"

    appmod._start_embeddings_build_background()

    assert len(calls) == 2
    assert all(call["column_mapping"] == mapping for call in calls)


@pytest.mark.parametrize(
    "lk_status,lk_alc,full_status,full_alc,expected_sources",
    [
        ("Sellable", 5, "Sellable", 2, ["full", "lk"]),
        ("Obsolete", 0, "Sellable", 2, ["full"]),
        ("Sellable", 5, "Obsolete", 0, ["lk"]),
    ],
)
def test_duplicate_artnr_candidates_keep_source_identity_price_and_eligibility(
    lk_status, lk_alc, full_status, full_alc, expected_sources
):
    lk_row = _catalog_row("10001", status=lk_status, alc=lk_alc, description="LK")
    full_row = _catalog_row("10001", status=full_status, alc=full_alc, description="Full")

    class SourceBundle(_Bundle):
        def price_for_artnr(self, _artnr, source=None):
            return {"lk": 50.0, "full": 20.0}[source], source

    bundle = SourceBundle(
        ("10001", [], lk_row, "Medium"),
        ("10001", [], full_row, "Medium"),
        ("10001", full_row, "Medium", "full"),
    )

    result = _match_single_row(_input_row(), bundle, top_n=5, prefer_own_brands=True)

    assert [candidate["matched_from"] for candidate in result["candidates"]] == expected_sources
    assert {
        candidate["matched_from"]: candidate["our_unit_price"]
        for candidate in result["candidates"]
    } == {source: {"lk": 50.0, "full": 20.0}[source] for source in expected_sources}


def test_explicit_catalog_mapping_preserves_item_status():
    source = pd.DataFrame({"Salgsstatus": ["Sellable"]})

    mapped = _apply_column_mapping(source, {"Item Status": "Salgsstatus"})

    assert list(mapped.columns) == ["Katalog: Item Status"]
