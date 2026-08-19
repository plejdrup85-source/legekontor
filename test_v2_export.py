from io import BytesIO

import pandas as pd

from v2.export import (
    build_export_rows,
    calculate_totals,
    generate_export_xlsx,
)


def test_export_uses_correct_norwegian_column_labels_and_totals():
    review_rows = [{
        "competitor_line_amount": 120.0,
        "our_comparable_line_price": 90.0,
        "savings_amount": 30.0,
        "selected_candidate_idx": 0,
        "candidates": [{
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
        "Review-status",
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
        "Match-kvalitet",
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
