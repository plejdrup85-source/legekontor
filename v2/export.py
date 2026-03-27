"""
V2 Export module – generates Excel output from review state.

Builds the export from the persisted review data with all overrides
(selections, decisions, extras) applied, so the output reflects
exactly what the reviewer approved.
"""
import logging
from io import BytesIO
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

# Column order for the export Excel
EXPORT_COLUMNS = [
    "Review-status",
    "Konkurrent",
    "Konkurrent Art.Nr",
    "Beskrivelse",
    "Kvt.",
    "Pakning",
    "Pakningsstr.",
    "Totalt enheter",
    "Konk. linjebelop",
    "Vart Art.Nr",
    "Vart produktnavn",
    "Vart spesifikasjon",
    "Var produsent",
    "Var pris/enhet",
    "Vart linjebelop",
    "Besparelse",
    "Besparelse %",
    "Match-kvalitet",
    "Kommentar",
    "Sammenslatt",
    "Avvik i sammenslatt",
]


def build_export_rows(review_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert review rows (with overrides applied) to flat export dicts."""
    export_rows = []

    for r in review_rows:
        # Skip deleted rows
        if r.get("deleted"):
            continue
        # Get selected candidate
        sel_idx = r.get("selected_candidate_idx")
        candidates = r.get("candidates", [])
        cand = candidates[sel_idx] if (sel_idx is not None and 0 <= sel_idx < len(candidates)) else None

        comp_line = r.get("competitor_line_amount")
        our_line = r.get("our_comparable_line_price")
        savings = r.get("savings_amount")

        # Calculate savings percentage
        savings_pct = None
        if savings is not None and comp_line and comp_line != 0:
            savings_pct = round(savings / comp_line * 100, 1)

        # Merge warning summary
        merge_note = ""
        if r.get("merged_from_count", 1) > 1:
            merge_note = f"{r['merged_from_count']} rader"

        warn_note = ""
        if r.get("merge_warning") and r.get("inconsistent_fields"):
            fields = [f.get("field", "") for f in r["inconsistent_fields"]]
            warn_note = ", ".join(fields)

        row = {
            "Review-status": r.get("review_status", "pending"),
            "Konkurrent": r.get("competitor", ""),
            "Konkurrent Art.Nr": r.get("competitor_artnr", ""),
            "Beskrivelse": r.get("description", ""),
            "Kvt.": r.get("quantity_purchased"),
            "Pakning": r.get("packaging_text", ""),
            "Pakningsstr.": r.get("packaging_count"),
            "Totalt enheter": r.get("quantity_override") or r.get("total_units"),
            "Konk. linjebelop": comp_line,
            "Vart Art.Nr": cand.get("our_artnr", "") if cand else "",
            "Vart produktnavn": cand.get("our_description", "") if cand else "",
            "Vart spesifikasjon": cand.get("our_specification", "") if cand else "",
            "Var produsent": cand.get("our_producer", "") if cand else "",
            "Var pris/enhet": r.get("our_unit_price"),
            "Vart linjebelop": our_line,
            "Besparelse": savings,
            "Besparelse %": savings_pct,
            "Match-kvalitet": cand.get("match_quality", "") if cand else "Ingen",
            "Kommentar": r.get("comment", ""),
            "Sammenslatt": merge_note,
            "Avvik i sammenslatt": warn_note,
        }
        export_rows.append(row)

    return export_rows


def generate_export_xlsx(review_rows: List[Dict[str, Any]]) -> bytes:
    """Generate an Excel file from review rows.

    Returns raw XLSX bytes ready for download.
    """
    rows = build_export_rows(review_rows)
    df = pd.DataFrame(rows)

    # Ensure column order, add missing columns
    for col in EXPORT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df.reindex(columns=EXPORT_COLUMNS)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="V2 Prissammenligning")
    return buf.getvalue()
