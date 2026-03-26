"""
V2 Parsing module – file classification, text extraction, and row extraction.

Handles:
- PDF text extraction (with OCR fallback)
- XLSX detection and inspection
- NorEngros invoice parsing with full field extraction
"""
import logging
import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

import pandas as pd
from pypdf import PdfReader

logger = logging.getLogger(__name__)

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


_OCR_MIN_CHARS = int(os.getenv("OCR_MIN_CHARS", "80"))
_OCR_MIN_WORDS = int(os.getenv("OCR_MIN_WORDS", "15"))


def needs_ocr(text: str) -> bool:
    if not text or not text.strip():
        return True
    stripped = text.strip()
    if len(stripped) < _OCR_MIN_CHARS:
        return True
    words = [w for w in re.split(r"\s+", stripped) if len(w) >= 2]
    if len(words) < _OCR_MIN_WORDS:
        return True
    return False


def ocr_pdf_fallback(pdf_bytes: bytes) -> str:
    import fitz
    import pytesseract
    from PIL import Image
    from io import BytesIO as _BytesIO

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    try:
        for page in doc:
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(_BytesIO(pix.tobytes("png")))
            t = pytesseract.image_to_string(img, lang="nor+eng")
            if t:
                parts.append(t)
    finally:
        doc.close()
    return "\n".join(parts)


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
# XLSX ROW EXTRACTION (generic competitor format)
# ============================================================

def parse_xlsx_rows(content: bytes, source_file: str) -> List[Dict[str, Any]]:
    """
    Read rows from an XLSX file in the standard competitor input format.

    Expected columns (flexible matching):
      Konkurrent Navn, Konkurrent Art.Nr, Konkurrent Item Description,
      Konkurrent Specification, Konkurrent Pris, Konkurrent salgsenhet, Antall
    """
    try:
        df = pd.read_excel(BytesIO(content), engine="openpyxl")
    except Exception as e:
        raise ValueError(f"Kunne ikke lese XLSX: {e}")

    rows: List[Dict[str, Any]] = []

    for idx, r in df.iterrows():
        row_dict = r.to_dict()
        desc = str(row_dict.get("Konkurrent Item Description", "") or "").strip()
        art = str(row_dict.get("Konkurrent Art.Nr", "") or "").strip()

        if not desc and not art:
            continue

        price = _to_float(row_dict.get("Konkurrent Pris"))
        qty = _to_float(row_dict.get("Antall"))

        qty_val = qty if qty else 0.0
        # For generic XLSX: no packaging info, so total_units = quantity
        total_units = qty_val * 1  # packaging_count = 1
        # competitor_line_amount: qty × price if available
        competitor_line_amount = round(qty_val * price, 2) if (price is not None and qty_val) else None

        rows.append({
            "source_file": source_file,
            "competitor": str(row_dict.get("Konkurrent Navn", "") or "").strip(),
            "competitor_artnr": art,
            "description": desc,
            "quantity_purchased": qty_val,
            "packaging_text": "",
            "packaging_count": 1,
            "unit": str(row_dict.get("Konkurrent salgsenhet", "") or "").strip(),
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
    """Detect invoice source from extracted text. Returns source name or 'unknown'."""
    low = text.lower()
    if "norengros" in low:
        return "norengros"
    if "epion" in low:
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
    """Extract text from PDF, detect source, and parse rows."""
    try:
        text = extract_text_from_pdf(content)
        ocr_needed = needs_ocr(text)
        ocr_used = False

        if ocr_needed:
            try:
                text = ocr_pdf_fallback(content)
                ocr_used = True
            except Exception as ocr_err:
                logger.warning(f"V2: OCR-fallback feilet: {ocr_err}")

        result["text_length"] = len(text)
        result["ocr_needed"] = ocr_needed
        result["ocr_used"] = ocr_used
        result["text_preview"] = text[:200] if text else ""
        result["page_count"] = _count_pdf_pages(content)

        # Detect source and parse rows
        source = detect_source(text)
        result["detected_source"] = source

        if source == "norengros":
            rows = parse_norengros_text(text, source_file=filename)
            result["rows"] = rows
            result["row_count"] = len(rows)
            result["parse_status"] = "parsed"
        else:
            # Other sources not yet implemented — mark as parsed but with 0 rows
            result["rows"] = []
            result["row_count"] = 0
            result["parse_status"] = "parsed"

    except Exception as e:
        result["parse_status"] = "error"
        result["error"] = f"PDF-lesing feilet: {e}"

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
