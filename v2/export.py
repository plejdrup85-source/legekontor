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
        df = pd.concat([df, pd.DataFrame([totals_row])], ignore_index=True)
    else:
        # Dedicated totals rows — no prices in data rows, totals in their own rows
        blank = {col: "" for col in columns}
        row_comp = dict(blank)
        row_comp[columns[0]] = "Totalt konkurrent"
        row_comp[columns[-1]] = totals["total_competitor"]
        row_our = dict(blank)
        row_our[columns[0]] = "Totalt OneMed"
        row_our[columns[-1]] = totals["total_onemed"]
        row_sav = dict(blank)
        row_sav[columns[0]] = "Besparelse"
        row_sav[columns[-1]] = f"{totals['total_savings']} ({totals['savings_pct']}%)"
        df = pd.concat([df, pd.DataFrame([blank, row_comp, row_our, row_sav])], ignore_index=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="V2 Prissammenligning")
    return buf.getvalue()


def generate_export_pdf(review_rows: List[Dict[str, Any]], show_line_prices: bool = True) -> bytes:
    """Generate a PDF report from review rows.

    Returns raw PDF bytes.
    """
    from datetime import date
    from fpdf import FPDF

    rows = build_export_rows(review_rows)
    totals = calculate_totals(rows)
    today = date.today().strftime("%d.%m.%Y")

    # -- Brand colors --
    C_PRIMARY = (31, 78, 121)      # OneMed dark blue
    C_ACCENT = (0, 133, 173)       # teal accent
    C_GREEN = (22, 163, 74)
    C_RED = (220, 38, 38)
    C_GRAY_TEXT = (100, 100, 100)
    C_LIGHT_BG = (247, 248, 250)
    C_WHITE = (255, 255, 255)
    C_DIVIDER = (210, 215, 220)
    C_BLACK = (30, 30, 30)

    def _s(v, maxlen=50):
        """Safe string format."""
        if v is None or v == "":
            return "-"
        if isinstance(v, float):
            return f"{v:,.2f}"
        return str(v)[:maxlen]

    def _kr(v):
        """Format as NOK amount."""
        if v is None or v == "":
            return "-"
        try:
            return f"{float(v):,.2f} kr"
        except (ValueError, TypeError):
            return "-"

    class PDF(FPDF):
        def header(self):
            # Logo or text brand
            if _LOGO_PATH.exists():
                try:
                    self.image(str(_LOGO_PATH), x=10, y=8, h=14)
                except Exception:
                    self._draw_text_logo()
            else:
                self._draw_text_logo()
            # Right side: title + date
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*C_GRAY_TEXT)
            self.set_xy(self.w - 80, 8)
            self.cell(70, 5, "Prissammenligning", align="R")
            self.set_xy(self.w - 80, 13)
            self.cell(70, 5, today, align="R")
            # Divider line
            self.set_draw_color(*C_DIVIDER)
            self.line(10, 24, self.w - 10, 24)
            self.set_y(28)

        def _draw_text_logo(self):
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(*C_PRIMARY)
            self.text(10, 16, "OneMed")

        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*C_GRAY_TEXT)
            self.cell(0, 8, f"Side {self.page_no()}/{{nb}}", align="C")

    pdf = PDF(orientation="L", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ============================================================
    # SUMMARY BOX — 3 columns: Konkurrent | OneMed | Besparelse
    # ============================================================
    box_y = pdf.get_y()
    box_w = (pdf.w - 20 - 8) / 3  # 3 equal columns with 4mm gaps
    box_h = 22

    def _draw_summary_card(x, y, w, h, label, value, accent=None):
        # Card background
        pdf.set_fill_color(*C_LIGHT_BG)
        if accent:
            pdf.set_fill_color(*accent, 15 if accent == C_GREEN else 15)
            # Lighter tint — fpdf doesn't do alpha so use a light mix
            if accent == C_GREEN:
                pdf.set_fill_color(235, 250, 240)
            elif accent == C_RED:
                pdf.set_fill_color(254, 240, 240)
        pdf.rect(x, y, w, h, "F")
        # Top accent bar
        if accent:
            pdf.set_fill_color(*accent)
            pdf.rect(x, y, w, 1.5, "F")
        # Label
        pdf.set_xy(x + 4, y + 4)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*C_GRAY_TEXT)
        pdf.cell(w - 8, 4, label)
        # Value
        pdf.set_xy(x + 4, y + 10)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(*(accent if accent else C_BLACK))
        pdf.cell(w - 8, 7, value)

    _draw_summary_card(10, box_y, box_w, box_h,
                       "Totalt konkurrent", _kr(totals["total_competitor"]))
    _draw_summary_card(10 + box_w + 4, box_y, box_w, box_h,
                       "Totalt OneMed", _kr(totals["total_onemed"]), accent=C_PRIMARY)

    sav = totals["total_savings"]
    sav_color = C_GREEN if sav >= 0 else C_RED
    sav_text = f"{_kr(sav)}  ({totals['savings_pct']}%)"
    _draw_summary_card(10 + (box_w + 4) * 2, box_y, box_w, box_h,
                       "Besparelse", sav_text, accent=sav_color)

    pdf.set_y(box_y + box_h + 6)

    # Row counts line
    approved = sum(1 for r in rows if r.get("Review-status") == "approved")
    rejected = sum(1 for r in rows if r.get("Review-status") == "rejected")
    pending = sum(1 for r in rows if r.get("Review-status") == "pending")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*C_GRAY_TEXT)
    pdf.cell(0, 4, f"{totals['row_count']} linjer  |  {approved} godkjent  |  {rejected} avvist  |  {pending} ventende", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ============================================================
    # PRODUCT COMPARISON TABLE
    # ============================================================

    # Column definitions: Konkurrent side → OneMed side
    if show_line_prices:
        col_defs = [
            ("Konk. Art.Nr",       20),
            ("Konkurrent",         42),
            ("Konk. Spesifikasjon", 30),
            ("Antall",             14),
            ("Konk. pris",         20),
            ("OneMed Art.Nr",      20),
            ("OneMed",             42),
            ("OneMed Spesifikasjon", 30),
            ("OneMed pris",        20),
            ("Besparelse",         22),
        ]
    else:
        col_defs = [
            ("Konk. Art.Nr",       22),
            ("Konkurrent",         52),
            ("Konk. Spesifikasjon", 38),
            ("Antall",             16),
            ("OneMed Art.Nr",      22),
            ("OneMed",             52),
            ("OneMed Spesifikasjon", 52),
        ]

    col_widths = [c[1] for c in col_defs]
    row_h = 5.5

    # Table header
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_fill_color(*C_PRIMARY)
    pdf.set_text_color(*C_WHITE)
    for name, w in col_defs:
        pdf.cell(w, 6, name, border=0, fill=True, align="C")
    pdf.ln()

    # Table rows
    pdf.set_text_color(*C_BLACK)
    for i, r in enumerate(rows):
        # Alternate background
        if i % 2 == 1:
            pdf.set_fill_color(*C_LIGHT_BG)
        else:
            pdf.set_fill_color(*C_WHITE)
        bg = True  # always fill for consistent look

        pdf.set_font("Helvetica", "", 7)

        if show_line_prices:
            vals = [
                _s(r.get("Konkurrent Art.Nr"), 20),
                _s(r.get("Beskrivelse"), 40),
                _s(r.get("Vart spesifikasjon"), 30),  # competitor spec not in export — use description context
                _s(r.get("Totalt enheter")),
                _kr(r.get("Konk. linjebelop")),
                _s(r.get("Vart Art.Nr"), 20),
                _s(r.get("Vart produktnavn"), 40),
                _s(r.get("Vart spesifikasjon"), 30),
                _kr(r.get("Vart linjebelop")),
                _kr(r.get("Besparelse")),
            ]
        else:
            vals = [
                _s(r.get("Konkurrent Art.Nr"), 22),
                _s(r.get("Beskrivelse"), 50),
                _s(r.get("Vart spesifikasjon"), 38),
                _s(r.get("Totalt enheter")),
                _s(r.get("Vart Art.Nr"), 22),
                _s(r.get("Vart produktnavn"), 50),
                _s(r.get("Vart spesifikasjon"), 50),
            ]

        for val, w in zip(vals, col_widths):
            pdf.cell(w, row_h, val, border=0, fill=bg)
        pdf.ln()

        # Subtle row separator
        pdf.set_draw_color(*C_DIVIDER)
        pdf.line(10, pdf.get_y(), 10 + sum(col_widths), pdf.get_y())

    # ============================================================
    # BOTTOM TOTALS
    # ============================================================
    pdf.ln(6)
    pdf.set_draw_color(*C_DIVIDER)
    pdf.line(10, pdf.get_y(), pdf.w - 10, pdf.get_y())
    pdf.ln(4)

    tw = 85
    lh = 7

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*C_BLACK)
    pdf.set_fill_color(*C_LIGHT_BG)
    pdf.cell(tw, lh, f"Totalt konkurrent:  {totals['total_competitor']:,.2f} kr", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(tw, lh, f"Totalt OneMed:  {totals['total_onemed']:,.2f} kr", fill=True, new_x="LMARGIN", new_y="NEXT")

    # Savings — highlighted
    pdf.set_text_color(*(C_GREEN if sav >= 0 else C_RED))
    if sav >= 0:
        pdf.set_fill_color(235, 250, 240)
    else:
        pdf.set_fill_color(254, 240, 240)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(tw, lh + 1, f"Besparelse:  {sav:,.2f} kr  ({totals['savings_pct']}%)", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*C_BLACK)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
