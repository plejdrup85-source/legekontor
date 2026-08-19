from io import BytesIO

import pandas as pd
import pytest

from v2.parsing import parse_xlsx_rows


@pytest.mark.parametrize("description_header", ["Varer ", "Varer\N{NO-BREAK SPACE}"])
def test_xlsx_positional_fallback_preserves_whitespace_headers(description_header):
    workbook = BytesIO()
    pd.DataFrame(
        [["Steril bandasje", None, "Ukjent verdi"]],
        columns=[description_header, "", "Ukjent kolonne"],
    ).to_excel(workbook, index=False, engine="openpyxl")

    rows = parse_xlsx_rows(workbook.getvalue(), "ukjent-format.xlsx")

    assert [row["description"] for row in rows] == ["Steril bandasje"]
