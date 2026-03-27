"""
V2 Review persistence – file-based storage for review state per job.

Files per job (stored in DATA_DIR/v2/reviews/{job_id}/):
  review.json      – full review rows (matched rows + review fields)
  selections.json  – {dedup_idx: selected_candidate_idx}
  decisions.json   – {dedup_idx: {status, decided_at}}
  extras.json      – {dedup_idx: {comment}}
  deletions.json   – {dedup_idx: {deleted_at}}
  lock.json        – {locked: bool, locked_at, locked_by}
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__import__("os").getenv("DATA_DIR", "/var/data")).resolve()
V2_REVIEWS_DIR = _DATA_DIR / "v2" / "reviews"
V2_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)


def _job_dir(job_id: str) -> Path:
    d = V2_REVIEWS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# REVIEW DATA (full review rows)
# ============================================================

def init_review(job_id: str, matched_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Create initial review data from matched rows.

    Adds review-specific fields to each row:
      - review_status: 'pending' | 'approved' | 'rejected'
      - selected_candidate_idx: from best_candidate_idx
      - comment: ''
      - deleted: False
    """
    review_rows = []
    for row in matched_rows:
        rr = dict(row)
        rr["review_status"] = "pending"
        rr["selected_candidate_idx"] = row.get("best_candidate_idx")
        rr["comment"] = ""
        rr["deleted"] = False
        review_rows.append(rr)
    return review_rows


def save_review(job_id: str, review_rows: List[Dict[str, Any]]) -> None:
    _write_json(_job_dir(job_id) / "review.json", review_rows)


def load_review(job_id: str) -> Optional[List[Dict[str, Any]]]:
    return _read_json(_job_dir(job_id) / "review.json")


# ============================================================
# SELECTIONS (which candidate is chosen per row)
# ============================================================

def save_selections(job_id: str, selections: Dict[str, int]) -> None:
    """selections: {"0": 2, "5": 0, ...} mapping dedup_idx → candidate_idx"""
    _write_json(_job_dir(job_id) / "selections.json", selections)


def load_selections(job_id: str) -> Dict[str, int]:
    data = _read_json(_job_dir(job_id) / "selections.json")
    return data if isinstance(data, dict) else {}


def save_selection(job_id: str, dedup_idx: int, candidate_idx: int) -> None:
    """Save a single selection, merging with existing."""
    sels = load_selections(job_id)
    sels[str(dedup_idx)] = candidate_idx
    save_selections(job_id, sels)


# ============================================================
# DECISIONS (review status per row)
# ============================================================

def save_decisions(job_id: str, decisions: Dict[str, Dict[str, Any]]) -> None:
    _write_json(_job_dir(job_id) / "decisions.json", decisions)


def load_decisions(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _read_json(_job_dir(job_id) / "decisions.json")
    return data if isinstance(data, dict) else {}


def save_decision(job_id: str, dedup_idx: int, status: str) -> None:
    """Save a single decision, merging with existing."""
    decs = load_decisions(job_id)
    decs[str(dedup_idx)] = {"status": status, "decided_at": _utc_now_iso()}
    save_decisions(job_id, decs)


# ============================================================
# DELETIONS (soft-delete rows)
# ============================================================

def save_deletions(job_id: str, deletions: Dict[str, Dict[str, Any]]) -> None:
    _write_json(_job_dir(job_id) / "deletions.json", deletions)


def load_deletions(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _read_json(_job_dir(job_id) / "deletions.json")
    return data if isinstance(data, dict) else {}


def save_deletion(job_id: str, dedup_idx: int) -> None:
    """Mark a row as deleted."""
    dels = load_deletions(job_id)
    dels[str(dedup_idx)] = {"deleted_at": _utc_now_iso()}
    save_deletions(job_id, dels)


def remove_deletion(job_id: str, dedup_idx: int) -> None:
    """Restore a deleted row."""
    dels = load_deletions(job_id)
    dels.pop(str(dedup_idx), None)
    save_deletions(job_id, dels)


# ============================================================
# EXTRAS (comment per row)
# ============================================================

def save_extras(job_id: str, extras: Dict[str, Dict[str, Any]]) -> None:
    _write_json(_job_dir(job_id) / "extras.json", extras)


def load_extras(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _read_json(_job_dir(job_id) / "extras.json")
    return data if isinstance(data, dict) else {}


def save_extra(job_id: str, dedup_idx: int, comment: Optional[str] = None) -> None:
    """Save comment for one row, merging with existing."""
    extras = load_extras(job_id)
    entry = extras.get(str(dedup_idx), {})
    if comment is not None:
        entry["comment"] = comment
    entry["updated_at"] = _utc_now_iso()
    extras[str(dedup_idx)] = entry
    save_extras(job_id, extras)


# ============================================================
# LOCK
# ============================================================

def set_lock(job_id: str, locked: bool, locked_by: str = "") -> None:
    _write_json(_job_dir(job_id) / "lock.json", {
        "locked": locked,
        "locked_at": _utc_now_iso() if locked else None,
        "locked_by": locked_by,
    })


def is_locked(job_id: str) -> bool:
    data = _read_json(_job_dir(job_id) / "lock.json")
    if isinstance(data, dict):
        return bool(data.get("locked", False))
    return False


def get_lock_info(job_id: str) -> Dict[str, Any]:
    data = _read_json(_job_dir(job_id) / "lock.json")
    if isinstance(data, dict):
        return data
    return {"locked": False, "locked_at": None, "locked_by": ""}


# ============================================================
# APPLY OVERRIDES: merge selections/decisions/extras into review rows
# ============================================================

def apply_overrides(review_rows: List[Dict[str, Any]], job_id: str) -> List[Dict[str, Any]]:
    """Apply persisted selections, decisions, extras, deletions, and quantity overrides.

    When a selection or quantity changes, recalculates:
      our_unit_price, our_comparable_line_price, savings_amount
    """
    sels = load_selections(job_id)
    decs = load_decisions(job_id)
    extras = load_extras(job_id)
    dels = load_deletions(job_id)

    result = []
    for row in review_rows:
        r = dict(row)
        idx_str = str(r.get("dedup_idx", ""))

        # Apply deletion
        if idx_str in dels:
            r["deleted"] = True
        else:
            r["deleted"] = r.get("deleted", False)

        # Apply selection
        if idx_str in sels:
            new_cand_idx = sels[idx_str]
            candidates = r.get("candidates", [])
            if 0 <= new_cand_idx < len(candidates):
                r["selected_candidate_idx"] = new_cand_idx
                cand = candidates[new_cand_idx]
                r["our_unit_price"] = cand.get("our_unit_price")

        # Apply quantity override from extras
        if idx_str in extras:
            ex = extras[idx_str]
            if "comment" in ex:
                r["comment"] = ex["comment"]
            if "quantity_override" in ex and ex["quantity_override"] is not None:
                r["quantity_override"] = ex["quantity_override"]

        # Recalculate prices using effective quantity
        _recalc_prices(r)

        # Apply decision
        if idx_str in decs:
            r["review_status"] = decs[idx_str].get("status", r.get("review_status", "pending"))

        result.append(r)

    return result


def _recalc_prices(r: Dict[str, Any]) -> None:
    """Recalculate our_comparable_line_price and savings_amount based on current state."""
    effective_units = r.get("quantity_override") or r.get("total_units", 0) or 0
    our_price = r.get("our_unit_price")
    competitor_line = r.get("competitor_line_amount")

    if our_price is not None and effective_units > 0:
        r["our_comparable_line_price"] = round(effective_units * our_price, 2)
        if competitor_line is not None:
            r["savings_amount"] = round(competitor_line - r["our_comparable_line_price"], 2)
        else:
            r["savings_amount"] = None
    else:
        r["our_comparable_line_price"] = None
        r["savings_amount"] = None
