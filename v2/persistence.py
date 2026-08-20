"""
V2 Review persistence – file-based storage for review state per job.

Files per job (stored in DATA_DIR/v2/reviews/{job_id}/):
  review.json      – full review rows (matched rows + review fields)
  selections.json  – {dedup_idx: {candidate_idx, candidate_status, selected_at}}
  decisions.json   – {dedup_idx: {status, decided_at}}
  extras.json      – {dedup_idx: {comment}}
  deletions.json   – {dedup_idx: {deleted_at}}
  lock.json        – {locked: bool, locked_at, locked_by}
"""
import json
import logging
import os
import threading
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__import__("os").getenv("DATA_DIR", "/var/data")).resolve()
V2_REVIEWS_DIR = _DATA_DIR / "v2" / "reviews"
V2_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
_STATE_LOCKS: Dict[str, threading.RLock] = {}
_STATE_LOCKS_GUARD = threading.Lock()


class StaleRevisionError(Exception):
    pass


def _state_lock(job_id: str) -> threading.RLock:
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(job_id, threading.RLock())


@contextmanager
def _state_transaction_lock(job_id: str):
    """Serialize one job's state transaction across threads and workers."""
    with _state_lock(job_id):
        lock_path = _job_dir(job_id) / ".state.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


def _write_json_atomic(path: Path, data: Any) -> None:
    """Durably replace one JSON document without exposing partial state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _legacy_review_state(job_id: str) -> Dict[str, Any]:
    state = _read_json(_job_dir(job_id) / "state.json")
    if isinstance(state, dict):
        return {
            "selections": state.get("selections", {}),
            "decisions": state.get("decisions", {}),
            "undo_tokens": state.get("undo_tokens", {}),
            "revoked_learning_tokens": state.get("revoked_learning_tokens", []),
            "revision": int(state.get("revision", 0)),
        }
    return {
        "selections": _read_json(_job_dir(job_id) / "selections.json") or {},
        "decisions": _read_json(_job_dir(job_id) / "decisions.json") or {},
        "undo_tokens": {},
        "revoked_learning_tokens": [],
        "revision": 0,
    }


def _load_workspace_unlocked(job_id: str) -> Dict[str, Any]:
    """Load the authoritative transaction document, with legacy fallback."""
    workspace = _read_json(_job_dir(job_id) / "workspace.json")
    if isinstance(workspace, dict):
        state = workspace.get("state") if isinstance(workspace.get("state"), dict) else {}
        return {
            "review": workspace.get("review"),
            "extras": workspace.get("extras", {}),
            "deletions": workspace.get("deletions", {}),
            "state": {
                "selections": state.get("selections", {}),
                "decisions": state.get("decisions", {}),
                "undo_tokens": state.get("undo_tokens", {}),
                "revoked_learning_tokens": state.get("revoked_learning_tokens", []),
                "revision": int(state.get("revision", 0)),
            },
        }
    return {
        "review": _read_json(_job_dir(job_id) / "review.json"),
        "extras": _read_json(_job_dir(job_id) / "extras.json") or {},
        "deletions": _read_json(_job_dir(job_id) / "deletions.json") or {},
        "state": _legacy_review_state(job_id),
    }


def _load_review_state(job_id: str) -> Dict[str, Any]:
    return _load_workspace_unlocked(job_id)["state"]


_UNSET = object()


def save_review_workspace(
    job_id: str,
    *,
    review: Any = _UNSET,
    extras: Any = _UNSET,
    deletions: Any = _UNSET,
    selections: Any = _UNSET,
    decisions: Any = _UNSET,
    undo_tokens: Any = _UNSET,
    revoked_learning_tokens: Any = _UNSET,
    expected_revision: Optional[int] = None,
) -> int:
    """Persist all material review data as one CAS-protected document."""
    with _state_transaction_lock(job_id):
        workspace = _load_workspace_unlocked(job_id)
        state = workspace["state"]
        if expected_revision is not None and state["revision"] != expected_revision:
            raise StaleRevisionError(
                f"Forventet revisjon {expected_revision}, aktuell er {state['revision']}"
            )
        if review is not _UNSET:
            workspace["review"] = review
        if extras is not _UNSET:
            workspace["extras"] = extras
        if deletions is not _UNSET:
            workspace["deletions"] = deletions
        if selections is not _UNSET:
            state["selections"] = selections
        if decisions is not _UNSET:
            state["decisions"] = decisions
        if undo_tokens is not _UNSET:
            state["undo_tokens"] = undo_tokens
        if revoked_learning_tokens is not _UNSET:
            state["revoked_learning_tokens"] = revoked_learning_tokens
        state["revision"] += 1
        _write_json_atomic(_job_dir(job_id) / "workspace.json", workspace)
        return state["revision"]


def save_review_state(
    job_id: str,
    *,
    selections: Dict[str, Any],
    decisions: Dict[str, Dict[str, Any]],
    undo_tokens: Optional[Dict[str, Dict[str, Any]]] = None,
    expected_revision: Optional[int] = None,
) -> int:
    """Atomically persist selection and decision state as one transaction."""
    return save_review_workspace(
        job_id,
        selections=selections,
        decisions=decisions,
        undo_tokens=_UNSET if undo_tokens is None else undo_tokens,
        expected_revision=expected_revision,
    )


def get_review_revision(job_id: str) -> int:
    return _load_review_state(job_id)["revision"]


def load_review_state(job_id: str) -> Dict[str, Any]:
    return _load_review_state(job_id)


def load_review_workspace(job_id: str) -> Dict[str, Any]:
    """Read one complete, internally consistent review workspace snapshot."""
    return _load_workspace_unlocked(job_id)


def has_material_review_state(job_id: str) -> bool:
    """Return whether matching would risk discarding persisted review work."""
    job_dir = _job_dir(job_id)
    workspace = _read_json(job_dir / "workspace.json")
    if isinstance(workspace, dict):
        state = workspace.get("state") if isinstance(workspace.get("state"), dict) else {}
        return any((
            workspace.get("review") is not None,
            bool(workspace.get("extras")),
            bool(workspace.get("deletions")),
            int(state.get("revision", 0)) > 0,
            bool(state.get("selections")),
            bool(state.get("decisions")),
        ))
    return any(
        (job_dir / filename).exists()
        for filename in (
            "review.json",
            "state.json",
            "selections.json",
            "decisions.json",
            "extras.json",
            "deletions.json",
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# REVIEW DATA (full review rows)
# ============================================================

def init_review(job_id: str, matched_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Create initial review data from matched rows.

    Adds review-specific fields to each row:
      - review_status: 'pending' | 'approved' | 'rejected'
      - suggested_candidate_idx: from best_candidate_idx
      - selected_candidate_idx: None until the reviewer chooses or approves
      - comment: ''
      - deleted: False
    """
    review_rows = []
    for row in matched_rows:
        rr = dict(row)
        rr["review_status"] = "pending"
        rr["suggested_candidate_idx"] = row.get("best_candidate_idx")
        rr["selected_candidate_idx"] = None
        rr["candidate_status"] = (
            "suggested" if row.get("best_candidate_idx") is not None else None
        )
        rr["comment"] = ""
        rr["deleted"] = False
        review_rows.append(rr)
    return review_rows


def save_review(job_id: str, review_rows: List[Dict[str, Any]]) -> None:
    save_review_workspace(job_id, review=review_rows)


def load_review(job_id: str) -> Optional[List[Dict[str, Any]]]:
    return _load_workspace_unlocked(job_id)["review"]


# ============================================================
# SELECTIONS (which candidate is chosen per row)
# ============================================================

def save_selections(job_id: str, selections: Dict[str, Any]) -> None:
    """Persist candidate selections; legacy integer values remain readable."""
    state = _load_review_state(job_id)
    save_review_state(
        job_id, selections=selections, decisions=state["decisions"]
    )


def load_selections(job_id: str) -> Dict[str, Any]:
    data = _load_review_state(job_id)["selections"]
    return data if isinstance(data, dict) else {}


def save_selection(job_id: str, dedup_idx: int, candidate_idx: int) -> None:
    """Save a single selection, merging with existing."""
    sels = load_selections(job_id)
    sels[str(dedup_idx)] = {
        "candidate_idx": candidate_idx,
        "candidate_status": "selected",
        "selected_at": _utc_now_iso(),
    }
    save_selections(job_id, sels)


# ============================================================
# DECISIONS (review status per row)
# ============================================================

def save_decisions(job_id: str, decisions: Dict[str, Dict[str, Any]]) -> None:
    state = _load_review_state(job_id)
    save_review_state(
        job_id, selections=state["selections"], decisions=decisions
    )


def load_decisions(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _load_review_state(job_id)["decisions"]
    return data if isinstance(data, dict) else {}


def save_decision(job_id: str, dedup_idx: int, status: str) -> None:
    """Save a single decision, merging with existing."""
    decs = load_decisions(job_id)
    decs[str(dedup_idx)] = {"status": status, "decided_at": _utc_now_iso()}
    save_decisions(job_id, decs)


def normalize_decision_status(status: Any) -> str:
    """Map legacy review values to the current decision contract."""
    if status == "rejected":
        return "not_same"
    if status in ("pending", "approved", "not_same", "no_suitable"):
        return status
    return "pending"


def add_undo_snapshot(state: Dict[str, Any], dedup_idx: int) -> str:
    """Add one exact pre-mutation snapshot to an in-memory state transaction."""
    idx = str(dedup_idx)
    token = uuid.uuid4().hex
    snapshots = state.setdefault("undo_tokens", {})
    snapshots[token] = {
        "dedup_idx": dedup_idx,
        "decision_present": idx in state["decisions"],
        "decision": state["decisions"].get(idx),
        "selection_present": idx in state["selections"],
        "selection": state["selections"].get(idx),
        "created_at": _utc_now_iso(),
        "valid_revision": state["revision"] + 1,
    }
    return token


def consume_undo_snapshot(
    job_id: str, token: str, expected_revision: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Restore and consume a server-issued one-time undo snapshot."""
    with _state_transaction_lock(job_id):
        workspace = _load_workspace_unlocked(job_id)
        state = workspace["state"]
        if expected_revision is not None and state["revision"] != expected_revision:
            raise StaleRevisionError
        snapshot = state["undo_tokens"].get(token)
        if not isinstance(snapshot, dict):
            return None
        if state["revision"] != snapshot.get("valid_revision"):
            raise StaleRevisionError
        idx = str(snapshot.get("dedup_idx"))
        if snapshot.get("decision_present"):
            state["decisions"][idx] = snapshot.get("decision")
        else:
            state["decisions"].pop(idx, None)
        if snapshot.get("selection_present"):
            state["selections"][idx] = snapshot.get("selection")
        else:
            state["selections"].pop(idx, None)
        state["undo_tokens"].pop(token, None)
        if token not in state["revoked_learning_tokens"]:
            state["revoked_learning_tokens"].append(token)
        revision = state["revision"] + 1
        state["revision"] = revision
        _write_json_atomic(_job_dir(job_id) / "workspace.json", workspace)
        snapshot["revision"] = revision
        return snapshot


def rehydrate_legacy_approved_selections(
    job_id: str, review_rows: List[Dict[str, Any]]
) -> None:
    """Persist explicit selections carried only by legacy approved review rows."""
    with _state_transaction_lock(job_id):
        workspace = _load_workspace_unlocked(job_id)
        state = workspace["state"]
        selections = dict(state["selections"])
        decisions = state["decisions"]
        changed = False
        for row in review_rows:
            idx = str(row.get("dedup_idx", ""))
            if idx in selections:
                continue
            status = decisions.get(idx, {}).get("status", row.get("review_status"))
            if normalize_decision_status(status) != "approved":
                continue
            candidate_idx = row.get("selected_candidate_idx")
            if candidate_idx is None:
                candidate_idx = row.get(
                    "suggested_candidate_idx", row.get("best_candidate_idx")
                )
            selected_candidate = next(
                (
                    candidate
                    for candidate in row.get("candidates", [])
                    if candidate.get("candidate_idx") == candidate_idx
                ),
                None,
            )
            if selected_candidate is None:
                continue
            selections[idx] = {
                "candidate_idx": candidate_idx,
                "candidate_status": "selected",
                "selected_at": _utc_now_iso(),
                "rehydrated": True,
                "candidate_identity": {
                    "candidate_idx": candidate_idx,
                    "our_artnr": str(selected_candidate.get("our_artnr") or ""),
                    "matched_from": str(
                        selected_candidate.get("matched_from")
                        or selected_candidate.get("source")
                        or ""
                    ),
                },
            }
            changed = True
        if not changed:
            return
        state["selections"] = selections
        state["revision"] += 1
        _write_json_atomic(_job_dir(job_id) / "workspace.json", workspace)


# ============================================================
# DELETIONS (soft-delete rows)
# ============================================================

def save_deletions(
    job_id: str,
    deletions: Dict[str, Dict[str, Any]],
    expected_revision: Optional[int] = None,
) -> int:
    return save_review_workspace(
        job_id, deletions=deletions, expected_revision=expected_revision
    )


def load_deletions(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _load_workspace_unlocked(job_id)["deletions"]
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

def save_extras(
    job_id: str,
    extras: Dict[str, Dict[str, Any]],
    expected_revision: Optional[int] = None,
) -> int:
    return save_review_workspace(
        job_id, extras=extras, expected_revision=expected_revision
    )


def load_extras(job_id: str) -> Dict[str, Dict[str, Any]]:
    data = _load_workspace_unlocked(job_id)["extras"]
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

def apply_overrides(
    review_rows: List[Dict[str, Any]],
    job_id: str,
    workspace: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Apply persisted selections, decisions, extras, deletions, and quantity overrides.

    When a selection or quantity changes, recalculates:
      our_unit_price, our_comparable_line_price, savings_amount
    """
    snapshot = workspace if workspace is not None else load_review_workspace(job_id)
    state = snapshot.get("state", {})
    sels = state.get("selections", {})
    decs = state.get("decisions", {})
    extras = snapshot.get("extras", {})
    dels = snapshot.get("deletions", {})

    result = []
    for row in review_rows:
        r = dict(row)
        idx_str = str(r.get("dedup_idx", ""))

        # Apply deletion
        if idx_str in dels:
            r["deleted"] = True
        else:
            r["deleted"] = r.get("deleted", False)

        # Apply suggestion/selection. Legacy rows that initialized selection
        # to the best candidate are interpreted as suggestions unless an
        # explicit selection exists.
        suggested_idx = r.get("suggested_candidate_idx", r.get("best_candidate_idx"))
        r["suggested_candidate_idx"] = suggested_idx
        r["selected_candidate_idx"] = None
        r["candidate_status"] = "suggested" if suggested_idx is not None else None
        if idx_str in sels:
            selection = sels[idx_str]
            if isinstance(selection, dict):
                new_cand_idx = selection.get("candidate_idx")
            else:
                new_cand_idx = selection
            candidates = r.get("candidates", [])
            cand = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.get("candidate_idx") == new_cand_idx
                ),
                None,
            )
            expected_identity = selection.get("candidate_identity") if isinstance(selection, dict) else None
            actual_identity = (
                {
                    "candidate_idx": cand.get("candidate_idx"),
                    "our_artnr": str(cand.get("our_artnr") or ""),
                    "matched_from": str(cand.get("matched_from") or cand.get("source") or ""),
                }
                if cand is not None
                else None
            )
            if cand is not None and (not expected_identity or expected_identity == actual_identity):
                r["selected_candidate_idx"] = new_cand_idx
                r["candidate_status"] = "selected"
                r["our_unit_price"] = cand.get("our_unit_price")
        r["active_candidate_idx"] = (
            r["selected_candidate_idx"]
            if r["selected_candidate_idx"] is not None
            else suggested_idx
        )

        # Apply quantity overrides and comment from extras
        if idx_str in extras:
            ex = extras[idx_str]
            if "comment" in ex:
                r["comment"] = ex["comment"]
            if "quantity_override" in ex and ex["quantity_override"] is not None:
                r["quantity_override"] = ex["quantity_override"]
            if "quantity_override_competitor" in ex and ex["quantity_override_competitor"] is not None:
                r["quantity_override_competitor"] = ex["quantity_override_competitor"]

        # Recalculate prices using effective quantity
        _recalc_prices(r)

        # Apply decision
        decision_status = r.get("review_status", "pending")
        decision = None
        if idx_str in decs:
            decision = decs[idx_str]
            decision_status = decision.get("status", decision_status)
        if decision and decision_status == "approved" and decision.get("candidate_identity"):
            selected = next(
                (
                    candidate
                    for candidate in r.get("candidates", [])
                    if candidate.get("candidate_idx") == r.get("selected_candidate_idx")
                ),
                None,
            )
            actual_identity = {
                "candidate_idx": selected.get("candidate_idx") if selected else None,
                "our_artnr": str(selected.get("our_artnr") or "") if selected else "",
                "matched_from": str(selected.get("matched_from") or selected.get("source") or "") if selected else "",
            }
            if actual_identity != decision["candidate_identity"]:
                decision_status = "pending"
        r["review_status"] = normalize_decision_status(decision_status)

        result.append(r)

    return result


def _recalc_prices(r: Dict[str, Any]) -> None:
    """Recalculate line prices and savings based on current state.

    Uses separate quantities for competitor and OM sides:
      - Competitor line amount uses competitor quantity (quantity_override_competitor or total_units)
      - Our line amount uses OM quantity (quantity_override or total_units)
    """
    base_units = r.get("total_units", 0) or 0

    # Competitor side: use competitor override if set, else original total_units
    comp_units = r.get("quantity_override_competitor")
    if comp_units is None:
        comp_units = base_units
    comp_units = comp_units or 0

    # OM side: use OM override if set, else original total_units
    om_units = r.get("quantity_override")
    if om_units is None:
        om_units = base_units
    om_units = om_units or 0

    # Recalc competitor line amount if competitor quantity was overridden
    comp_unit_price = r.get("competitor_unit_price")
    if r.get("quantity_override_competitor") is not None and comp_unit_price is not None and comp_units > 0:
        r["competitor_line_amount"] = round(comp_units * comp_unit_price, 2)

    # Recalc OM line price
    our_price = r.get("our_unit_price")
    competitor_line = r.get("competitor_line_amount")

    if our_price is not None and om_units > 0:
        r["our_comparable_line_price"] = round(om_units * our_price, 2)
        if competitor_line is not None:
            r["savings_amount"] = round(competitor_line - r["our_comparable_line_price"], 2)
        else:
            r["savings_amount"] = None
    else:
        r["our_comparable_line_price"] = None
        r["savings_amount"] = None
