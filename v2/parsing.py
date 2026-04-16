"""
V2 Parsing module – file classification, text extraction, and row extraction.

Handles:
- PDF text extraction (with OCR fallback)
- Enhanced OCR with image preprocessing for scanned documents
- XLSX detection and inspection
- NorEngros invoice parsing with full field extraction
- Generic product line parsing for unknown sources (price optional)
- Page-level resilience and quality scoring
- Confidence scoring per extracted row
"""
import logging
import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from pypdf import PdfReader

logger = logging.getLogger(__name__)


# ============================================================
# QUALITY SCORING CONSTANTS
# ============================================================

# Minimum thresholds for text quality assessment
_OCR_MIN_CHARS = int(os.getenv("OCR_MIN_CHARS", "80"))
_OCR_MIN_WORDS = int(os.getenv("OCR_MIN_WORDS", "15"))

# Pattern for lines that look like product lines (item number + text)
_PRODUCT_LINE_HINT_RE = re.compile(r"\b\d{4,9}\b.*[A-Za-zÆØÅæøå]{3,}")

# Noise patterns to filter out during generic parsing
_NOISE_PATTERNS = [
    re.compile(r"^\s*side\s+\d+", re.IGNORECASE),
    re.compile(r"^\s*\d+\s*$"),  # bare page numbers
    re.compile(r"^\s*(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+\d{4}\s*$", re.IGNORECASE),
    re.compile(r"^\s*dato\s*:", re.IGNORECASE),
    re.compile(r"^\s*legekontor\s+\d+", re.IGNORECASE),
    re.compile(r"^\s*(lab|beskyttelse|sårbehandling|diagnostikk|utstyr|forbruk|hygiene)\s*$", re.IGNORECASE),
    # Common document headers/titles
    re.compile(r"^\s*(vareliste|faktura|prisliste|bestilling|ordre|følgeseddel)\b.*\d{4}", re.IGNORECASE),
    re.compile(r"^\s*(vareliste|faktura|prisliste|bestilling|ordre|følgeseddel)\s*$", re.IGNORECASE),
]

# ============================================================
# FILE TYPE DETECTION
# ============================================================

def is_pdf_bytes(b: bytes) -> bool:
    return bool(b) and b[:4] == b"%PDF"


def is_xlsx_bytes(b: bytes) -> bool:
    return bool(b) and b[:2] == b"PK"


def classify_file(filename: str, content: bytes) -> str:
    """Return 'pdf', 'xlsx', or 'unknown'."""
    if is_pdf_bytes(content) or filename.lower().endswith(".pdf"):
        return "pdf"
    if is_xlsx_bytes(content) or filename.lower().endswith(".xlsx"):
        return "xlsx"
    return "unknown"


# ============================================================
# NUMBER PARSING
# ============================================================

def _parse_money(x: str) -> Optional[float]:
    """Parse Norwegian number format: 1.234,56 / 1234,56 / 1234.56"""
    if not x:
        return None
    s = str(x).strip().replace("\u00a0", " ").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() == "nan":
            return None
        s = s.replace(" ", "").replace(",", ".")
        return float(s)
    except Exception:
        return None


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    parts = []
    for p in reader.pages:
        try:
            t = p.extract_text() or ""
        except Exception:
            t = ""
        if t:
            parts.append(t)
    return "\n".join(parts)


def needs_ocr(text: str) -> bool:
    """Check if extracted text is too poor and OCR is needed."""
    if not text or not text.strip():
        return True
    stripped = text.strip()
    if len(stripped) < _OCR_MIN_CHARS:
        return True
    words = [w for w in re.split(r"\s+", stripped) if len(w) >= 2]
    if len(words) < _OCR_MIN_WORDS:
        return True
    return False


def assess_text_quality(text: str) -> Dict[str, Any]:
    """Assess quality of extracted text for logging and decision-making.

    Returns dict with quality metrics:
      - char_count, word_count, line_count
      - product_line_hints: number of lines that look like product lines
      - quality_score: 0.0-1.0 overall quality estimate
      - verdict: 'good', 'poor', 'empty'
    """
    if not text or not text.strip():
        return {"char_count": 0, "word_count": 0, "line_count": 0,
                "product_line_hints": 0, "quality_score": 0.0, "verdict": "empty"}

    stripped = text.strip()
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    words = [w for w in re.split(r"\s+", stripped) if len(w) >= 2]
    product_hints = sum(1 for l in lines if _PRODUCT_LINE_HINT_RE.search(l))

    # Score based on multiple factors
    score = 0.0
    if len(stripped) >= _OCR_MIN_CHARS:
        score += 0.2
    if len(words) >= _OCR_MIN_WORDS:
        score += 0.2
    if len(lines) >= 5:
        score += 0.2
    if product_hints >= 2:
        score += 0.3
    # Bonus for digit+text lines (typical for product lists)
    digit_text_lines = sum(1 for l in lines if re.search(r"\d", l) and re.search(r"[A-Za-zÆØÅæøå]", l))
    if digit_text_lines >= 3:
        score += 0.1

    score = min(score, 1.0)
    verdict = "good" if score >= 0.5 else ("poor" if score > 0 else "empty")

    return {
        "char_count": len(stripped),
        "word_count": len(words),
        "line_count": len(lines),
        "product_line_hints": product_hints,
        "quality_score": round(score, 2),
        "verdict": verdict,
    }


def _preprocess_image_for_ocr(img):
    """Apply image preprocessing to improve OCR quality.

    Steps: grayscale, moderate contrast enhancement, sharpen.
    NOTE: No binary thresholding — it destroys anti-aliased text from
    browser-to-PDF printouts and reduces Tesseract accuracy.
    """
    from PIL import ImageEnhance, ImageFilter

    # Convert to grayscale
    if img.mode != "L":
        img = img.convert("L")

    # Moderate contrast enhancement (1.8 was too aggressive with thresholding)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.4)

    # Sharpen slightly to help with scanned text edges
    img = img.filter(ImageFilter.SHARPEN)

    return img


def ocr_pdf_fallback(pdf_bytes: bytes) -> str:
    """Legacy compatibility wrapper. Returns concatenated text from all pages."""
    page_results = ocr_pdf_pages(pdf_bytes)
    return "\n".join(pr["text"] for pr in page_results if pr.get("text"))


def ocr_pdf_pages(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """OCR each page of a PDF individually with preprocessing.

    Returns a list of per-page results:
      [{"page": 1, "text": "...", "char_count": N, "quality": "good"/"poor"/"empty", "error": None}, ...]

    Page-level resilience: if one page fails, others still process.
    """
    import fitz
    import pytesseract
    from PIL import Image
    from io import BytesIO as _BytesIO

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_results = []

    try:
        for page_num, page in enumerate(doc):
            page_result = {"page": page_num + 1, "text": "", "char_count": 0,
                           "quality": "empty", "error": None}
            try:
                # Render at ~300 DPI for Tesseract (72 * 4.17 ≈ 300)
                mat = fitz.Matrix(4.17, 4.17)
                pix = page.get_pixmap(matrix=mat)
                img = Image.open(_BytesIO(pix.tobytes("png")))

                # Preprocess image for better OCR
                img_processed = _preprocess_image_for_ocr(img)

                # Run OCR with Norwegian + English
                text = pytesseract.image_to_string(img_processed, lang="nor+eng")

                if text and text.strip():
                    page_result["text"] = text.strip()
                    page_result["char_count"] = len(text.strip())

                    # Quick quality check
                    words = [w for w in text.split() if len(w) >= 2]
                    has_products = bool(_PRODUCT_LINE_HINT_RE.search(text))
                    if len(words) >= 5 and has_products:
                        page_result["quality"] = "good"
                    elif len(words) >= 3:
                        page_result["quality"] = "poor"

                logger.debug(
                    f"OCR side {page_num + 1}: {page_result['char_count']} tegn, "
                    f"kvalitet={page_result['quality']}"
                )

            except Exception as e:
                page_result["error"] = str(e)
                logger.warning(f"OCR feilet for side {page_num + 1}: {e}")

            page_results.append(page_result)
    finally:
        doc.close()

    return page_results


# ============================================================
# XLSX INSPECTION
# ============================================================

def inspect_xlsx(content: bytes) -> Dict[str, Any]:
    """Read basic metadata from an XLSX file without full parsing."""
    try:
        xls = pd.ExcelFile(BytesIO(content))
        sheets = xls.sheet_names
        total_rows = 0
        sheet_info = []
        for s in sheets:
            df = pd.read_excel(xls, sheet_name=s, nrows=0)
            nrows = len(pd.read_excel(xls, sheet_name=s))
            sheet_info.append({"name": s, "columns": len(df.columns), "rows": nrows})
            total_rows += nrows
        return {
            "sheets": sheet_info,
            "total_rows": total_rows,
        }
    except Exception as e:
        raise ValueError(f"Kunne ikke lese XLSX: {e}")


# ============================================================
# NORENGROS PDF PARSER
# ============================================================

def _looks_like_total(line_low: str) -> bool:
    return any(k in line_low for k in [
        "sum", "total", "mva", "å betale", "beløp å betale", "til betaling",
        "delsum", "fakturasum", "sum eks", "sum inkl"
    ])


def _extract_packaging_count(packaging_text: str) -> int:
    """
    Extract numeric packaging count from packaging text.

    Examples:
        "á 25 STK" → 25
        "25 STK"   → 25
        "à 2 RLL"  → 2
        "á 100 PK" → 100
        ""         → 1  (default: 1 unit per package)
    """
    if not packaging_text:
        return 1
    m = re.search(r"(\d+)", packaging_text)
    if m:
        val = int(m.group(1))
        return val if val > 0 else 1
    return 1


# NorEngros line format (example):
#   123456  PRODUKT NAVN ...  2,00  PK  á 25 STK  199,00  STK  5,0 %  378,10
#
# Groups:
#   art:          123456
#   desc:         PRODUKT NAVN ...
#   qty:          2,00          (Kvt. = how many packages purchased)
#   unit:         PK            (purchase unit)
#   packaging:    á 25 STK      (optional: items per package)
#   price:        199,00        (list price per price_unit)
#   price_unit:   STK           (price unit)
#   rab:          5,0           (discount %)
#   amount:       378,10        (line total)
_NORENGROS_LINE_RE = re.compile(
    r"^(?P<art>\d{5,9})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<qty>\d+,\d{2})\s+"
    r"(?P<unit>[A-ZÆØÅ]{1,6})"
    r"(?:\s+(?P<packaging>á\s*\d+\s+[A-ZÆØÅ]{1,6}|à\s*\d+\s+[A-ZÆØÅ]{1,6}))?\s+"
    r"(?P<price>\d[\d\.]*,\d{2})\s+"
    r"(?P<price_unit>[A-ZÆØÅ]{1,6})"
    r"(?:\s+(?P<rab>\d+,\d)\s*%\s+(?P<amount>\d[\d\.]*,\d{2}))?"
    r".*$"
)


def parse_norengros_text(text: str, source_file: str) -> List[Dict[str, Any]]:
    """
    Parse NorEngros invoice text into structured V2 rows.

    Returns list of row dicts with all raw + calculated fields.
    Rows that cannot be parsed are skipped (not errors).
    """
    lines = [re.sub(r"\s+", " ", (line or "")).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    rows: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if _looks_like_total(line.lower()):
            i += 1
            continue

        if re.match(r"^\d{5,9}\b", line):
            buf = line
            # Join continuation lines (description split across lines)
            while (i + 1) < len(lines):
                if re.search(r"\b\d+,\d{2}\b", buf):
                    break
                nxt = lines[i + 1]
                if _looks_like_total(nxt.lower()):
                    i += 1
                    continue
                if re.match(r"^\d{5,9}\b", nxt):
                    break
                buf = (buf + " " + nxt).strip()
                i += 1

            m = _NORENGROS_LINE_RE.match(buf)
            if m:
                row = _build_norengros_row(m, source_file)
                if row:
                    rows.append(row)

        i += 1

    return rows


def _build_norengros_row(m: re.Match, source_file: str) -> Optional[Dict[str, Any]]:
    """Build a V2 parsed row from a NorEngros regex match."""
    art = (m.group("art") or "").strip()
    desc = (m.group("desc") or "").strip()

    if not desc and not art:
        return None

    # Kvt. = quantity purchased (how many packages bought)
    quantity_purchased = _to_float(m.group("qty")) or 0.0
    unit = (m.group("unit") or "").strip()

    # Packaging: "á 25 STK" → packaging_text + packaging_count
    packaging_raw = (m.group("packaging") or "").strip()
    packaging_count = _extract_packaging_count(packaging_raw)

    # Price = list price per price_unit (before discount)
    price_before_discount = _parse_money(m.group("price"))
    price_unit = (m.group("price_unit") or "").strip()

    # Discount
    rab_raw = m.groupdict().get("rab")
    discount_pct = _to_float(rab_raw) if rab_raw else None

    # Line amount (total from invoice)
    amount_raw = m.groupdict().get("amount")
    line_amount = _parse_money(amount_raw) if amount_raw else None

    # === calculated_unit_price (support field) ===
    # Price per individual unit, derived from NorEngros price fields.
    # Useful for reference but NOT the primary comparison basis.
    calculated_unit_price = None
    if price_before_discount is not None:
        discount_factor = 1.0
        if discount_pct is not None:
            discount_factor = 1.0 - (discount_pct / 100.0)
        calculated_unit_price = round(
            price_before_discount * discount_factor / packaging_count,
            4,
        )

    # === total_units ===
    # Total individual units purchased across all packages.
    #   total_units = quantity_purchased (Kvt.) × packaging_count
    # Example: Kvt.=4, packaging="á 30 STK" → 4 × 30 = 120 units
    total_units = quantity_purchased * packaging_count

    # === competitor_line_amount ===
    # The authoritative NorEngros line total (Beløp from invoice).
    # This is the ground truth for what the competitor charges.
    # If line_amount is missing, fall back to calculation from fields.
    competitor_line_amount = line_amount
    if competitor_line_amount is None and price_before_discount is not None:
        discount_factor = 1.0
        if discount_pct is not None:
            discount_factor = 1.0 - (discount_pct / 100.0)
        competitor_line_amount = round(
            price_before_discount * discount_factor * quantity_purchased,
            2,
        )

    # === Comparison fields (populated during matching) ===
    # our_unit_price: our catalog price per unit (set during matching)
    # our_comparable_line_price: total_units × our_unit_price
    # savings_amount: competitor_line_amount - our_comparable_line_price

    return {
        "source_file": source_file,
        "competitor": "NorEngros",
        "competitor_artnr": art,
        "description": desc,
        "quantity_purchased": quantity_purchased,
        "packaging_text": packaging_raw,
        "packaging_count": packaging_count,
        "unit": unit,
        "price_unit": price_unit,
        "price_before_discount": price_before_discount,
        "discount_pct": discount_pct,
        "line_amount": line_amount,
        "calculated_unit_price": calculated_unit_price,
        "total_units": total_units,
        "competitor_line_amount": competitor_line_amount,
        # Placeholders — set during matching step:
        "our_unit_price": None,
        "our_comparable_line_price": None,
        "savings_amount": None,
    }


# ============================================================
# EPION ORDER CONFIRMATION PARSER
# ============================================================
# Epion order confirmations (from epion.no/ordrer/XXXXX) display
# products in a card layout:
#
#   Product Name
#   NN stk. pr. pakke       (optional packaging info)
#   N/N sendt               (delivery status)
#   unit_price,-   X qty   total,-
#
# Price format uses ",-" suffix (no øre): "1 635,-", "60,-"
# The summary section has "Sum eks. mva", "Frakt", "MVA", "Totalpris".
#
# IMPORTANT: Epion's web pages use × (multiplication sign U+00D7) for
# quantity, not the letter X. PDF extraction may also produce various
# dash characters (en dash, em dash, minus sign) for the ",-" suffix.

# Unicode-safe character classes for Epion patterns
_DASH_CHARS = r"\-\–\—\−"           # hyphen, en-dash, em-dash, minus sign
_MULT_CHARS = r"Xx×✕"               # letter X, lowercase x, multiplication sign, heavy mult X

# Price+qty+total on one line: "835,- X 5 4 175,-"
_EPION_PRICE_QTY_TOTAL_RE = re.compile(
    r"(\d[\d\s.]*)[,.]([" + _DASH_CHARS + r"])\s*[" + _MULT_CHARS + r"]\s*(\d+)\s+(\d[\d\s.]*)[,.]([" + _DASH_CHARS + r"])"
)

# Just a standalone price: "835,-" or "1 317,-"
_EPION_PRICE_ONLY_RE = re.compile(
    r"^(\d[\d\s.]*)[,.]([" + _DASH_CHARS + r"])\s*$"
)

# Just a quantity: "X 5", "× 10", "x 3"
_EPION_QTY_ONLY_RE = re.compile(
    r"^[" + _MULT_CHARS + r"]\s*(\d+)\s*$"
)

# Packaging info: "100 stk. pr. pakke", "2 rull pr. pakke", "100 pr. pakke"
_EPION_PACK_LINE_RE = re.compile(
    r"(\d+)\s+(?:(?:stk|rull|pk)\.?\s+)?pr\.?\s*pakke", re.IGNORECASE
)

# Delivery status: "N/N sendt" — flexible (may have surrounding text)
_EPION_DELIVERY_RE = re.compile(r"\d+/\d+\s+sendt", re.IGNORECASE)

# Packaging in product name parentheses: "(100 stk)", "(15 stk)", "(200 stk)"
_EPION_PACK_IN_NAME_RE = re.compile(r"\((\d+)\s*(stk|pk|rull)\)", re.IGNORECASE)

# Keywords marking lines to skip (metadata, navigation, summaries)
_EPION_SKIP_KEYWORDS = [
    "ordre #", "fullført", "ordr.dato", "sist oppdatert", "varer sendt",
    "bestilt av", "bestilt:", "faktura og sending", "faktura for",
    "totaloversikt", "alle varer", "delleveranse", "bruksanvisning",
    "epion faqs", "brosjyre", "https://epion", "epion.no",
]

# Summary-section keywords (separate from product-level skip)
_EPION_SUMMARY_KEYWORDS = ["sum eks", "totalpris ink"]


# Date/timestamp lines: "14.04.2026, 13:34" or "14.04.2026"
_EPION_DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}")
# Page numbers: "2/4", "3/4", "1/4" at end of line, or "Side N"
_EPION_PAGENO_RE = re.compile(r"^\d+/\d+\s*$|^\s*side\s+\d+", re.IGNORECASE)


def _epion_should_skip(line_low: str) -> bool:
    """Check if a line is Epion metadata/navigation that should be skipped."""
    if any(kw in line_low for kw in _EPION_SKIP_KEYWORDS):
        return True
    if _EPION_DATE_RE.match(line_low):
        return True
    if _EPION_PAGENO_RE.match(line_low):
        return True
    return False


def _epion_is_summary(line_low: str) -> bool:
    """Check if a line starts the summary section (Sum eks. mva, Totalpris)."""
    return any(kw in line_low for kw in _EPION_SUMMARY_KEYWORDS)


def _epion_is_price_only(line: str) -> bool:
    """Check if a line is just a standalone price like '835,-' or '1 317,-'."""
    return bool(_EPION_PRICE_ONLY_RE.match(line.strip()))


def _epion_is_qty_only(line: str) -> bool:
    """Check if a line is just a quantity like 'X 5' or '× 10'."""
    return bool(_EPION_QTY_ONLY_RE.match(line.strip()))


def _epion_extract_price(text: str) -> Optional[float]:
    """Extract a price value from text like '835,-' or '1 317,-'."""
    m = re.search(r"(\d[\d\s.]*)[,.][" + _DASH_CHARS + r"]", text)
    if m:
        raw = m.group(1).replace(" ", "").replace(".", "")
        return _to_float(raw)
    return None


def _epion_extract_qty(text: str) -> Optional[int]:
    """Extract quantity from text like 'X 5' or '× 10'."""
    m = re.search(r"[" + _MULT_CHARS + r"]\s*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def parse_epion_text(text: str, source_file: str) -> List[Dict[str, Any]]:
    """Parse Epion order confirmation text into structured V2 rows.

    Uses two strategies:
    1. Primary: Anchor on "price,- × qty total,-" lines (handles multi-line splits)
    2. Fallback: Anchor on "N/N sendt" delivery status lines and find prices nearby

    Returns list of row dicts in the standard V2 format.
    """
    raw_lines = [re.sub(r"\s+", " ", (l or "")).strip() for l in text.splitlines()]
    raw_lines = [l for l in raw_lines if l]

    rows = _epion_strategy_price_anchor(raw_lines, source_file)

    # Fallback: use delivery status as anchor if price-line strategy found nothing
    if not rows:
        rows = _epion_strategy_delivery_anchor(raw_lines, source_file)

    return rows


def _epion_strategy_price_anchor(
    raw_lines: List[str], source_file: str
) -> List[Dict[str, Any]]:
    """Strategy 1: Find products by anchoring on price+qty+total lines."""

    # --- Phase 1: Merge multi-line price patterns ---
    lines: List[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # Try 3-line merge: "price,-" + "X qty" + "total,-"
        if _epion_is_price_only(line) and i + 2 < len(raw_lines):
            next1 = raw_lines[i + 1]
            next2 = raw_lines[i + 2]
            if _epion_is_qty_only(next1) and _epion_is_price_only(next2):
                lines.append(f"{line} {next1} {next2}")
                i += 3
                continue

        # Try 2-line merge in both directions
        if i + 1 < len(raw_lines):
            combined = f"{line} {raw_lines[i + 1]}"
            if _EPION_PRICE_QTY_TOTAL_RE.search(combined):
                lines.append(combined)
                i += 2
                continue

        lines.append(line)
        i += 1

    # --- Phase 2: Find product blocks anchored by price lines ---
    rows: List[Dict[str, Any]] = []
    price_indices: List[int] = []

    for idx, line in enumerate(lines):
        if _EPION_PRICE_QTY_TOTAL_RE.search(line):
            price_indices.append(idx)

    for pi in price_indices:
        m = _EPION_PRICE_QTY_TOTAL_RE.search(lines[pi])
        if not m:
            continue

        # Parse price values (groups: 1=unit_price, 2=dash, 3=qty, 4=total, 5=dash)
        unit_price_raw = m.group(1).replace(" ", "").replace(".", "")
        qty = int(m.group(3))
        total_raw = m.group(4).replace(" ", "").replace(".", "")

        unit_price = _to_float(unit_price_raw)
        total = _to_float(total_raw)

        desc, packaging_text, packaging_count = _epion_lookback_description(
            lines, pi
        )

        # Use packaging text or a placeholder if description is empty
        # (happens when product name is handwritten and OCR fails)
        if not desc and packaging_text:
            desc = packaging_text
        if not desc:
            desc = f"Ukjent produkt (linje: {lines[pi][:40]})"

        rows.append(_build_epion_row(
            desc, qty, unit_price, total, packaging_text, packaging_count,
            source_file, lines[pi],
        ))

    return rows


def _epion_strategy_delivery_anchor(
    raw_lines: List[str], source_file: str
) -> List[Dict[str, Any]]:
    """Strategy 2: Find products by anchoring on 'N/N sendt' delivery status lines.

    Each product block in Epion order confirmations has a delivery status line.
    We find these, then look above for the product name/packaging and below
    for the price information.
    """
    rows: List[Dict[str, Any]] = []

    for i, line in enumerate(raw_lines):
        if not _EPION_DELIVERY_RE.search(line):
            continue

        # Stop if we've reached the summary section
        if _epion_is_summary(line.lower()):
            break

        # --- Look below for price and quantity ---
        unit_price = None
        qty = None
        total = None

        # Check remaining text on the delivery status line itself
        # (sometimes price is on the same line)
        pqt_m = _EPION_PRICE_QTY_TOTAL_RE.search(line)
        if pqt_m:
            unit_price_raw = pqt_m.group(1).replace(" ", "").replace(".", "")
            qty = int(pqt_m.group(3))
            total_raw = pqt_m.group(4).replace(" ", "").replace(".", "")
            unit_price = _to_float(unit_price_raw)
            total = _to_float(total_raw)
        else:
            # Look at lines below for price info (within 4 lines)
            prices_found: List[float] = []
            for k in range(i + 1, min(i + 5, len(raw_lines))):
                below = raw_lines[k]
                below_low = below.lower()

                # Stop at next delivery status or summary
                if _EPION_DELIVERY_RE.search(below) or _epion_is_summary(below_low):
                    break

                # Check for full price+qty+total line
                pqt_m2 = _EPION_PRICE_QTY_TOTAL_RE.search(below)
                if pqt_m2:
                    unit_price_raw = pqt_m2.group(1).replace(" ", "").replace(".", "")
                    qty = int(pqt_m2.group(3))
                    total_raw = pqt_m2.group(4).replace(" ", "").replace(".", "")
                    unit_price = _to_float(unit_price_raw)
                    total = _to_float(total_raw)
                    break

                # Check for standalone components
                p = _epion_extract_price(below)
                if p is not None:
                    prices_found.append(p)
                q = _epion_extract_qty(below)
                if q is not None:
                    qty = q

            # If we found standalone prices but no combined line
            if unit_price is None and prices_found and qty is not None:
                if len(prices_found) >= 2:
                    # First price = unit price, last = total
                    unit_price = prices_found[0]
                    total = prices_found[-1]
                elif len(prices_found) == 1:
                    unit_price = prices_found[0]
                    total = prices_found[0] * qty if qty else None

        if qty is None:
            continue

        # --- Look above for product name and packaging ---
        desc_parts: List[str] = []
        packaging_text = ""
        packaging_count = 1

        j = i - 1
        lookback = 0
        while j >= 0 and lookback < 10:
            prev = raw_lines[j].strip()
            prev_low = prev.lower()

            # Stop at another delivery status (start of previous product)
            if _EPION_DELIVERY_RE.search(prev):
                break
            # Stop at a price line (from previous product)
            if _EPION_PRICE_QTY_TOTAL_RE.search(prev):
                break
            # Stop at standalone price
            if _epion_is_price_only(prev):
                break

            # Skip metadata/noise
            if _epion_should_skip(prev_low) or _epion_is_summary(prev_low):
                j -= 1
                lookback += 1
                continue

            # Check for packaging info
            pack_m = _EPION_PACK_LINE_RE.search(prev)
            if pack_m and packaging_count == 1:
                packaging_count = int(pack_m.group(1)) or 1
                packaging_text = prev
                j -= 1
                lookback += 1
                continue

            # This is likely a description line (take up to 2)
            if len(prev) >= 3 and len(desc_parts) < 2:
                desc_parts.insert(0, prev)

            j -= 1
            lookback += 1

        description = " ".join(desc_parts).strip()

        # Use packaging text or placeholder if description is empty
        if not description and packaging_text:
            description = packaging_text
        if not description:
            description = f"Ukjent produkt (linje: {raw_lines[i][:40]})"

        # Extract packaging from description parentheses
        if packaging_count == 1:
            pack_in_name = _EPION_PACK_IN_NAME_RE.search(description)
            if pack_in_name:
                packaging_count = int(pack_in_name.group(1)) or 1

        rows.append(_build_epion_row(
            description, qty, unit_price, total, packaging_text, packaging_count,
            source_file, raw_lines[i],
        ))

    return rows


def _epion_lookback_description(
    lines: List[str], price_idx: int
) -> tuple:
    """Look backwards from a price line to find the product description and packaging."""
    desc_parts: List[str] = []
    packaging_text = ""
    packaging_count = 1

    j = price_idx - 1
    lookback = 0
    while j >= 0 and lookback < 10:
        prev = lines[j].strip()
        prev_low = prev.lower()

        # Stop at another price line (start of previous product)
        if _EPION_PRICE_QTY_TOTAL_RE.search(prev):
            break
        # Stop at a standalone price (from previous product's split lines)
        if _epion_is_price_only(prev):
            break

        # Skip metadata/noise lines
        if _epion_should_skip(prev_low) or _epion_is_summary(prev_low):
            j -= 1
            lookback += 1
            continue

        # Skip delivery status lines (e.g. "5/5 sendt")
        if re.match(r"^\d+/\d+\s+sendt\s*$", prev, re.IGNORECASE):
            j -= 1
            lookback += 1
            continue

        # Check for packaging info line (e.g. "100 stk. pr. pakke")
        pack_m = _EPION_PACK_LINE_RE.search(prev)
        if pack_m and packaging_count == 1:
            packaging_count = int(pack_m.group(1)) or 1
            packaging_text = prev
            j -= 1
            lookback += 1
            continue

        # This is likely a description line (take up to 2)
        if len(prev) >= 3 and len(desc_parts) < 2:
            desc_parts.insert(0, prev)

        j -= 1
        lookback += 1

    description = " ".join(desc_parts).strip()

    # Extract packaging from description parentheses if not found separately
    if packaging_count == 1 and description:
        pack_in_name = _EPION_PACK_IN_NAME_RE.search(description)
        if pack_in_name:
            packaging_count = int(pack_in_name.group(1)) or 1

    return description, packaging_text, packaging_count


def _build_epion_row(
    description: str, qty: int, unit_price: Optional[float],
    total: Optional[float], packaging_text: str, packaging_count: int,
    source_file: str, raw_text: str,
) -> Dict[str, Any]:
    """Build a V2 row dict from parsed Epion product fields."""
    total_units = float(qty * packaging_count)
    competitor_line_amount = total if total else (
        round(unit_price * qty, 2) if unit_price else None
    )
    calculated_unit_price = (
        round(unit_price / packaging_count, 4)
        if unit_price and packaging_count > 0 else unit_price
    )

    return {
        "source_file": source_file,
        "competitor": "Epion",
        "competitor_artnr": "",
        "description": description,
        "quantity_purchased": float(qty),
        "packaging_text": packaging_text,
        "packaging_count": packaging_count,
        "unit": "",
        "price_unit": "",
        "price_before_discount": unit_price,
        "discount_pct": None,
        "line_amount": total,
        "calculated_unit_price": calculated_unit_price,
        "total_units": total_units,
        "competitor_line_amount": competitor_line_amount,
        "our_unit_price": None,
        "our_comparable_line_price": None,
        "savings_amount": None,
        "source_page": 0,
        "raw_text": raw_text,
        "confidence": 0.85,
        "parse_method": "epion",
    }


# ============================================================
# GENERIC PRODUCT LINE PARSER (for scanned / unknown sources)
# ============================================================
# Handles product lists where lines may contain:
#   item_number  description  (optional packaging)  (optional price)
# Price is NEVER required. This parser works for scanned PDFs
# with or without price information.

# Regex for a line starting with what looks like an item number (4-9 digits)
_GENERIC_ITEM_START_RE = re.compile(r"^(\d{4,9})\s+(.+)")

# Regex for packaging info in parentheses: "(15 STK)", "(50 stk)", "(25 pk)" etc.
_PACKAGING_RE = re.compile(r"\((\d+)\s*([A-Za-zÆØÅæøå]{1,6})\)")

# Regex for a price at end of line: "199,00" or "1.234,56" or "199.00"
_TRAILING_PRICE_RE = re.compile(
    r"\s+(\d[\d\.]*[,\.]\d{2})\s*$"
)

# Lines that are clearly noise (category headers, page info, etc.)
def _is_noise_line(line: str) -> bool:
    """Check if a line is noise (headers, page numbers, dates, categories)."""
    stripped = line.strip()
    if not stripped:
        return True
    if len(stripped) < 3:
        return True
    for pat in _NOISE_PATTERNS:
        if pat.search(stripped):
            return True
    # Lines that are all uppercase and short are likely headers
    # But not if they look like packaging info "(15 STK)" or contain digits+unit pattern
    if stripped.isupper() and len(stripped) < 30 and not re.search(r"\d{4,}", stripped):
        if not re.search(r"\d+\s*[A-ZÆØÅ]{1,6}", stripped):
            return True
    return False


def _is_continuation_line(line: str) -> bool:
    """Check if a line looks like it continues a previous product line.

    A continuation line does NOT start with a new item number and contains
    meaningful text (packaging info, more description, etc.)
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Starts with an item number -> not a continuation
    if re.match(r"^\d{4,9}\s", stripped):
        return False
    # Contains packaging pattern like "(15 STK)" - check early, before noise filter
    if _PACKAGING_RE.search(stripped):
        return True
    # Looks like noise
    if _is_noise_line(stripped):
        return False
    # Contains some alphabetic text
    if re.search(r"[A-Za-zÆØÅæøå]{2,}", stripped):
        return True
    return False


def parse_generic_product_lines(text: str, source_file: str,
                                page_texts: Optional[List[Dict[str, Any]]] = None
                                ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse generic product lines from OCR or text output.

    This is the fallback parser when NorEngros format is not detected.
    It extracts product lines based on item number + description patterns.
    Price is completely optional.

    Args:
        text: Full extracted text
        source_file: Source filename
        page_texts: Optional per-page OCR results for source_page tracking

    Returns:
        Tuple of (rows, stats) where:
        - rows: list of parsed row dicts
        - stats: dict with parsing statistics for logging/UI
    """
    stats = {
        "total_lines": 0,
        "noise_lines_skipped": 0,
        "candidate_lines": 0,
        "continuation_lines_merged": 0,
        "rows_with_price": 0,
        "rows_without_price": 0,
        "rows_with_item_no": 0,
        "rows_without_item_no": 0,
        "discard_reasons": {},
    }

    # Build page map for source_page tracking
    page_map: Dict[int, int] = {}  # line_index -> page_number
    if page_texts:
        global_line_idx = 0
        for pr in page_texts:
            page_num = pr.get("page", 0)
            page_text = pr.get("text", "")
            for _ in page_text.splitlines():
                page_map[global_line_idx] = page_num
                global_line_idx += 1

    all_lines = text.splitlines()
    stats["total_lines"] = len(all_lines)

    # Clean lines
    clean_lines = []
    for i, raw_line in enumerate(all_lines):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if _is_noise_line(line):
            stats["noise_lines_skipped"] += 1
            continue
        if _looks_like_total(line.lower()):
            stats["noise_lines_skipped"] += 1
            continue
        clean_lines.append((i, line))  # (original_index, cleaned_line)

    # Group lines into product entries
    entries: List[Dict[str, Any]] = []
    current_entry = None

    for orig_idx, line in clean_lines:
        item_match = _GENERIC_ITEM_START_RE.match(line)

        if item_match:
            # Save previous entry
            if current_entry:
                entries.append(current_entry)

            stats["candidate_lines"] += 1
            current_entry = {
                "item_no": item_match.group(1),
                "raw_text": line,
                "rest": item_match.group(2),
                "source_page": page_map.get(orig_idx, 0),
                "orig_idx": orig_idx,
            }
        elif current_entry and _is_continuation_line(line):
            # Merge continuation into current entry
            current_entry["raw_text"] += " " + line
            current_entry["rest"] += " " + line
            stats["continuation_lines_merged"] += 1
        elif not item_match and re.search(r"[A-Za-zÆØÅæøå]{3,}", line):
            # Line with text but no item number - could be a description-only product
            # Only keep if it has enough substance
            words = [w for w in line.split() if len(w) >= 2]
            if len(words) >= 2 and not _is_noise_line(line):
                if current_entry:
                    entries.append(current_entry)
                current_entry = {
                    "item_no": "",
                    "raw_text": line,
                    "rest": line,
                    "source_page": page_map.get(orig_idx, 0),
                    "orig_idx": orig_idx,
                }

    # Don't forget last entry
    if current_entry:
        entries.append(current_entry)

    # Build structured rows from entries
    rows: List[Dict[str, Any]] = []

    for entry in entries:
        rest = entry["rest"].strip()
        item_no = entry["item_no"]

        # Extract packaging info from parentheses
        pack_info = ""
        packaging_count = 1
        pack_match = _PACKAGING_RE.search(rest)
        if pack_match:
            packaging_count = int(pack_match.group(1)) or 1
            pack_unit = pack_match.group(2).upper()
            pack_info = f"{packaging_count} {pack_unit}"
            # Remove packaging from description
            rest = rest[:pack_match.start()] + rest[pack_match.end():]
            rest = re.sub(r"\s+", " ", rest).strip()

        # Try to extract trailing price
        price = None
        price_match = _TRAILING_PRICE_RE.search(rest)
        if price_match:
            price = _parse_money(price_match.group(1))
            if price is not None:
                rest = rest[:price_match.start()].strip()

        # What remains is the description
        description = rest.strip()
        # Clean up stray punctuation at end
        description = re.sub(r"[\s,;:]+$", "", description)

        # Skip if we have neither item_no nor meaningful description
        if not item_no and len(description) < 4:
            reason = "no_item_no_and_short_desc"
            stats["discard_reasons"][reason] = stats["discard_reasons"].get(reason, 0) + 1
            continue

        # Calculate confidence
        confidence = _calculate_confidence(item_no, description, pack_info, price)

        # Skip very low confidence rows (likely OCR garbage)
        if confidence < 0.2 and not item_no:
            reason = "low_confidence_no_item"
            stats["discard_reasons"][reason] = stats["discard_reasons"].get(reason, 0) + 1
            continue

        # Track stats
        if price is not None:
            stats["rows_with_price"] += 1
        else:
            stats["rows_without_price"] += 1
        if item_no:
            stats["rows_with_item_no"] += 1
        else:
            stats["rows_without_item_no"] += 1

        row = {
            "source_file": source_file,
            "competitor": "",
            "competitor_artnr": item_no,
            "description": description,
            "quantity_purchased": 1.0,
            "packaging_text": pack_info,
            "packaging_count": packaging_count,
            "unit": "",
            "price_unit": "",
            "price_before_discount": price,
            "discount_pct": None,
            "line_amount": None,
            "calculated_unit_price": price,
            "total_units": float(packaging_count),
            "competitor_line_amount": price,
            # Matching placeholders
            "our_unit_price": None,
            "our_comparable_line_price": None,
            "savings_amount": None,
            # Enhanced fields
            "source_page": entry.get("source_page", 0),
            "raw_text": entry["raw_text"],
            "confidence": confidence,
            "parse_method": "generic_ocr",
        }

        rows.append(row)

    return rows, stats


def _calculate_confidence(item_no: str, description: str,
                          pack_info: str, price: Optional[float]) -> float:
    """Calculate a confidence score (0.0-1.0) for an extracted row.

    Higher confidence when:
    - Item number is a clean 5-9 digit number
    - Description is meaningful text (not garbled OCR)
    - Packaging info is present
    - Price is present (but absence doesn't heavily penalize)
    """
    score = 0.0

    # Item number quality
    if item_no:
        if re.match(r"^\d{5,9}$", item_no):
            score += 0.35  # Clean item number
        elif re.match(r"^\d{4,}$", item_no):
            score += 0.25  # Somewhat valid
        else:
            score += 0.1
    else:
        score += 0.0  # No item number

    # Description quality
    if description:
        words = [w for w in description.split() if len(w) >= 2]
        alpha_ratio = sum(1 for c in description if c.isalpha()) / max(len(description), 1)

        if len(words) >= 2 and alpha_ratio > 0.5:
            score += 0.35  # Good description
        elif len(words) >= 1 and alpha_ratio > 0.3:
            score += 0.2  # Acceptable
        else:
            score += 0.05  # Probably garbage

    # Packaging info present
    if pack_info:
        score += 0.15

    # Price present (small bonus, not required)
    if price is not None and price > 0:
        score += 0.15

    return min(round(score, 2), 1.0)


# ============================================================
# XLSX ROW EXTRACTION (generic competitor format)
# ============================================================

def _match_xlsx_column(actual_columns: List[str], candidates: List[str]) -> Optional[str]:
    """Find which actual column name matches any of the candidate names (case-insensitive).

    Returns the actual column name or None if no match found.
    """
    for actual in actual_columns:
        actual_low = str(actual).strip().lower()
        for cand in candidates:
            if actual_low == cand.lower():
                return str(actual).strip()
    # Fuzzy: check if any candidate is contained in actual column name
    for actual in actual_columns:
        actual_low = str(actual).strip().lower()
        for cand in candidates:
            if cand.lower() in actual_low:
                return str(actual).strip()
    return None


def parse_xlsx_rows(content: bytes, source_file: str) -> List[Dict[str, Any]]:
    """
    Read rows from an XLSX file with flexible column name matching.

    Recognizes multiple variants of column names (Norwegian/English, with/without
    'Konkurrent' prefix, different casing). Falls back to positional reading
    if no columns match the expected names.
    """
    try:
        df = pd.read_excel(BytesIO(content), engine="openpyxl")
    except Exception as e:
        raise ValueError(f"Kunne ikke lese XLSX: {e}")

    if df.empty:
        return []

    actual_cols = [str(c).strip() for c in df.columns.tolist()]

    # Map each logical field to a list of possible column name variants
    _COL_VARIANTS = {
        "competitor": [
            "Konkurrent Navn", "Konkurrent", "Leverandør", "Supplier",
            "Competitor", "Kilde", "Firma",
        ],
        "artnr": [
            "Konkurrent Art.Nr", "Art.Nr", "Artikkelnr", "Artikkel",
            "Art.nr", "Varenr", "Varenummer", "Item No", "SKU",
            "Konkurrent Artikkelnummer",
        ],
        "description": [
            "Konkurrent Item Description", "Item Description", "Beskrivelse",
            "Produktnavn", "Produkt", "Description", "Varenavn", "Navn",
            "Konkurrent Beskrivelse",
        ],
        "specification": [
            "Konkurrent Specification", "Specification", "Spesifikasjon",
            "Spec", "Detaljer",
        ],
        "price": [
            "Konkurrent Pris", "Pris", "Price", "Enhetspris", "Stk.pris",
            "Stkpris", "Unit Price",
        ],
        "unit": [
            "Konkurrent salgsenhet", "Salgsenhet", "Enhet", "Unit",
            "Pakningsenhet",
        ],
        "quantity": [
            "Antall", "Kvantum", "Quantity", "Qty", "Mengde", "Ant",
            "Bestilt antall",
        ],
    }

    # Resolve actual column names for each field
    col_map = {}
    for field, variants in _COL_VARIANTS.items():
        col_map[field] = _match_xlsx_column(actual_cols, variants)

    # If no description or artnr column found, try positional fallback:
    # assume first text-heavy column is description
    if not col_map["description"] and not col_map["artnr"]:
        logger.info(
            f"XLSX {source_file}: Ingen kjente kolonner funnet "
            f"(kolonner: {actual_cols}). Prøver posisjonell fallback."
        )
        # Find first column with mostly string values
        for c in actual_cols:
            sample = df[c].dropna().head(5)
            if len(sample) > 0 and all(isinstance(v, str) for v in sample):
                col_map["description"] = c
                break
        # Find first numeric column for price
        for c in actual_cols:
            if c == col_map.get("description"):
                continue
            sample = df[c].dropna().head(5)
            if len(sample) > 0:
                try:
                    [float(v) for v in sample]
                    col_map["price"] = c
                    break
                except (ValueError, TypeError):
                    continue

    logger.debug(f"XLSX column mapping for {source_file}: {col_map}")

    rows: List[Dict[str, Any]] = []

    for idx, r in df.iterrows():
        row_dict = r.to_dict()

        def _get(field: str, default: str = "") -> str:
            col = col_map.get(field)
            if col is None:
                return default
            val = row_dict.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return default
            return str(val).strip()

        desc = _get("description")
        art = _get("artnr")

        if not desc and not art:
            continue

        price = _to_float(_get("price")) if col_map.get("price") else None
        qty = _to_float(_get("quantity")) if col_map.get("quantity") else None

        qty_val = qty if qty else 0.0
        total_units = qty_val * 1  # packaging_count = 1
        competitor_line_amount = round(qty_val * price, 2) if (price is not None and qty_val) else None

        rows.append({
            "source_file": source_file,
            "competitor": _get("competitor"),
            "competitor_artnr": art,
            "description": desc,
            "quantity_purchased": qty_val,
            "packaging_text": "",
            "packaging_count": 1,
            "unit": _get("unit"),
            "price_unit": "",
            "price_before_discount": price,
            "discount_pct": None,
            "line_amount": None,
            "calculated_unit_price": price,
            "total_units": total_units,
            "competitor_line_amount": competitor_line_amount,
            "our_unit_price": None,
            "our_comparable_line_price": None,
            "savings_amount": None,
        })

    return rows


# ============================================================
# DETECT INVOICE SOURCE FROM PDF TEXT
# ============================================================

def detect_source(text: str) -> str:
    """Detect invoice source from extracted text. Returns source name or 'unknown'.

    For Epion, also checks for the distinctive price pattern (N,- X N N,-)
    because OCR on scanned pages may fail to read the 'Epion' brand text.
    """
    low = text.lower()
    if "norengros" in low:
        return "norengros"
    if "epion" in low:
        return "epion"
    # Fallback: detect Epion by its unique price+qty pattern and delivery status
    # Pattern: "N,- X N N,-" with delivery status "N/N sendt"
    has_epion_prices = bool(re.search(
        r"\d[\d\s.]*,[" + _DASH_CHARS + r"]\s*[" + _MULT_CHARS + r"]\s*\d+", text
    ))
    has_delivery_status = bool(re.search(r"\d+/\d+\s+sendt", low))
    if has_epion_prices and has_delivery_status:
        return "epion"
    return "unknown"


# ============================================================
# PER-FILE PARSE ENTRY POINT
# ============================================================

def parse_file(filename: str, content: bytes) -> Dict[str, Any]:
    """
    Classify, extract text/metadata, and parse rows from a single file.

    Returns a dict with:
      - file_type, parse_status, metadata
      - rows: list of parsed V2 row dicts
    """
    file_type = classify_file(filename, content)
    result: Dict[str, Any] = {"file_type": file_type, "rows": []}

    if file_type == "unknown":
        result["parse_status"] = "error"
        result["error"] = "Ukjent filformat"
        return result

    if file_type == "pdf":
        return _parse_pdf(filename, content, result)

    if file_type == "xlsx":
        return _parse_xlsx(filename, content, result)

    return result


def _parse_pdf(filename: str, content: bytes, result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract text from PDF with full pipeline: text extraction -> quality check -> OCR -> parsing.

    Pipeline:
    1. Try native PDF text extraction
    2. Assess quality of extracted text
    3. If quality is poor, run enhanced OCR with preprocessing
    4. Try NorEngros parser on the text
    5. Try Epion parser for Epion order confirmations
    6. If no specialized parser matched, try generic parser
    7. Return structured rows with confidence scores
    """
    pipeline_log: List[str] = []
    page_count = _count_pdf_pages(content)
    result["page_count"] = page_count
    pipeline_log.append(f"PDF mottatt: {filename}, {page_count} sider")

    try:
        # === Step 1: Native text extraction ===
        pipeline_log.append("Forsøker vanlig tekstuttrekk...")
        text = extract_text_from_pdf(content)
        text_quality = assess_text_quality(text)
        result["direct_text_quality"] = text_quality
        pipeline_log.append(
            f"Tekstuttrekk: {text_quality['char_count']} tegn, "
            f"{text_quality['word_count']} ord, "
            f"{text_quality['product_line_hints']} produktlinjer, "
            f"kvalitet={text_quality['verdict']} (score={text_quality['quality_score']})"
        )

        ocr_needed = needs_ocr(text) or text_quality["verdict"] == "poor"
        ocr_used = False
        ocr_page_results = None

        # === Step 2: OCR if needed ===
        if ocr_needed:
            pipeline_log.append("Tekstkvalitet for lav - starter OCR med bildeprosessering...")
            try:
                ocr_page_results = ocr_pdf_pages(content)
                ocr_used = True

                # Gather stats
                good_pages = sum(1 for p in ocr_page_results if p["quality"] == "good")
                poor_pages = sum(1 for p in ocr_page_results if p["quality"] == "poor")
                empty_pages = sum(1 for p in ocr_page_results if p["quality"] == "empty")
                error_pages = sum(1 for p in ocr_page_results if p.get("error"))

                pipeline_log.append(
                    f"OCR ferdig: {good_pages} gode sider, {poor_pages} svake sider, "
                    f"{empty_pages} tomme sider, {error_pages} feilede sider"
                )

                # Build combined text from OCR, but keep page results for tracking
                ocr_text = "\n".join(p["text"] for p in ocr_page_results if p.get("text"))
                ocr_quality = assess_text_quality(ocr_text)
                result["ocr_text_quality"] = ocr_quality

                pipeline_log.append(
                    f"OCR total: {ocr_quality['char_count']} tegn, "
                    f"{ocr_quality['word_count']} ord, "
                    f"kvalitet={ocr_quality['verdict']}"
                )

                # Use OCR text if it's better than direct extraction
                if ocr_quality["quality_score"] >= text_quality["quality_score"]:
                    text = ocr_text
                    pipeline_log.append("Bruker OCR-tekst (bedre kvalitet)")
                else:
                    pipeline_log.append("Beholder direkte tekstuttrekk (bedre enn OCR)")

                result["ocr_page_stats"] = {
                    "good": good_pages,
                    "poor": poor_pages,
                    "empty": empty_pages,
                    "error": error_pages,
                }

            except Exception as ocr_err:
                pipeline_log.append(f"OCR feilet: {ocr_err}")
                logger.warning(f"V2: OCR feilet for {filename}: {ocr_err}")

        result["text_length"] = len(text)
        result["ocr_needed"] = ocr_needed
        result["ocr_used"] = ocr_used
        result["text_preview"] = text[:300] if text else ""

        # === Step 3: Detect source and parse ===
        source = detect_source(text)
        result["detected_source"] = source
        pipeline_log.append(f"Kilde detektert: {source}")

        rows: List[Dict[str, Any]] = []
        parse_method = "none"

        # Try NorEngros parser first
        if source == "norengros":
            pipeline_log.append("Prøver NorEngros-parser...")
            norengros_rows = parse_norengros_text(text, source_file=filename)
            pipeline_log.append(f"NorEngros-parser: {len(norengros_rows)} rader funnet")

            if norengros_rows:
                rows = norengros_rows
                parse_method = "norengros"
                # Add enhanced fields to NorEngros rows
                for r in rows:
                    r.setdefault("source_page", 0)
                    r.setdefault("raw_text", "")
                    r.setdefault("confidence", 0.8)  # NorEngros rows are generally reliable
                    r.setdefault("parse_method", "norengros")

        # Try Epion parser for Epion order confirmations
        if not rows and source == "epion":
            pipeline_log.append("Prøver Epion-parser...")
            epion_rows = parse_epion_text(text, source_file=filename)
            pipeline_log.append(f"Epion-parser: {len(epion_rows)} rader funnet")

            if epion_rows:
                rows = epion_rows
                parse_method = "epion"

        # Try Epion parser even for unknown sources if text has Epion-like patterns
        # (OCR on scanned pages may fail to read the 'Epion' brand text)
        if not rows and source == "unknown":
            has_delivery = bool(re.search(r"\d+/\d+\s+sendt", text, re.IGNORECASE))
            has_price_x = bool(re.search(
                r"\d[,.][-–—−]\s*[Xx×]\s*\d", text
            ))
            if has_delivery and has_price_x:
                pipeline_log.append(
                    "Kilde ukjent, men tekst inneholder Epion-lignende mønster "
                    "(leveringsstatus + prismønster) - prøver Epion-parser..."
                )
                epion_rows = parse_epion_text(text, source_file=filename)
                pipeline_log.append(f"Epion-parser (fallback): {len(epion_rows)} rader funnet")
                if epion_rows:
                    rows = epion_rows
                    parse_method = "epion_fallback"

        # === Step 4: Generic parser fallback ===
        if not rows:
            pipeline_log.append(
                "Ingen rader fra spesialisert parser - prøver generisk produktlinje-parser..."
            )
            generic_rows, generic_stats = parse_generic_product_lines(
                text, source_file=filename, page_texts=ocr_page_results
            )
            pipeline_log.append(
                f"Generisk parser: {len(generic_rows)} rader, "
                f"{generic_stats['rows_with_price']} med pris, "
                f"{generic_stats['rows_without_price']} uten pris, "
                f"{generic_stats['noise_lines_skipped']} støylinjer filtrert, "
                f"{generic_stats['continuation_lines_merged']} sammenslåtte linjer"
            )

            if generic_stats["discard_reasons"]:
                pipeline_log.append(
                    f"Forkastede linjer: {generic_stats['discard_reasons']}"
                )

            result["generic_parse_stats"] = generic_stats

            if generic_rows:
                rows = generic_rows
                parse_method = "generic_ocr"

        # Also try generic parser if NorEngros gave very few rows vs page count
        elif len(rows) < page_count and page_count >= 2:
            pipeline_log.append(
                f"NorEngros ga kun {len(rows)} rader for {page_count} sider - "
                f"prøver også generisk parser..."
            )
            generic_rows, generic_stats = parse_generic_product_lines(
                text, source_file=filename, page_texts=ocr_page_results
            )
            if len(generic_rows) > len(rows):
                pipeline_log.append(
                    f"Generisk parser fant {len(generic_rows)} rader "
                    f"(mer enn NorEngros: {len(rows)}) - bruker generisk resultat"
                )
                rows = generic_rows
                parse_method = "generic_ocr"
                result["generic_parse_stats"] = generic_stats
            else:
                pipeline_log.append(
                    f"Generisk parser fant {len(generic_rows)} rader "
                    f"- beholder NorEngros-resultat ({len(rows)} rader)"
                )

        result["rows"] = rows
        result["row_count"] = len(rows)
        result["parse_method"] = parse_method
        result["pipeline_log"] = pipeline_log

        # === Step 5: Set parse status with detailed info ===
        if rows:
            result["parse_status"] = "parsed"
            rows_with_price = sum(1 for r in rows if r.get("price_before_discount") is not None)
            rows_without_price = len(rows) - rows_with_price
            result["rows_with_price"] = rows_with_price
            result["rows_without_price"] = rows_without_price
            result["price_info"] = (
                f"{rows_with_price} rader med pris, {rows_without_price} uten"
                if rows_without_price > 0
                else f"Alle {rows_with_price} rader har pris"
            )
            pipeline_log.append(
                f"Ferdig: {len(rows)} strukturerte varelinjer "
                f"({rows_with_price} med pris, {rows_without_price} uten pris)"
            )
        else:
            result["parse_status"] = "parsed"
            # Build a specific explanation of why no rows were found
            if not text or not text.strip():
                result["parse_warning"] = (
                    "Kunne ikke hente ut lesbar tekst fra PDF. "
                    "OCR ga ingen brukbare varelinjer."
                )
            elif ocr_used and not ocr_page_results:
                result["parse_warning"] = (
                    "PDF ser ut til å være bildebasert. "
                    "OCR ble forsøkt, men resultatet var for svakt."
                )
            elif text_quality["verdict"] != "empty":
                result["parse_warning"] = (
                    "PDF analysert, men fant ingen varelinjer som matcher "
                    "forventet format (varenummer + beskrivelse)."
                )
            else:
                result["parse_warning"] = (
                    "OCR fullført, men parsing av varetabell ga 0 strukturerte rader."
                )
            pipeline_log.append(f"Ingen strukturerte rader funnet. {result['parse_warning']}")

        # Log the full pipeline
        for line in pipeline_log:
            logger.info(f"V2 PDF pipeline [{filename}]: {line}")

    except Exception as e:
        result["parse_status"] = "error"
        result["error"] = f"PDF-lesing feilet: {e}"
        logger.exception(f"V2: PDF parsing feilet for {filename}")

    return result


def _parse_xlsx(filename: str, content: bytes, result: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect XLSX and extract rows."""
    try:
        info = inspect_xlsx(content)
        result["sheets"] = info["sheets"]
        result["total_rows"] = info["total_rows"]

        rows = parse_xlsx_rows(content, source_file=filename)
        result["rows"] = rows
        result["row_count"] = len(rows)
        result["parse_status"] = "parsed"

    except Exception as e:
        result["parse_status"] = "error"
        result["error"] = str(e)

    return result


def _count_pdf_pages(content: bytes) -> int:
    try:
        return len(PdfReader(BytesIO(content)).pages)
    except Exception:
        return 0
