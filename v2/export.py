"""
V2 Export module – generates Excel and PDF output from review state.

Builds the export from the persisted review data with all overrides
(selections, decisions, extras) applied, so the output reflects
exactly what the reviewer approved.

Supports two modes via show_line_prices:
  True  → full detail: prices per line, quantities, line totals
  False → no line prices: quantities shown, only summary totals
"""
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Optional logo path (place logo.png next to this file or in v2/)
_LOGO_PATH = Path(__file__).parent / "logo.png"

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

# Columns when line prices are hidden
EXPORT_COLUMNS_NO_PRICES = [
    "Review-status",
    "Konkurrent",
    "Konkurrent Art.Nr",
    "Beskrivelse",
    "Kvt.",
    "Pakning",
    "Pakningsstr.",
    "Totalt enheter",
    "Vart Art.Nr",
    "Vart produktnavn",
    "Vart spesifikasjon",
    "Var produsent",
    "Match-kvalitet",
    "Kommentar",
    "Sammenslatt",
    "Avvik i sammenslatt",
]


def calculate_totals(export_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate summary totals from export rows.

    Returns:
        total_competitor: sum of all competitor line amounts
        total_onemed: sum of all our line amounts
        total_savings: total_competitor - total_onemed
        savings_pct: savings as percentage of competitor total
        row_count: number of rows included
    """
    total_comp = 0.0
    total_our = 0.0
    count = 0
    for r in export_rows:
        cl = r.get("Konk. linjebelop")
        ol = r.get("Vart linjebelop")
        if cl is not None:
            total_comp += float(cl)
        if ol is not None:
            total_our += float(ol)
        count += 1
    total_savings = total_comp - total_our
    savings_pct = round(total_savings / total_comp * 100, 1) if total_comp else 0.0
    return {
        "total_competitor": round(total_comp, 2),
        "total_onemed": round(total_our, 2),
        "total_savings": round(total_savings, 2),
        "savings_pct": savings_pct,
        "row_count": count,
    }


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


def generate_export_xlsx(review_rows: List[Dict[str, Any]], show_line_prices: bool = True) -> bytes:
    """Generate an Excel file from review rows.

    Args:
        show_line_prices: If False, price columns are excluded and only totals row is added.

    Returns raw XLSX bytes ready for download.
    """
    rows = build_export_rows(review_rows)
    totals = calculate_totals(rows)
    columns = EXPORT_COLUMNS if show_line_prices else EXPORT_COLUMNS_NO_PRICES

    df = pd.DataFrame(rows)

    # Ensure column order, add missing columns
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    df = df.reindex(columns=columns)

    # Add totals row
    totals_row = {col: "" for col in columns}
    totals_row[columns[0]] = "TOTALT"
    if show_line_prices:
        totals_row["Konk. linjebelop"] = totals["total_competitor"]
        totals_row["Vart linjebelop"] = totals["total_onemed"]
        totals_row["Besparelse"] = totals["total_savings"]
        totals_row["Besparelse %"] = totals["savings_pct"]
    else:
        # Even without line prices, put totals in the last column as a note
        totals_row["Kommentar"] = (
            f"Totalt konkurrent: {totals['total_competitor']:.2f} kr | "
            f"Totalt OneMed: {totals['total_onemed']:.2f} kr | "
            f"Besparelse: {totals['total_savings']:.2f} kr ({totals['savings_pct']}%)"
        )

    df = pd.concat([df, pd.DataFrame([totals_row])], ignore_index=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="V2 Prissammenligning")
    return buf.getvalue()


def generate_export_pdf(review_rows: List[Dict[str, Any]], show_line_prices: bool = True) -> bytes:
    """Generate a PDF report from review rows.

    Returns raw PDF bytes.
    """
    from fpdf import FPDF

    rows = build_export_rows(review_rows)
    totals = calculate_totals(rows)

    class PDF(FPDF):
        def header(self):
            # Logo or text fallback
            if _LOGO_PATH.exists():
                try:
                    self.image(str(_LOGO_PATH), x=10, y=8, h=12)
                except Exception:
                    self.set_font("Helvetica", "B", 14)
                    self.cell(0, 10, "OneMed", align="L")
            else:
                self.set_font("Helvetica", "B", 14)
                self.text(10, 14, "OneMed")
            self.set_font("Helvetica", "", 9)
            self.text(self.w - 50, 14, "Prissammenligning V2")
            self.ln(18)

        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f"Side {self.page_no()}/{{nb}}", align="C")

    pdf = PDF(orientation="L", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # --- Summary box ---
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Sammendrag", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    approved = sum(1 for r in rows if r.get("Review-status") == "approved")
    rejected = sum(1 for r in rows if r.get("Review-status") == "rejected")
    pending = sum(1 for r in rows if r.get("Review-status") == "pending")
    pdf.cell(0, 5, f"Rader: {totals['row_count']}  |  Godkjent: {approved}  |  Avvist: {rejected}  |  Pending: {pending}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # --- Table ---
    if show_line_prices:
        col_defs = [
            ("Beskrivelse", 55),
            ("Konk.", 22),
            ("Konk. Art.Nr", 20),
            ("Enheter", 16),
            ("Konk. linje", 24),
            ("Vart Art.Nr", 20),
            ("Vart produktnavn", 45),
            ("Var pris", 20),
            ("Vart linje", 22),
            ("Besparelse", 22),
            ("Score", 14),
        ]
    else:
        col_defs = [
            ("Beskrivelse", 65),
            ("Konk.", 28),
            ("Konk. Art.Nr", 24),
            ("Enheter", 18),
            ("Vart Art.Nr", 24),
            ("Vart produktnavn", 60),
            ("Vart spesifikasjon", 40),
            ("Score", 18),
        ]

    col_names = [c[0] for c in col_defs]
    col_widths = [c[1] for c in col_defs]

    # Header row
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_fill_color(31, 78, 121)
    pdf.set_text_color(255, 255, 255)
    for name, w in col_defs:
        pdf.cell(w, 6, name, border=1, fill=True, align="C")
    pdf.ln()

    # Data rows
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(0, 0, 0)

    def _fmt(v):
        if v is None or v == "":
            return "-"
        if isinstance(v, float):
            return f"{v:,.2f}"
        return str(v)[:40]

    for i, r in enumerate(rows):
        bg = (i % 2 == 1)
        if bg:
            pdf.set_fill_color(245, 245, 245)

        if show_line_prices:
            vals = [
                _fmt(r.get("Beskrivelse")),
                _fmt(r.get("Konkurrent")),
                _fmt(r.get("Konkurrent Art.Nr")),
                _fmt(r.get("Totalt enheter")),
                _fmt(r.get("Konk. linjebelop")),
                _fmt(r.get("Vart Art.Nr")),
                _fmt(r.get("Vart produktnavn")),
                _fmt(r.get("Var pris/enhet")),
                _fmt(r.get("Vart linjebelop")),
                _fmt(r.get("Besparelse")),
                _fmt(r.get("Match-kvalitet")),
            ]
        else:
            vals = [
                _fmt(r.get("Beskrivelse")),
                _fmt(r.get("Konkurrent")),
                _fmt(r.get("Konkurrent Art.Nr")),
                _fmt(r.get("Totalt enheter")),
                _fmt(r.get("Vart Art.Nr")),
                _fmt(r.get("Vart produktnavn")),
                _fmt(r.get("Vart spesifikasjon")),
                _fmt(r.get("Match-kvalitet")),
            ]

        for val, w in zip(vals, col_widths):
            pdf.cell(w, 5, val, border=1, fill=bg)
        pdf.ln()

    # --- Totals box ---
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, "Totaler", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.set_fill_color(240, 240, 240)
    tw = 90

    pdf.cell(tw, 6, f"Totalt konkurrent:  {totals['total_competitor']:,.2f} kr", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(tw, 6, f"Totalt OneMed:  {totals['total_onemed']:,.2f} kr", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")

    # Savings line in green or red
    sav = totals["total_savings"]
    if sav > 0:
        pdf.set_text_color(22, 163, 74)
    elif sav < 0:
        pdf.set_text_color(220, 38, 38)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(tw, 6, f"Besparelse:  {sav:,.2f} kr  ({totals['savings_pct']}%)", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
