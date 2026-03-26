"""
V2 Parsing module – file classification and text extraction.

Handles PDF text extraction (with OCR fallback) and XLSX detection.
Does NOT implement invoice-specific parsers (NorEngros, Epion, etc.) yet.
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
# PER-FILE PARSE ENTRY POINT
# ============================================================

def parse_file(filename: str, content: bytes) -> Dict[str, Any]:
    """
    Classify and extract basic metadata from a single uploaded file.

    Returns a dict with parse results:
      - file_type: 'pdf' | 'xlsx' | 'unknown'
      - parse_status: 'parsed' | 'error'
      - Plus type-specific metadata (text_length, ocr_used, sheets, etc.)
    """
    file_type = classify_file(filename, content)
    result: Dict[str, Any] = {"file_type": file_type}

    if file_type == "unknown":
        result["parse_status"] = "error"
        result["error"] = "Ukjent filformat"
        return result

    if file_type == "pdf":
        return _parse_pdf_metadata(content, result)

    if file_type == "xlsx":
        return _parse_xlsx_metadata(content, result)

    return result


def _parse_pdf_metadata(content: bytes, result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract text from PDF and report metadata."""
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
                # Keep whatever text we got from direct extraction

        result["text_length"] = len(text)
        result["ocr_needed"] = ocr_needed
        result["ocr_used"] = ocr_used
        result["text_preview"] = text[:200] if text else ""
        result["page_count"] = _count_pdf_pages(content)
        result["parse_status"] = "parsed"

    except Exception as e:
        result["parse_status"] = "error"
        result["error"] = f"PDF-lesing feilet: {e}"

    return result


def _parse_xlsx_metadata(content: bytes, result: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect XLSX and report metadata."""
    try:
        info = inspect_xlsx(content)
        result["sheets"] = info["sheets"]
        result["total_rows"] = info["total_rows"]
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
