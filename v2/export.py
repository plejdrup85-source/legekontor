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
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Logo path – use the single logo.png in the project root (same as web UI)
_LOGO_PATH = Path(__file__).parent.parent / "onemed_logo.png"

# DejaVuSans font paths — search common locations
_FONT_SEARCH_PATHS = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _find_font(name: str = "DejaVuSans.ttf") -> Optional[Path]:
    """Find a TTF font file by searching common system paths."""
    for p in _FONT_SEARCH_PATHS:
        if p.name == name and p.exists():
            return p
    # Fallback: search with shutil.which-style approach
    for base in [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]:
        if base.exists():
            for f in base.rglob(name):
                return f
    return None

# Column order for the export Excel
EXPORT_COLUMNS = [
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

# Columns when line prices are hidden
EXPORT_COLUMNS_NO_PRICES = [
    "Review-status",
    "Konkurrent",
    "Konkurrent Art.Nr",
    "Beskrivelse",
    "Kvt.",
    "Pakning",
    "Pakningsstr.",
    "Totale enheter Konkurrent",
    "Totale Enheter OM",
    "Vårt Art.Nr",
    "Vårt produktnavn",
    "Vår spesifikasjon",
    "Vår produsent",
    "Match-kvalitet",
    "Kommentar",
    "Sammenslått",
    "Avvik i sammenslått",
]


def calculate_totals(export_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate summary totals from export rows.

    Returns:
        total_competitor: sum of all competitor line amounts
        total_onemed: sum of all our line amounts
        total_savings: total_competitor - total_onemed
        savings_pct: savings as percentage of competitor total
        total_units_competitor: sum of competitor units
        total_units_om: sum of OM units
        row_count: number of rows included
    """
    total_comp = 0.0
    total_our = 0.0
    total_units_comp = 0.0
    total_units_om = 0.0
    count = 0
    for r in export_rows:
        cl = r.get("Konk. linjebeløp")
        ol = r.get("Vårt linjebeløp")
        uc = r.get("Totale enheter Konkurrent")
        uo = r.get("Totale Enheter OM")
        if cl is not None:
            total_comp += float(cl)
        if ol is not None:
            total_our += float(ol)
        if uc is not None:
            total_units_comp += float(uc)
        if uo is not None:
            total_units_om += float(uo)
        count += 1
    total_savings = total_comp - total_our
    savings_pct = round(total_savings / total_comp * 100, 1) if total_comp else 0.0
    return {
        "total_competitor": round(total_comp, 2),
        "total_onemed": round(total_our, 2),
        "total_savings": round(total_savings, 2),
        "savings_pct": savings_pct,
        "total_units_competitor": round(total_units_comp, 2),
        "total_units_om": round(total_units_om, 2),
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

        # Effective quantities: competitor and OM may differ due to separate overrides
        base_units = r.get("total_units")
        comp_units = r.get("quantity_override_competitor") if r.get("quantity_override_competitor") is not None else base_units
        om_units = r.get("quantity_override") if r.get("quantity_override") is not None else base_units

        row = {
            "Review-status": r.get("review_status", "pending"),
            "Konkurrent": r.get("competitor", ""),
            "Konkurrent Art.Nr": r.get("competitor_artnr", ""),
            "Beskrivelse": r.get("description", ""),
            "Kvt.": r.get("quantity_purchased"),
            "Pakning": r.get("packaging_text", ""),
            "Pakningsstr.": r.get("packaging_count"),
            "Totale enheter Konkurrent": comp_units,
            "Totale Enheter OM": om_units,
            "Konk. linjebeløp": comp_line,
            "Vårt Art.Nr": cand.get("our_artnr", "") if cand else "",
            "Vårt produktnavn": cand.get("our_description", "") if cand else "",
            "Vår spesifikasjon": cand.get("our_specification", "") if cand else "",
            "Vår produsent": cand.get("our_producer", "") if cand else "",
            "Vår pris/enhet": r.get("our_unit_price"),
            "Vårt linjebeløp": our_line,
            "Besparelse": savings,
            "Besparelse %": savings_pct,
            "Match-kvalitet": cand.get("match_quality", "") if cand else "Ingen",
            "Kommentar": r.get("comment", ""),
            "Sammenslått": merge_note,
            "Avvik i sammenslått": warn_note,
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
    totals_row["Totale enheter Konkurrent"] = totals["total_units_competitor"]
    totals_row["Totale Enheter OM"] = totals["total_units_om"]
    if show_line_prices:
        totals_row["Konk. linjebeløp"] = totals["total_competitor"]
        totals_row["Vårt linjebeløp"] = totals["total_onemed"]
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
    """Generate a branded PDF report from review rows.

    Returns raw PDF bytes.
    """
    from datetime import date
    from fpdf import FPDF

    rows = build_export_rows(review_rows)
    totals = calculate_totals(rows)
    today = date.today().strftime("%d.%m.%Y")

    # -- Design tokens --
    C_PRIMARY = (122, 166, 65)     # #7AA641 OneMed green
    C_DARK = (31, 42, 55)         # #1F2A37 dark text
    C_SECONDARY = (91, 101, 115)  # #5B6573 secondary text
    C_BORDER = (217, 225, 232)    # #D9E1E8 light grey borders
    C_PANEL = (247, 249, 251)     # #F7F9FB very light bg
    C_SAV_BG = (238, 246, 229)    # #EEF6E5 savings highlight bg
    C_SAV_TEXT = (94, 142, 46)    # #5E8E2E savings highlight text
    C_WHITE = (255, 255, 255)
    C_RED = (200, 50, 50)

    M_LEFT = 14                    # generous outer margins
    M_RIGHT = 14

    def _s(v, maxlen=55):
        if v is None or v == "":
            return ""
        if isinstance(v, float):
            return f"{v:,.2f}"
        return str(v)[:maxlen]

    def _kr(v):
        if v is None or v == "":
            return ""
        try:
            return f"{float(v):,.2f} kr"
        except (ValueError, TypeError):
            return ""

    class PDF(FPDF):
        font_family_name = "Helvetica"  # overridden after font registration

        # Header layout constants (mm)
        _LOGO_MAX_W = 50   # max logo width
        _LOGO_MAX_H = 14   # max logo height
        _HEADER_TOP = 10   # top margin for logo
        _LOGO_TITLE_GAP = 3  # space between logo and title
        _TITLE_H = 8       # title cell height
        _DIVIDER_GAP = 3   # space between title and divider
        _CONTENT_GAP = 4   # space below divider before content

        def header(self):
            f = self.font_family_name
            y = self._HEADER_TOP

            # ── Logo block ──
            logo_h = self._render_logo(y)

            # ── Title block ── below logo with spacing
            title_y = y + logo_h + self._LOGO_TITLE_GAP
            self.set_xy(M_LEFT, title_y)
            self.set_font(f, "B", 20)
            self.set_text_color(*C_DARK)
            self.cell(0, self._TITLE_H, "Prissammenligning")

            # Date right-aligned on same baseline as title
            self.set_font(f, "", 9)
            self.set_text_color(*C_SECONDARY)
            self.set_xy(self.w - M_RIGHT - 60, title_y + 2)
            self.cell(60, 5, today, align="R")

            # ── Divider ── thin line below title
            divider_y = title_y + self._TITLE_H + self._DIVIDER_GAP
            self.set_draw_color(*C_BORDER)
            self.set_line_width(0.3)
            self.line(M_LEFT, divider_y, self.w - M_RIGHT, divider_y)

            # ── Content starts here
            self.set_y(divider_y + self._CONTENT_GAP)

        def _render_logo(self, y: float) -> float:
            """Render logo constrained to max bounds. Returns actual height used."""
            if not _LOGO_PATH.exists():
                return self._render_text_logo(y)
            try:
                info = self.image(str(_LOGO_PATH), x=M_LEFT, y=y,
                                  w=self._LOGO_MAX_W, h=self._LOGO_MAX_H,
                                  keep_aspect_ratio=True)
                # info.rendered_height gives the actual height after scaling
                return getattr(info, "rendered_height", self._LOGO_MAX_H)
            except Exception:
                return self._render_text_logo(y)

        def _render_text_logo(self, y: float) -> float:
            """Fallback: render 'OneMed' text. Returns height used."""
            self.set_font(self.font_family_name, "B", 15)
            self.set_text_color(*C_PRIMARY)
            self.text(M_LEFT, y + 6, "OneMed")
            return 8

        def footer(self):
            self.set_y(-13)
            self.set_font(self.font_family_name, "", 7.5)
            self.set_text_color(*C_SECONDARY)
            self.cell(0, 6, f"Side {self.page_no()}/{{nb}}", align="C")

    pdf = PDF(orientation="L", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=18)

    # Register Unicode font (DejaVuSans) for full character support
    font_regular = _find_font("DejaVuSans.ttf")
    font_bold = _find_font("DejaVuSans-Bold.ttf")
    if not font_regular:
        raise RuntimeError(
            "DejaVuSans.ttf not found. Install fonts-dejavu-core "
            "(apt-get install fonts-dejavu-core)."
        )
    pdf.add_font("DejaVu", "", str(font_regular), uni=True)
    if font_bold:
        pdf.add_font("DejaVu", "B", str(font_bold), uni=True)
    else:
        # Fallback: use regular for bold too
        pdf.add_font("DejaVu", "B", str(font_regular), uni=True)

    FONT = "DejaVu"
    pdf.font_family_name = FONT

    pdf.add_page()

    content_w = pdf.w - M_LEFT - M_RIGHT

    # ============================================================
    # SUMMARY CARDS — 3 columns
    # ============================================================
    card_gap = 5
    card_w = (content_w - card_gap * 2) / 3
    card_h = 24
    card_y = pdf.get_y()

    def _summary_card(x, y, w, h, label, value, highlight=False):
        # Background
        if highlight:
            pdf.set_fill_color(*C_SAV_BG)
        else:
            pdf.set_fill_color(*C_PANEL)
        pdf.rect(x, y, w, h, "F")
        # Left accent stripe
        pdf.set_fill_color(*(C_SAV_TEXT if highlight else C_PRIMARY))
        pdf.rect(x, y, 1.2, h, "F")
        # Label
        pdf.set_xy(x + 6, y + 5)
        pdf.set_font(FONT, "", 8.5)
        pdf.set_text_color(*C_SECONDARY)
        pdf.cell(w - 10, 4, label)
        # Value
        pdf.set_xy(x + 6, y + 12)
        if highlight:
            pdf.set_font(FONT, "B", 16)
            pdf.set_text_color(*C_SAV_TEXT)
        else:
            pdf.set_font(FONT, "B", 14)
            pdf.set_text_color(*C_DARK)
        pdf.cell(w - 10, 7, value)

    _summary_card(M_LEFT, card_y, card_w, card_h,
                  "Totalt konkurrent", _kr(totals["total_competitor"]))
    _summary_card(M_LEFT + card_w + card_gap, card_y, card_w, card_h,
                  "Totalt OneMed", _kr(totals["total_onemed"]))

    sav = totals["total_savings"]
    sav_label = f"{_kr(sav)}  ({totals['savings_pct']}%)"
    _summary_card(M_LEFT + (card_w + card_gap) * 2, card_y, card_w, card_h,
                  "Besparelse", sav_label, highlight=(sav >= 0))

    pdf.set_y(card_y + card_h + 5)

    pdf.ln(6)

    # ============================================================
    # PRODUCT COMPARISON — card-style rows
    # ============================================================
    pdf.set_x(M_LEFT)
    pdf.set_font(FONT, "B", 12)
    pdf.set_text_color(*C_DARK)
    pdf.cell(content_w, 6, "Produktsammenligning", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    half_w = (content_w - 12) / 2  # left and right halves with center gap
    arrow_w = 12

    for i, r in enumerate(rows):
        # Check if we need a new page (need ~28mm for a row block)
        if pdf.get_y() > pdf.h - 38:
            pdf.add_page()

        row_y = pdf.get_y()

        # Row background panel
        pdf.set_fill_color(*C_PANEL)
        block_h = 20 if not show_line_prices else 24
        pdf.rect(M_LEFT, row_y, content_w, block_h, "F")

        # --- Left side: Konkurrent ---
        lx = M_LEFT + 4
        pdf.set_xy(lx, row_y + 2)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*C_SECONDARY)
        pdf.cell(30, 3, "Konkurrent")

        pdf.set_xy(lx, row_y + 5.5)
        pdf.set_font(FONT, "B", 9)
        pdf.set_text_color(*C_DARK)
        desc = _s(r.get("Beskrivelse"), 50)
        pdf.cell(half_w - 8, 4, desc)

        pdf.set_xy(lx, row_y + 10)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*C_SECONDARY)
        artnr = _s(r.get("Konkurrent Art.Nr"), 20)
        comp_qty = _s(r.get("Totale enheter Konkurrent"))
        meta_left = f"Art.nr: {artnr}" if artnr else ""
        if comp_qty:
            meta_left += f"   Antall: {comp_qty}"
        pdf.cell(half_w - 8, 3.5, meta_left)

        if show_line_prices:
            pdf.set_xy(lx, row_y + 14.5)
            pdf.set_font(FONT, "", 8)
            pdf.set_text_color(*C_DARK)
            pdf.cell(half_w - 8, 3.5, _kr(r.get("Konk. linjebeløp")))

        # --- Center arrow ---
        cx = M_LEFT + half_w + 2
        pdf.set_xy(cx, row_y + 6)
        pdf.set_font(FONT, "", 10)
        pdf.set_text_color(*C_BORDER)
        pdf.cell(arrow_w, 5, chr(0x2192), align="C")  # →

        # --- Right side: OneMed ---
        rx = M_LEFT + half_w + arrow_w
        pdf.set_xy(rx, row_y + 2)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*C_PRIMARY)
        pdf.cell(30, 3, "OneMed")

        pdf.set_xy(rx, row_y + 5.5)
        pdf.set_font(FONT, "B", 9)
        pdf.set_text_color(*C_DARK)
        our_name = _s(r.get("Vårt produktnavn"), 50)
        pdf.cell(half_w - 8, 4, our_name if our_name else "-")

        pdf.set_xy(rx, row_y + 10)
        pdf.set_font(FONT, "", 7.5)
        pdf.set_text_color(*C_SECONDARY)
        our_artnr = _s(r.get("Vårt Art.Nr"), 20)
        om_qty = _s(r.get("Totale Enheter OM"))
        our_spec = _s(r.get("Vår spesifikasjon"), 35)
        meta_right = f"Art.nr: {our_artnr}" if our_artnr else ""
        if om_qty:
            meta_right += f"   Antall: {om_qty}"
        if our_spec:
            meta_right += f"   {our_spec}"
        pdf.cell(half_w - 8, 3.5, meta_right)

        if show_line_prices:
            pdf.set_xy(rx, row_y + 14.5)
            pdf.set_font(FONT, "", 8)
            sav_row = r.get("Besparelse")
            our_price_text = _kr(r.get("Vårt linjebeløp"))
            if sav_row is not None and sav_row != "":
                try:
                    sv = float(sav_row)
                    pdf.set_text_color(*(C_SAV_TEXT if sv >= 0 else C_RED))
                    our_price_text += f"   (besparelse: {_kr(sav_row)})"
                except (ValueError, TypeError):
                    pdf.set_text_color(*C_DARK)
            else:
                pdf.set_text_color(*C_DARK)
            pdf.cell(half_w - 8, 3.5, our_price_text)

        # Move past this row block + gap
        pdf.set_y(row_y + block_h + 3)

    # ============================================================
    # BOTTOM TOTALS
    # ============================================================
    if pdf.get_y() > pdf.h - 35:
        pdf.add_page()

    pdf.ln(2)
    pdf.set_draw_color(*C_BORDER)
    pdf.set_line_width(0.3)
    pdf.line(M_LEFT, pdf.get_y(), pdf.w - M_RIGHT, pdf.get_y())
    pdf.ln(5)

    tot_x = M_LEFT
    tw = 100
    lh = 7.5

    pdf.set_x(tot_x)
    pdf.set_font(FONT, "", 9.5)
    pdf.set_text_color(*C_DARK)
    pdf.set_fill_color(*C_PANEL)
    pdf.cell(tw, lh, f"  Totalt konkurrent     {totals['total_competitor']:,.2f} kr", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(tot_x)
    pdf.cell(tw, lh, f"  Totalt OneMed          {totals['total_onemed']:,.2f} kr", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    # Savings — prominent
    pdf.set_x(tot_x)
    pdf.set_fill_color(*C_SAV_BG)
    pdf.set_text_color(*(C_SAV_TEXT if sav >= 0 else C_RED))
    pdf.set_font(FONT, "B", 11)
    pdf.cell(tw, lh + 2, f"  Besparelse                {sav:,.2f} kr  ({totals['savings_pct']}%)", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*C_DARK)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
