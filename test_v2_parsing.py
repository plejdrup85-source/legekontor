from io import BytesIO

import pandas as pd
import pytest
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from v2.parsing import parse_xlsx_rows


def _structured_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    main_fill = PatternFill("solid", fgColor="4472C4")
    subsection_fill = PatternFill("solid", fgColor="D9EAF7")

    sheet.append(["Varer   ", None, "GyMo"])
    sheet.append(["UNDERSØKELSESROM", "Varenr", None])
    sheet["A2"].fill = main_fill
    sheet["A2"].font = Font(bold=True)
    sheet.append(["Metall varer", None, None])
    sheet["A3"].fill = subsection_fill
    sheet["A3"].font = Font(bold=True)
    sheet.append(["Spekulum med æøå", 10001, 19.5])
    sheet.append(["Produkt uten varenummer", None, 7.0])
    sheet.append(["Produkt uten pris", 10002, None])
    sheet.append(["Produkt uten varenummer og pris", None, None])
    sheet.append(["SUM", None, "=SUM(C4:C7)"])
    sheet.append(["LAB", None, None])
    sheet["A9"].fill = main_fill
    sheet["A9"].font = Font(bold=True)
    sheet.append(["Større utstyr", None, None])
    sheet["A10"].fill = subsection_fill
    sheet.append(["Måleinstrument", 10003, 100.0])

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_xlsx_multiline_headers_and_styled_sections_produce_only_products():
    rows = parse_xlsx_rows(_structured_workbook(), "strukturert.xlsx")

    assert len(rows) == 5
    assert [row["description"] for row in rows] == [
        "Spekulum med æøå",
        "Produkt uten varenummer",
        "Produkt uten pris",
        "Produkt uten varenummer og pris",
        "Måleinstrument",
    ]
    assert [row["competitor_artnr"] for row in rows] == ["10001", "", "10002", "", "10003"]
    assert [row["price_before_discount"] for row in rows] == [19.5, 7.0, None, None, 100.0]
    assert {row["competitor"] for row in rows} == {"GyMo"}
    assert [(row["section"], row["subsection"]) for row in rows] == [
        ("UNDERSØKELSESROM", "Metall varer"),
        ("UNDERSØKELSESROM", "Metall varer"),
        ("UNDERSØKELSESROM", "Metall varer"),
        ("UNDERSØKELSESROM", "Metall varer"),
        ("LAB", "Større utstyr"),
    ]


def test_xlsx_multiline_headers_do_not_guess_between_numeric_columns():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Varer", None, "Ukjent antall", "Ukjent leverandørpris"])
    sheet.append([None, "Varenr", None, None])
    sheet.append(["Produkt A", 10001, 2, 99.0])
    sheet.append(["Produkt B", 10002, 3, 149.0])
    output = BytesIO()
    workbook.save(output)

    rows = parse_xlsx_rows(output.getvalue(), "tvetydige-kolonner.xlsx")

    assert [row["price_before_discount"] for row in rows] == [None, None]
    assert [row["calculated_unit_price"] for row in rows] == [None, None]
    assert [row["competitor_line_amount"] for row in rows] == [None, None]
    assert {row["competitor"] for row in rows} == {""}


@pytest.mark.parametrize("description_header", ["Varer ", "Varer\N{NO-BREAK SPACE}"])
def test_xlsx_positional_fallback_preserves_whitespace_headers(description_header):
    workbook = BytesIO()
    pd.DataFrame(
        [["Steril bandasje", None, "Ukjent verdi"]],
        columns=[description_header, "", "Ukjent kolonne"],
    ).to_excel(workbook, index=False, engine="openpyxl")

    rows = parse_xlsx_rows(workbook.getvalue(), "ukjent-format.xlsx")

    assert [row["description"] for row in rows] == ["Steril bandasje"]
