"""
V2 Matching module – orchestrates matching of deduplicated rows against our catalog.

Uses the existing matcher.Catalog and LegekontorCatalogBundle from app.py
without modifying them. Keeps top-N candidates per row for later review.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Top-N candidates to keep per row for review
DEFAULT_TOP_N = 5


def _build_v1_compat_row(deduped_row: Dict[str, Any]) -> Dict[str, Any]:
    """Build a dict compatible with LegekontorCatalogBundle.match_competitor_row().

    The bundle expects keys like 'Konkurrent Item Description', etc.
    """
    return {
        "Konkurrent Item Description": deduped_row.get("description", ""),
        "Konkurrent Specification": "",
        "Konkurrent Art.Nr": deduped_row.get("competitor_artnr", ""),
    }


def _extract_candidate_fields(artnr: str, best_row: Optional[Dict[str, Any]],
                               quality: str, source: str,
                               price: Optional[float], price_source: str) -> Dict[str, Any]:
    """Extract candidate info from a match result."""
    description = ""
    specification = ""
    producer = ""

    gid = ""

    if isinstance(best_row, dict):
        description = str(
            best_row.get("Katalog: Item Description")
            or best_row.get("Item Description")
            or best_row.get("Description")
            or ""
        )
        specification = str(
            best_row.get("Katalog: Specification")
            or best_row.get("Specification")
            or best_row.get("Spesifikasjon")
            or ""
        )
        producer = str(
            best_row.get("Katalog: Producer Name")
            or best_row.get("Producer Name")
            or best_row.get("Produsent")
            or ""
        )
        gid = str(
            best_row.get("Katalog: GID")
            or best_row.get("GID")
            or ""
        )
        if gid.lower() == "nan":
            gid = ""

    return {
        "our_artnr": str(artnr or ""),
        "our_description": description,
        "our_specification": specification,
        "our_producer": producer,
        "our_gid": gid,
        "our_unit_price": price,
        "price_source": price_source,
        "match_quality": quality,
        "matched_from": source,
    }


def match_deduped_rows(
    deduped_rows: List[Dict[str, Any]],
    bundle: Any,
    top_n: int = DEFAULT_TOP_N,
    prefer_own_brands: bool = True,
    progress_cb: Any = None,
) -> List[Dict[str, Any]]:
    """Match each deduplicated row against the catalog bundle.

    For each row:
    1. Call bundle.match_competitor_row() for best match (reuses V1 logic)
    2. Also retrieve candidates from both LK and full catalogs
    3. Keep top-N unique candidates with price info
    4. Calculate comparison fields (our_comparable_line_price, savings_amount)

    Returns a new list of matched rows (does not mutate input).
    """
    total = len(deduped_rows)
    matched_rows: List[Dict[str, Any]] = []

    for idx, row in enumerate(deduped_rows):
        matched = _match_single_row(row, bundle, top_n, prefer_own_brands)
        matched_rows.append(matched)

        if progress_cb and total > 0:
            try:
                progress_cb((idx + 1) / total)
            except Exception:
                pass

    return matched_rows


def _match_single_row(
    row: Dict[str, Any],
    bundle: Any,
    top_n: int,
    prefer_own_brands: bool,
) -> Dict[str, Any]:
    """Match a single deduplicated row and return enriched result with candidates."""
    result = dict(row)  # shallow copy, preserves all existing fields
    compat_row = _build_v1_compat_row(row)

    # Collect candidates from both catalogs
    candidates: List[Dict[str, Any]] = []
    seen_artnrs: set = set()

    # Try LK catalog first
    try:
        artnr_lk, _alts_lk, best_row_lk, quality_lk = bundle.lk.match_row(
            compat_row, top_n=top_n * 3, prefer_own_brands=prefer_own_brands
        )
        if artnr_lk and artnr_lk not in seen_artnrs:
            price_lk, psrc_lk = bundle.price_for_artnr(artnr_lk)
            cand = _extract_candidate_fields(artnr_lk, best_row_lk, quality_lk, "lk", price_lk, psrc_lk)
            cand["candidate_idx"] = len(candidates)
            candidates.append(cand)
            seen_artnrs.add(artnr_lk)
    except Exception as e:
        logger.warning(f"V2 matching LK feilet for rad {row.get('dedup_idx')}: {e}")

    # Try full catalog
    try:
        artnr_full, _alts_full, best_row_full, quality_full = bundle.full.match_row(
            compat_row, top_n=top_n * 3, prefer_own_brands=prefer_own_brands
        )
        if artnr_full and artnr_full not in seen_artnrs:
            price_full, psrc_full = bundle.price_for_artnr(artnr_full)
            cand = _extract_candidate_fields(artnr_full, best_row_full, quality_full, "full", price_full, psrc_full)
            cand["candidate_idx"] = len(candidates)
            candidates.append(cand)
            seen_artnrs.add(artnr_full)
    except Exception as e:
        logger.warning(f"V2 matching full feilet for rad {row.get('dedup_idx')}: {e}")

    # Also use the bundle's combined match for the "best" pick
    best_candidate_idx = None
    try:
        artnr_best, best_row_best, quality_best, source_best = bundle.match_competitor_row(
            compat_row, top_n=top_n * 3, prefer_own_brands=prefer_own_brands
        )
        if artnr_best:
            if artnr_best in seen_artnrs:
                # Already in candidates, find it
                for c in candidates:
                    if c["our_artnr"] == artnr_best:
                        best_candidate_idx = c["candidate_idx"]
                        break
            else:
                # New candidate from combined match
                price_best, psrc_best = bundle.price_for_artnr(artnr_best)
                cand = _extract_candidate_fields(
                    artnr_best, best_row_best, quality_best, source_best, price_best, psrc_best
                )
                cand["candidate_idx"] = len(candidates)
                best_candidate_idx = cand["candidate_idx"]
                candidates.append(cand)
                seen_artnrs.add(artnr_best)

            if best_candidate_idx is None:
                best_candidate_idx = 0  # first candidate
    except Exception as e:
        logger.warning(f"V2 matching combined feilet for rad {row.get('dedup_idx')}: {e}")

    # Default best_candidate_idx to first candidate if we have any but combined match failed
    if best_candidate_idx is None and candidates:
        best_candidate_idx = 0

    # Trim to top_n
    candidates = candidates[:top_n]

    # Calculate comparison fields for each candidate
    total_units = row.get("total_units", 0) or 0
    competitor_line_amount = row.get("competitor_line_amount")

    for cand in candidates:
        our_price = cand.get("our_unit_price")
        if our_price is not None and total_units > 0:
            cand["our_comparable_line_price"] = round(total_units * our_price, 2)
            if competitor_line_amount is not None:
                cand["savings_amount"] = round(competitor_line_amount - cand["our_comparable_line_price"], 2)
            else:
                cand["savings_amount"] = None
        else:
            cand["our_comparable_line_price"] = None
            cand["savings_amount"] = None

    # Set top-level fields from best candidate
    if best_candidate_idx is not None and best_candidate_idx < len(candidates):
        best = candidates[best_candidate_idx]
        result["our_unit_price"] = best.get("our_unit_price")
        result["our_comparable_line_price"] = best.get("our_comparable_line_price")
        result["savings_amount"] = best.get("savings_amount")
    else:
        result["our_unit_price"] = None
        result["our_comparable_line_price"] = None
        result["savings_amount"] = None

    result["candidates"] = candidates
    result["best_candidate_idx"] = best_candidate_idx
    result["match_status"] = "matched" if candidates else "no_match"

    return result
