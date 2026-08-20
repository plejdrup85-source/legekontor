from io import BytesIO

import pandas as pd
import pytest

from v2.export import (
    build_export_rows,
    calculate_totals,
    generate_export_xlsx,
)


def test_export_uses_correct_norwegian_column_labels_and_totals():
    review_rows = [{
        "review_status": "approved",
        "competitor_line_amount": 120.0,
        "our_comparable_line_price": 90.0,
        "savings_amount": 30.0,
        "selected_candidate_idx": 0,
        "candidates": [{
            "candidate_idx": 0,
            "our_artnr": "10001",
            "our_description": "Vårt produkt",
            "our_specification": "Steril",
            "our_producer": "OneMed",
            "match_quality": "God",
        }],
        "merged_from_count": 2,
        "merge_warning": True,
        "inconsistent_fields": [{"field": "pris"}],
    }]
    expected_columns = [
        "Konkurrent",
        "Konkurrent Art.Nr",
        "Beskrivelse",
        "Kvt.",
        "Pakning",
        "Pakningsstr.",
        "Totale enheter Konkurrent",
        "Totale Enheter OM",
        "Konk. linjebeløp",
        "Vårt Art.Nr",
        "Vårt produktnavn",
        "Vår spesifikasjon",
        "Vår produsent",
        "Vår pris/enhet",
        "Vårt linjebeløp",
        "Besparelse",
        "Besparelse %",
        "Kommentar",
        "Sammenslått",
        "Avvik i sammenslått",
    ]

    export_rows = build_export_rows(review_rows)

    assert list(export_rows[0]) == expected_columns
    assert export_rows[0]["Konk. linjebeløp"] == 120.0
    assert export_rows[0]["Vårt linjebeløp"] == 90.0
    assert calculate_totals(export_rows)["total_savings"] == 30.0
    workbook = pd.read_excel(BytesIO(generate_export_xlsx(review_rows)))
    assert list(workbook.columns) == expected_columns


@pytest.mark.parametrize("review_status", ["pending", "not_same", "no_suitable"])
def test_export_keeps_suggestion_blank_without_explicit_selection(review_status):
    rows = build_export_rows([{
        "review_status": review_status,
        "description": "Konkurrentvare",
        "selected_candidate_idx": None,
        "suggested_candidate_idx": 0,
        "candidates": [{
            "candidate_idx": 0,
            "our_artnr": "10001",
            "our_description": "OneMed-vare",
            "our_unit_price": 10.0,
        }],
        "our_unit_price": 10.0,
        "our_comparable_line_price": 20.0,
        "savings_amount": 5.0,
    }])

    assert len(rows) == 1
    assert rows[0]["Beskrivelse"] == "Konkurrentvare"
    assert rows[0]["Vårt Art.Nr"] == ""
    assert rows[0]["Vårt produktnavn"] == ""
    assert rows[0]["Vår pris/enhet"] is None
    assert rows[0]["Vårt linjebeløp"] is None


def test_export_projects_explicit_selection_while_decision_is_pending():
    rows = build_export_rows([{
        "review_status": "pending",
        "selected_candidate_idx": 1,
        "suggested_candidate_idx": 0,
        "candidates": [{
            "candidate_idx": 1,
            "our_artnr": "10001",
            "our_description": "Eksplisitt valgt vare",
            "our_unit_price": 10.0,
        }],
        "our_unit_price": 10.0,
        "our_comparable_line_price": 20.0,
        "savings_amount": 5.0,
        "competitor_line_amount": 25.0,
    }])

    assert rows[0]["Vårt Art.Nr"] == "10001"
    assert rows[0]["Vårt produktnavn"] == "Eksplisitt valgt vare"
    assert rows[0]["Vår pris/enhet"] == 10.0
    assert rows[0]["Vårt linjebeløp"] == 20.0


def test_totals_exclude_unmatched_rows_from_savings_denominator():
    totals = calculate_totals([
        {
            "Konk. linjebeløp": 100,
            "Vårt linjebeløp": 80,
            "Totale enheter Konkurrent": 1,
            "Totale Enheter OM": 1,
        },
        {
            "Konk. linjebeløp": 200,
            "Vårt linjebeløp": None,
            "Totale enheter Konkurrent": 1,
            "Totale Enheter OM": 1,
        },
    ])

    assert totals["total_competitor"] == 100
    assert totals["total_onemed"] == 80
    assert totals["total_savings"] == 20
    assert totals["savings_pct"] == 20
