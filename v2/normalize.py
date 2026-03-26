"""
V2 Normalize module – text normalization and row deduplication.

Takes parsed rows from multiple files and produces a deduplicated list
suitable as input to the matching step.
"""
import re
from typing import Any, Dict, List, Optional


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_artnr(artnr: str) -> str:
    """Normalize article number for dedup comparison.

    Strips whitespace, leading zeros, and lowercases.
    """
    s = (artnr or "").strip()
    # Remove common non-alphanumeric separators
    s = re.sub(r"[\s\-\.\/]+", "", s)
    # Strip leading zeros
    s = s.lstrip("0")
    return s.lower()


def normalize_description(desc: str) -> str:
    """Normalize description for fuzzy dedup comparison.

    Lowercases, collapses whitespace, removes some punctuation.
    Keeps digits and Norwegian characters.
    """
    s = (desc or "").strip().lower()
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    # Remove common noise punctuation but keep hyphens and slashes (often meaningful)
    s = re.sub(r"[,;:\"'()]+", "", s)
    return s.strip()


def normalize_unit(unit: str) -> str:
    """Normalize unit text (uppercase, strip whitespace)."""
    return (unit or "").strip().upper()


# ============================================================
# DEDUPLICATION
# ============================================================

def _dedup_key(row: Dict[str, Any]) -> Optional[str]:
    """Build a dedup key for a row.

    Primary: normalized competitor_artnr (if non-empty).
    Fallback: normalized description (conservative — only exact normalized match).

    Returns None if no usable key can be built (row stands alone).
    """
    artnr = normalize_artnr(row.get("competitor_artnr", ""))
    if artnr:
        return f"artnr:{artnr}"

    desc = normalize_description(row.get("description", ""))
    if desc and len(desc) >= 5:
        return f"desc:{desc}"

    return None


def _merge_rows(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a group of duplicate rows into one deduplicated row.

    Summable fields are summed. Other fields use the first row's values.
    Source rows are preserved for traceability.
    """
    first = group[0]

    # Sum numeric accumulation fields
    total_qty = sum(r.get("quantity_purchased", 0) or 0 for r in group)
    total_units = sum(r.get("total_units", 0) or 0 for r in group)
    total_comp_line = None

    # Sum competitor_line_amount only if all rows have it
    amounts = [r.get("competitor_line_amount") for r in group]
    if all(a is not None for a in amounts):
        total_comp_line = round(sum(amounts), 2)

    # Sum line_amount similarly
    raw_amounts = [r.get("line_amount") for r in group]
    total_line_amount = None
    if all(a is not None for a in raw_amounts):
        total_line_amount = round(sum(raw_amounts), 2)

    # Collect source files
    source_files = list(dict.fromkeys(r.get("source_file", "") for r in group))

    # Build source_rows for traceability
    source_rows = []
    for r in group:
        source_rows.append({
            "row_idx": r.get("row_idx"),
            "source_file": r.get("source_file"),
            "quantity_purchased": r.get("quantity_purchased"),
            "total_units": r.get("total_units"),
            "competitor_line_amount": r.get("competitor_line_amount"),
            "line_amount": r.get("line_amount"),
        })

    merged = {
        # Identity fields — from first row
        "source_file": source_files[0] if len(source_files) == 1 else source_files,
        "competitor": first.get("competitor", ""),
        "competitor_artnr": first.get("competitor_artnr", ""),
        "description": first.get("description", ""),
        "packaging_text": first.get("packaging_text", ""),
        "packaging_count": first.get("packaging_count", 1),
        "unit": first.get("unit", ""),
        "price_unit": first.get("price_unit", ""),
        "price_before_discount": first.get("price_before_discount"),
        "discount_pct": first.get("discount_pct"),
        "calculated_unit_price": first.get("calculated_unit_price"),
        # Summed fields
        "quantity_purchased": total_qty,
        "total_units": total_units,
        "line_amount": total_line_amount,
        "competitor_line_amount": total_comp_line,
        # Matching placeholders
        "our_unit_price": None,
        "our_comparable_line_price": None,
        "savings_amount": None,
        # Traceability
        "merged_from_count": len(group),
        "source_rows": source_rows,
    }

    return merged


def deduplicate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate parsed rows.

    Rules:
    1. Primary key: normalized competitor_artnr (if non-empty)
    2. Fallback key: exact normalized description (if artnr is empty, desc >= 5 chars)
    3. Rows with no usable key are kept as-is (no merging)
    4. When merging: quantity_purchased, total_units, competitor_line_amount are summed
    5. Product identity fields (artnr, description, packaging, prices) from first row

    Conservative: only exact key matches are merged. No fuzzy matching.
    """
    # Group by dedup key
    groups: Dict[str, List[Dict[str, Any]]] = {}
    ungrouped: List[Dict[str, Any]] = []

    for row in rows:
        key = _dedup_key(row)
        if key is None:
            ungrouped.append(row)
        else:
            groups.setdefault(key, []).append(row)

    # Merge groups
    result: List[Dict[str, Any]] = []

    for key, group in groups.items():
        if len(group) == 1:
            # Single row — no merge needed, but add traceability fields
            single = dict(group[0])
            single["merged_from_count"] = 1
            single["source_rows"] = [{
                "row_idx": single.get("row_idx"),
                "source_file": single.get("source_file"),
                "quantity_purchased": single.get("quantity_purchased"),
                "total_units": single.get("total_units"),
                "competitor_line_amount": single.get("competitor_line_amount"),
                "line_amount": single.get("line_amount"),
            }]
            result.append(single)
        else:
            result.append(_merge_rows(group))

    # Add ungrouped rows (no key → standalone)
    for row in ungrouped:
        single = dict(row)
        single["merged_from_count"] = 1
        single["source_rows"] = [{
            "row_idx": single.get("row_idx"),
            "source_file": single.get("source_file"),
            "quantity_purchased": single.get("quantity_purchased"),
            "total_units": single.get("total_units"),
            "competitor_line_amount": single.get("competitor_line_amount"),
            "line_amount": single.get("line_amount"),
        }]
        result.append(single)

    # Assign new sequential dedup_idx
    for idx, row in enumerate(result):
        row["dedup_idx"] = idx

    return result
