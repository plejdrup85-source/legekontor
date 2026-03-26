"""
V2 Price database – persistent record of reviewed price comparisons.

Storage: a single JSONL file (one JSON object per record line) at
  DATA_DIR/v2/pricedb.jsonl

Each record represents one reviewed row from a V2 job.
Records are appended when a job is explicitly "committed" to the
price database (not automatically on every review action).

Idempotency: each record carries job_id + dedup_idx. Before writing,
existing records for that job are removed (replace strategy).
This means re-committing a job overwrites its previous entries
rather than duplicating them.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__import__("os").getenv("DATA_DIR", "/var/data")).resolve()
V2_PRICEDB_PATH = _DATA_DIR / "v2" / "pricedb.jsonl"
V2_PRICEDB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_record(row: Dict[str, Any], job_id: str, committed_at: str) -> Dict[str, Any]:
    """Build a flat price database record from a review row (with overrides applied)."""
    sel_idx = row.get("selected_candidate_idx")
    candidates = row.get("candidates", [])
    cand = candidates[sel_idx] if (sel_idx is not None and 0 <= sel_idx < len(candidates)) else None

    source_file = row.get("source_file", "")
    if isinstance(source_file, list):
        source_file = ", ".join(source_file)

    return {
        "job_id": job_id,
        "dedup_idx": row.get("dedup_idx"),
        "committed_at": committed_at,
        "source_file": source_file,
        "competitor": row.get("competitor", ""),
        "competitor_artnr": row.get("competitor_artnr", ""),
        "description": row.get("description", ""),
        "quantity_purchased": row.get("quantity_purchased"),
        "packaging_text": row.get("packaging_text", ""),
        "packaging_count": row.get("packaging_count"),
        "total_units": row.get("total_units"),
        "competitor_line_amount": row.get("competitor_line_amount"),
        "our_artnr": cand.get("our_artnr", "") if cand else "",
        "our_description": cand.get("our_description", "") if cand else "",
        "our_unit_price": row.get("our_unit_price"),
        "our_comparable_line_price": row.get("our_comparable_line_price"),
        "savings_amount": row.get("savings_amount"),
        "review_status": row.get("review_status", "pending"),
        "strategy": row.get("strategy", ""),
        "merge_warning": bool(row.get("merge_warning", False)),
    }


def commit_job(job_id: str, review_rows: List[Dict[str, Any]]) -> int:
    """Write all review rows for a job to the price database.

    Only rows with review_status 'approved' are written.
    Replaces any previous entries for the same job_id (idempotent).

    Returns the number of records written.
    """
    now = _utc_now_iso()

    # Build new records (approved only)
    new_records = []
    for row in review_rows:
        if row.get("review_status") != "approved":
            continue
        new_records.append(_build_record(row, job_id, now))

    # Read existing, remove old records for this job
    existing = _read_all()
    kept = [r for r in existing if r.get("job_id") != job_id]

    # Write kept + new
    kept.extend(new_records)
    _write_all(kept)

    logger.info(f"V2 pricedb: committed {len(new_records)} records for job={job_id}")
    return len(new_records)


def load_all() -> List[Dict[str, Any]]:
    """Load all records from the price database."""
    return _read_all()


def search(
    text: Optional[str] = None,
    competitor: Optional[str] = None,
    competitor_artnr: Optional[str] = None,
    our_artnr: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Search records with optional filters."""
    records = _read_all()

    if competitor:
        comp_low = competitor.lower()
        records = [r for r in records if comp_low in (r.get("competitor") or "").lower()]

    if competitor_artnr:
        art = competitor_artnr.strip()
        records = [r for r in records if art in (r.get("competitor_artnr") or "")]

    if our_artnr:
        art = our_artnr.strip()
        records = [r for r in records if art in (r.get("our_artnr") or "")]

    if text:
        text_low = text.lower()
        records = [r for r in records if text_low in (
            (r.get("description") or "") + " " +
            (r.get("competitor_artnr") or "") + " " +
            (r.get("our_artnr") or "") + " " +
            (r.get("our_description") or "") + " " +
            (r.get("competitor") or "")
        ).lower()]

    # Most recent first
    records.sort(key=lambda r: r.get("committed_at", ""), reverse=True)
    return records[:limit]


def _read_all() -> List[Dict[str, Any]]:
    if not V2_PRICEDB_PATH.exists():
        return []
    records = []
    try:
        with open(V2_PRICEDB_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"V2 pricedb: read error: {e}")
    return records


def _write_all(records: List[Dict[str, Any]]) -> None:
    try:
        with open(V2_PRICEDB_PATH, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"V2 pricedb: write error: {e}")
