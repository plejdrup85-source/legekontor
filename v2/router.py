import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from v2.parsing import classify_file, parse_file
from v2.normalize import deduplicate
from v2.matching import match_deduped_rows
from v2 import persistence as rv
from v2.export import generate_export_xlsx, generate_export_pdf
from v2 import pricedb

logger = logging.getLogger(__name__)

# ============================================================
# V2 STORAGE (completely separate from V1)
# ============================================================
_DATA_DIR = Path(__import__("os").getenv("DATA_DIR", "/var/data")).resolve()

V2_DIR = _DATA_DIR / "v2"
V2_DIR.mkdir(parents=True, exist_ok=True)

V2_UPLOADS_DIR = V2_DIR / "uploads"
V2_UPLOADS_DIR.mkdir(exist_ok=True)

V2_JOBS_INDEX = V2_DIR / "jobs.jsonl"

# ============================================================
# V2 IN-MEMORY TASK STATE + REHYDRATION
# ============================================================
V2_TASKS: Dict[str, Dict[str, Any]] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_task(job_id: str) -> Optional[Dict[str, Any]]:
    """Get task from memory, or rehydrate from disk if missing."""
    t = V2_TASKS.get(job_id)
    if t is not None:
        return t
    return _rehydrate_task(job_id)


def _rehydrate_task(job_id: str) -> Optional[Dict[str, Any]]:
    """Reconstruct a V2 task from on-disk data after restart."""
    # 1) Check jobs.jsonl for metadata
    job_meta = _load_job_meta(job_id)
    if not job_meta:
        return None

    task: Dict[str, Any] = dict(job_meta)
    task["job_id"] = job_id

    job_upload_dir = V2_UPLOADS_DIR / job_id

    # 2) Load persisted row data if available
    deduped = _read_json_file(job_upload_dir / "deduped_rows.json")
    if deduped is not None:
        task["deduped_rows"] = deduped
        task["deduped_count"] = len(deduped)

    matched = _read_json_file(job_upload_dir / "matched_rows.json")
    if matched is not None:
        task["matched_rows"] = matched
        task["matched_count"] = len(matched)

    raw_rows = _read_json_file(job_upload_dir / "parsed_rows.json")
    if raw_rows is not None:
        task["rows"] = raw_rows
        task["total_rows"] = len(raw_rows)

    # 3) Determine best status from what's on disk
    review_exists = rv.load_review(job_id) is not None
    status = task.get("status", "unknown")

    if review_exists and status in ("matched", "review", "matching"):
        task["status"] = "review"
    elif matched is not None and status in ("matching", "matched", "review"):
        task["status"] = "review" if review_exists else "matched"
    elif deduped is not None and status in ("parsing", "parsed", "partial_error"):
        task["status"] = status if status in ("parsed", "partial_error") else "parsed"

    # Cache in memory
    V2_TASKS[job_id] = task
    logger.info(f"V2: Rehydrated job {job_id} (status={task.get('status')})")
    return task


def _load_job_meta(job_id: str) -> Optional[Dict[str, Any]]:
    """Load merged metadata for a single job from jobs.jsonl."""
    if not V2_JOBS_INDEX.exists():
        return None
    result: Optional[Dict[str, Any]] = None
    try:
        with open(V2_JOBS_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("job_id") == job_id:
                        if result is None:
                            result = e
                        else:
                            result.update(e)
                except Exception:
                    continue
    except Exception:
        pass
    return result


def _read_json_file(path: Path) -> Optional[Any]:
    """Read a JSON file, return None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _append_v2_job(event: Dict[str, Any]) -> None:
    try:
        with open(V2_JOBS_INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"V2: Kunne ikke skrive jobs-index: {e}")


def _load_v2_jobs(limit: int = 200) -> List[Dict[str, Any]]:
    if not V2_JOBS_INDEX.exists():
        return []
    jobs: Dict[str, Dict[str, Any]] = {}
    try:
        with open(V2_JOBS_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    jid = e.get("job_id")
                    if not jid:
                        continue
                    jobs.setdefault(jid, {}).update(e)
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"V2: Kunne ikke lese jobs-index: {e}")
        return []
    out = list(jobs.values())
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out[:limit]


# ============================================================
# ROUTER
# ============================================================
v2_router = APIRouter()


@v2_router.get("/", response_class=HTMLResponse)
def v2_index():
    return HTMLResponse(V2_INDEX_HTML)


@v2_router.post("/upload")
async def v2_upload(files: List[UploadFile] = File(...)):
    """Accept one or more PDF/XLSX files, create a V2 job, and start parsing."""
    job_id = uuid.uuid4().hex
    now = _utc_now_iso()

    # Create job-specific upload directory
    job_upload_dir = V2_UPLOADS_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)

    file_results: List[Dict[str, Any]] = []

    for f in files:
        filename = (f.filename or "unknown").strip()
        entry: Dict[str, Any] = {"filename": filename}
        try:
            content = await f.read()
            if not content:
                entry["upload_status"] = "error"
                entry["error"] = "Tom fil"
                file_results.append(entry)
                continue

            file_type = classify_file(filename, content)

            if file_type == "unknown":
                entry["upload_status"] = "error"
                entry["error"] = "Ugyldig filformat. Kun .xlsx og .pdf er støttet."
                file_results.append(entry)
                continue

            entry["type"] = file_type
            entry["size_bytes"] = len(content)

            # Save file to disk
            safe_name = f"{len(file_results):03d}_{filename}"
            dest = job_upload_dir / safe_name
            dest.write_bytes(content)
            entry["stored_as"] = safe_name
            entry["upload_status"] = "uploaded"

        except Exception as e:
            entry["upload_status"] = "error"
            entry["error"] = str(e)

        file_results.append(entry)

    uploaded_count = sum(1 for fr in file_results if fr.get("upload_status") == "uploaded")
    error_count = sum(1 for fr in file_results if fr.get("upload_status") == "error")

    if uploaded_count == 0:
        try:
            job_upload_dir.rmdir()
        except Exception:
            pass
        return JSONResponse(
            {"error": "Ingen gyldige filer ble lastet opp.", "files": file_results},
            status_code=400,
        )

    # Register job as uploaded, then start background parsing
    task_entry = {
        "job_id": job_id,
        "created_at": now,
        "status": "parsing",
        "files": file_results,
        "total_files": len(file_results),
        "uploaded_files": uploaded_count,
        "error_files": error_count,
    }

    V2_TASKS[job_id] = task_entry
    _append_v2_job(task_entry)

    # Start parsing in background thread
    threading.Thread(target=_parse_job_files, args=(job_id,), daemon=True).start()

    return {"ok": True, "job_id": job_id, "files": file_results}


def _parse_job_files(job_id: str) -> None:
    """Background worker: parse each uploaded file and update job status."""
    task = V2_TASKS.get(job_id)
    if not task:
        return

    job_upload_dir = V2_UPLOADS_DIR / job_id
    parsed_count = 0
    parse_error_count = 0
    all_rows: List[Dict[str, Any]] = []

    for entry in task["files"]:
        if entry.get("upload_status") != "uploaded":
            continue

        stored_as = entry.get("stored_as")
        if not stored_as:
            continue

        filepath = job_upload_dir / stored_as
        if not filepath.exists():
            entry["parse_status"] = "error"
            entry["parse_error"] = "Fil ikke funnet på disk"
            parse_error_count += 1
            continue

        try:
            content = filepath.read_bytes()
            parse_result = parse_file(entry["filename"], content)

            entry["parse_status"] = parse_result.get("parse_status", "error")
            entry["parse_meta"] = {
                k: v for k, v in parse_result.items()
                if k not in ("parse_status", "error", "rows")
            }
            if parse_result.get("error"):
                entry["parse_error"] = parse_result["error"]
                parse_error_count += 1
            else:
                parsed_count += 1

            # Collect rows from this file
            file_rows = parse_result.get("rows", [])
            entry["row_count"] = len(file_rows)
            all_rows.extend(file_rows)

        except Exception as e:
            entry["parse_status"] = "error"
            entry["parse_error"] = str(e)
            parse_error_count += 1

    # Assign sequential row_idx across all raw rows
    for idx, row in enumerate(all_rows):
        row["row_idx"] = idx

    # Deduplicate
    deduped_rows = deduplicate(all_rows)

    # Update overall job status
    if parsed_count == 0 and parse_error_count > 0:
        task["status"] = "error"
    elif parse_error_count > 0:
        task["status"] = "partial_error"
    else:
        task["status"] = "parsed"

    task["parsed_files"] = parsed_count
    task["parse_error_files"] = parse_error_count
    task["total_rows"] = len(all_rows)
    task["rows"] = all_rows
    task["deduped_rows"] = deduped_rows
    task["deduped_count"] = len(deduped_rows)

    # Persist both raw and deduped rows to disk
    job_dir = V2_UPLOADS_DIR / job_id
    try:
        with open(job_dir / "parsed_rows.json", "w", encoding="utf-8") as f:
            json.dump(all_rows, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"V2: Kunne ikke lagre parsed rows: {e}")
    try:
        with open(job_dir / "deduped_rows.json", "w", encoding="utf-8") as f:
            json.dump(deduped_rows, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"V2: Kunne ikke lagre deduped rows: {e}")

    _append_v2_job({
        "job_id": job_id,
        "status": task["status"],
        "parsed_files": parsed_count,
        "parse_error_files": parse_error_count,
        "total_rows": len(all_rows),
        "deduped_count": len(deduped_rows),
    })

    logger.info(
        f"V2 parsing ferdig: job={job_id}, parsed={parsed_count}, errors={parse_error_count}, "
        f"rows={len(all_rows)}, deduped={len(deduped_rows)}"
    )


@v2_router.get("/status/{job_id}")
def v2_status(job_id: str):
    """Get status for a V2 job, including per-file parse results."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    return {
        "job_id": t["job_id"],
        "status": t["status"],
        "created_at": t.get("created_at"),
        "files": t.get("files", []),
        "total_files": t.get("total_files", 0),
        "uploaded_files": t.get("uploaded_files", 0),
        "error_files": t.get("error_files", 0),
        "parsed_files": t.get("parsed_files"),
        "parse_error_files": t.get("parse_error_files"),
        "total_rows": t.get("total_rows", 0),
        "deduped_count": t.get("deduped_count"),
        "match_progress": t.get("match_progress"),
        "matched_ok": t.get("matched_ok"),
        "no_match": t.get("no_match"),
        "match_error": t.get("match_error"),
    }


@v2_router.get("/rows/{job_id}")
def v2_rows(job_id: str, deduped: bool = True):
    """Get rows for a V2 job. Use ?deduped=false for raw parsed rows."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t["status"] == "parsing":
        return JSONResponse({"error": "Parsing pågår fortsatt"}, status_code=409)
    if deduped:
        rows = t.get("deduped_rows", [])
        return {"job_id": job_id, "type": "deduped", "count": len(rows), "rows": rows}
    rows = t.get("rows", [])
    return {"job_id": job_id, "type": "raw", "count": len(rows), "rows": rows}


@v2_router.get("/jobs")
def v2_jobs(limit: int = 50):
    """List V2 jobs (most recent first)."""
    return {"jobs": _load_v2_jobs(limit=limit)}


# ============================================================
# V2 MATCHING
# ============================================================

def _get_catalog_bundle():
    """Lazy import of CATALOG_BUNDLE from app to avoid circular imports."""
    import app as _app
    return _app.CATALOG_BUNDLE


@v2_router.post("/match/{job_id}")
def v2_start_matching(job_id: str, prefer_own_brands: bool = True):
    """Start matching for a parsed V2 job."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)

    if t["status"] not in ("parsed", "partial_error", "error"):
        return JSONResponse(
            {"error": f"Kan ikke matche jobb med status '{t['status']}'. Krever 'parsed' eller 'error'."},
            status_code=400,
        )

    bundle = _get_catalog_bundle()
    if bundle is None:
        return JSONResponse({"error": "Katalog ikke lastet. Last opp katalog først."}, status_code=400)

    t["status"] = "matching"
    _append_v2_job({"job_id": job_id, "status": "matching"})

    threading.Thread(
        target=_run_matching,
        args=(job_id, bundle, prefer_own_brands),
        daemon=True,
    ).start()

    return {"ok": True, "job_id": job_id, "status": "matching"}


def _run_matching(job_id: str, bundle: Any, prefer_own_brands: bool) -> None:
    """Background worker: run matching on deduplicated rows."""
    task = V2_TASKS.get(job_id)
    if not task:
        return

    deduped = task.get("deduped_rows", [])
    if not deduped:
        task["status"] = "error"
        task["match_error"] = "Ingen dedupliserte rader å matche"
        _append_v2_job({"job_id": job_id, "status": "error", "match_error": task["match_error"]})
        return

    def progress_cb(p: float):
        task["match_progress"] = round(p, 3)

    try:
        matched = match_deduped_rows(
            deduped_rows=deduped,
            bundle=bundle,
            top_n=5,
            prefer_own_brands=prefer_own_brands,
            progress_cb=progress_cb,
        )

        task["matched_rows"] = matched
        task["matched_count"] = len(matched)
        task["match_progress"] = 1.0

        matched_ok = sum(1 for r in matched if r.get("match_status") == "matched")
        no_match = sum(1 for r in matched if r.get("match_status") == "no_match")
        task["matched_ok"] = matched_ok
        task["no_match"] = no_match

        task["status"] = "matched"

        # Persist matched rows
        job_dir = V2_UPLOADS_DIR / job_id
        try:
            with open(job_dir / "matched_rows.json", "w", encoding="utf-8") as f:
                json.dump(matched, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"V2: Kunne ikke lagre matched rows: {e}")

        # Initialize review data
        try:
            review_rows = rv.init_review(job_id, matched)
            rv.save_review(job_id, review_rows)
            task["status"] = "review"
        except Exception as e:
            logger.warning(f"V2: Kunne ikke initialisere review: {e}")
            # Fall back to matched status — review can be retried

        _append_v2_job({
            "job_id": job_id,
            "status": task["status"],
            "matched_count": len(matched),
            "matched_ok": matched_ok,
            "no_match": no_match,
        })

        logger.info(f"V2 matching ferdig: job={job_id}, matched={matched_ok}, no_match={no_match}, status={task['status']}")

    except Exception as e:
        logger.exception(f"V2 matching feilet for job={job_id}")
        task["status"] = "error"
        task["match_error"] = str(e)
        _append_v2_job({"job_id": job_id, "status": "error", "match_error": str(e)})


@v2_router.get("/matched/{job_id}")
def v2_matched_rows(job_id: str):
    """Get matched rows with candidates for a V2 job."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t["status"] == "matching":
        return {
            "job_id": job_id,
            "status": "matching",
            "match_progress": t.get("match_progress", 0),
        }
    if t["status"] not in ("matched", "review"):
        return JSONResponse(
            {"error": f"Matching ikke kjørt ennå (status: {t['status']})"},
            status_code=400,
        )
    rows = t.get("matched_rows", [])
    return {
        "job_id": job_id,
        "status": "matched",
        "count": len(rows),
        "matched_ok": t.get("matched_ok", 0),
        "no_match": t.get("no_match", 0),
        "rows": rows,
    }


# ============================================================
# V2 REVIEW ROUTES
# ============================================================

def _require_review_job(job_id: str) -> tuple:
    """Validate job exists and is in review state. Returns (task, error_response)."""
    t = _get_task(job_id)
    if not t:
        return None, JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t.get("status") not in ("review", "matched"):
        return None, JSONResponse(
            {"error": f"Jobb er ikke i review-modus (status: {t.get('status')})"},
            status_code=400,
        )
    if rv.is_locked(job_id):
        return None, JSONResponse({"error": "Jobben er låst"}, status_code=423)
    return t, None


@v2_router.get("/review/{job_id}")
def v2_review(job_id: str):
    """Get full review data with all overrides applied."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t.get("status") not in ("review", "matched"):
        return JSONResponse(
            {"error": f"Review ikke tilgjengelig (status: {t.get('status')})"},
            status_code=400,
        )

    review_rows = rv.load_review(job_id)
    if review_rows is None:
        return JSONResponse({"error": "Review-data ikke funnet"}, status_code=404)

    rows = rv.apply_overrides(review_rows, job_id)
    lock = rv.get_lock_info(job_id)

    # Summary counts
    approved = sum(1 for r in rows if r.get("review_status") == "approved" and not r.get("deleted"))
    rejected = sum(1 for r in rows if r.get("review_status") == "rejected" and not r.get("deleted"))
    pending = sum(1 for r in rows if r.get("review_status") == "pending" and not r.get("deleted"))
    deleted = sum(1 for r in rows if r.get("deleted"))

    return {
        "job_id": job_id,
        "status": t.get("status"),
        "lock": lock,
        "count": len(rows),
        "summary": {"approved": approved, "rejected": rejected, "pending": pending, "deleted": deleted},
        "rows": rows,
    }


@v2_router.post("/review/{job_id}/select")
async def v2_select_candidate(job_id: str, request: Request):
    """Select a candidate for one row. Body: {dedup_idx: int, candidate_idx: int}"""
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    candidate_idx = body.get("candidate_idx")
    if dedup_idx is None or candidate_idx is None:
        return JSONResponse({"error": "dedup_idx og candidate_idx er påkrevd"}, status_code=400)

    rv.save_selection(job_id, int(dedup_idx), int(candidate_idx))

    # Record learning from selection
    try:
        review_rows = rv.load_review(job_id)
        if review_rows:
            for r in review_rows:
                if r.get("dedup_idx") == int(dedup_idx):
                    cands = r.get("candidates", [])
                    if 0 <= int(candidate_idx) < len(cands):
                        record_selection_learning(job_id, r, cands[int(candidate_idx)])
                    break
    except Exception as e:
        logger.warning(f"V2 learning on select failed: {e}")

    return {"ok": True, "dedup_idx": dedup_idx, "candidate_idx": candidate_idx}


@v2_router.post("/review/{job_id}/batch-select")
async def v2_batch_select(job_id: str, request: Request):
    """Auto-select best candidate for all rows that have a match.

    Body: {override_existing: bool} — whether to override rows that already have a selection.
    """
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    override = body.get("override_existing", False)
    review_rows = rv.load_review(job_id)
    if not review_rows:
        return JSONResponse({"error": "Ingen review-data"}, status_code=404)

    sels = rv.load_selections(job_id)
    count = 0

    for row in review_rows:
        idx_str = str(row.get("dedup_idx", ""))
        if not override and idx_str in sels:
            continue
        best = row.get("best_candidate_idx")
        if best is not None:
            sels[idx_str] = best
            count += 1

    rv.save_selections(job_id, sels)
    return {"ok": True, "selected_count": count}


@v2_router.post("/review/{job_id}/decide")
async def v2_decide(job_id: str, request: Request):
    """Set review status for one row. Body: {dedup_idx: int, status: 'approved'|'rejected'|'pending'}"""
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    status = body.get("status")
    if dedup_idx is None or status not in ("approved", "rejected", "pending"):
        return JSONResponse(
            {"error": "dedup_idx og status ('approved'|'rejected'|'pending') er påkrevd"},
            status_code=400,
        )

    rv.save_decision(job_id, int(dedup_idx), status)

    # Record learning when approving
    if status == "approved":
        try:
            review_rows = rv.load_review(job_id)
            if review_rows:
                sels = rv.load_selections(job_id)
                for r in review_rows:
                    if r.get("dedup_idx") == int(dedup_idx):
                        sel_idx = sels.get(str(dedup_idx), r.get("selected_candidate_idx"))
                        cands = r.get("candidates", [])
                        if sel_idx is not None and 0 <= sel_idx < len(cands):
                            record_approval_learning(job_id, r, cands[sel_idx])
                        break
        except Exception as e:
            logger.warning(f"V2 learning on approve failed: {e}")

    return {"ok": True, "dedup_idx": dedup_idx, "status": status}


@v2_router.post("/review/{job_id}/bulk-decide")
async def v2_bulk_decide(job_id: str, request: Request):
    """Set review status for multiple rows. Body: {dedup_indices: [int], status: str}"""
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    indices = body.get("dedup_indices", [])
    status = body.get("status")
    if not indices or status not in ("approved", "rejected", "pending"):
        return JSONResponse({"error": "dedup_indices og status er påkrevd"}, status_code=400)

    decs = rv.load_decisions(job_id)
    now = _utc_now_iso()
    for idx in indices:
        decs[str(idx)] = {"status": status, "decided_at": now}
    rv.save_decisions(job_id, decs)

    return {"ok": True, "count": len(indices), "status": status}


@v2_router.post("/review/{job_id}/extras")
async def v2_extras(job_id: str, request: Request):
    """Update comment for one row.

    Body: {dedup_idx: int, comment?: str}
    """
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    if dedup_idx is None:
        return JSONResponse({"error": "dedup_idx er påkrevd"}, status_code=400)

    rv.save_extra(
        job_id,
        int(dedup_idx),
        comment=body.get("comment"),
    )

    # Handle quantity override (stored separately in extras.json)
    if "quantity_override" in body:
        extras = rv.load_extras(job_id)
        entry = extras.get(str(dedup_idx), {})
        entry["quantity_override"] = body["quantity_override"]
        entry["updated_at"] = _utc_now_iso()
        extras[str(dedup_idx)] = entry
        rv.save_extras(job_id, extras)

    return {"ok": True, "dedup_idx": dedup_idx}


@v2_router.post("/review/{job_id}/search-catalog")
async def v2_search_catalog(job_id: str, request: Request):
    """Fast interactive catalog search using BM25 only (no Claude/embeddings).

    Body: {query: str, limit?: int}
    Returns matching products from both LK and full catalogs, ranked by relevance.
    """
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    query = (body.get("query") or "").strip()
    limit = body.get("limit", 15)
    if not query or len(query) < 2:
        return {"ok": True, "results": []}

    bundle = _get_catalog_bundle()
    if bundle is None:
        return JSONResponse({"error": "Produktkatalog er ikke lastet"}, status_code=503)

    results = []
    seen = set()

    def _extract_gid(row_data):
        if not isinstance(row_data, dict):
            return ""
        gid = str(row_data.get("Katalog: GID") or row_data.get("GID") or "")
        return "" if gid.lower() == "nan" else gid

    def _make_result(item, score, source):
        artnr = str(item.artnr)
        price, _ = bundle.price_for_artnr(artnr)
        row_data = item.row if hasattr(item, "row") else {}
        desc = ""
        spec = ""
        producer = ""
        gid = ""
        if isinstance(row_data, dict):
            desc = str(row_data.get("Katalog: Item Description") or row_data.get("Item Description") or row_data.get("Description") or "")
            spec = str(row_data.get("Katalog: Specification") or row_data.get("Specification") or row_data.get("Spesifikasjon") or "")
            producer = str(row_data.get("Katalog: Producer Name") or row_data.get("Producer Name") or row_data.get("Produsent") or "")
            gid = _extract_gid(row_data)
        return {
            "our_artnr": artnr,
            "our_description": desc,
            "our_specification": spec,
            "our_producer": producer,
            "our_gid": gid,
            "our_unit_price": price,
            "matched_from": source,
            "match_quality": "Søk",
            "_score": round(score, 4),
        }

    # Use BM25 directly for fast, deterministic results — skip Claude/embeddings
    query_upper = query.upper()
    for catalog, source in [(bundle.lk, "lk"), (bundle.full, "full")]:
        bm25 = catalog.bm25_index

        # First: exact substring match on artnr or text (catches short codes like "CRP")
        exact_hits = []
        for item in bm25.docs:
            item_text_upper = item.text.upper() if item.text else ""
            artnr_upper = item.artnr.upper() if item.artnr else ""
            if query_upper in artnr_upper or query_upper in item_text_upper:
                # Score: prefer artnr match, then position in text
                if query_upper in artnr_upper:
                    s = 1000.0
                else:
                    pos = item_text_upper.find(query_upper)
                    s = 500.0 - pos * 0.1  # earlier match = higher score
                exact_hits.append((item, s))
        exact_hits.sort(key=lambda x: -x[1])

        # Then: BM25 token search
        bm25_hits = bm25.top_n(query, n=limit * 3)

        # Merge: exact matches first, then BM25 (deduplicated)
        seen_in_catalog = set()
        merged = []
        for item, score in exact_hits:
            if item.artnr not in seen_in_catalog:
                merged.append((item, score))
                seen_in_catalog.add(item.artnr)
        for item, score in bm25_hits:
            if item.artnr not in seen_in_catalog:
                merged.append((item, score))
                seen_in_catalog.add(item.artnr)

        for item, score in merged:
            if item.artnr in seen:
                continue
            results.append(_make_result(item, score, source))
            seen.add(item.artnr)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    # Sort globally by score descending
    results.sort(key=lambda r: -r.get("_score", 0))
    # Remove internal score from response
    for r in results:
        r.pop("_score", None)

    return {"ok": True, "results": results[:limit]}


@v2_router.post("/review/{job_id}/replace-candidate")
async def v2_replace_candidate(job_id: str, request: Request):
    """Replace/add a candidate from catalog search. Body: {dedup_idx: int, our_artnr: str}

    Looks up the product in the catalog, adds it as a new candidate, and selects it.
    Returns the updated row data.
    """
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    our_artnr = (body.get("our_artnr") or "").strip()
    if dedup_idx is None or not our_artnr:
        return JSONResponse({"error": "dedup_idx og our_artnr er påkrevd"}, status_code=400)

    bundle = _get_catalog_bundle()
    if bundle is None:
        return JSONResponse({"error": "Produktkatalog er ikke lastet"}, status_code=503)

    # Look up price
    price, price_source = bundle.price_for_artnr(our_artnr)

    # Try to find the product details from catalog items
    desc = ""
    spec = ""
    producer = ""
    gid = ""
    for catalog, source in [(bundle.lk, "lk"), (bundle.full, "full")]:
        for item in catalog.items:
            if str(getattr(item, "artnr", "")).strip() == our_artnr:
                row_data = item.row if hasattr(item, "row") else {}
                if isinstance(row_data, dict):
                    desc = str(row_data.get("Katalog: Item Description") or row_data.get("Item Description") or row_data.get("Description") or "")
                    spec = str(row_data.get("Katalog: Specification") or row_data.get("Specification") or row_data.get("Spesifikasjon") or "")
                    producer = str(row_data.get("Katalog: Producer Name") or row_data.get("Producer Name") or row_data.get("Produsent") or "")
                    gid_raw = str(row_data.get("Katalog: GID") or row_data.get("GID") or "")
                    gid = "" if gid_raw.lower() == "nan" else gid_raw
                break
        if desc:
            break

    # Load review data and update the row
    review_rows = rv.load_review(job_id)
    if review_rows is None:
        return JSONResponse({"error": "Review-data ikke funnet"}, status_code=404)

    row = None
    for r in review_rows:
        if r.get("dedup_idx") == int(dedup_idx):
            row = r
            break
    if row is None:
        return JSONResponse({"error": f"Rad {dedup_idx} ikke funnet"}, status_code=404)

    # Build new candidate
    candidates = row.get("candidates", [])
    total_units = row.get("total_units", 0) or 0
    competitor_line = row.get("competitor_line_amount")

    new_cand = {
        "candidate_idx": len(candidates),
        "our_artnr": our_artnr,
        "our_description": desc,
        "our_specification": spec,
        "our_producer": producer,
        "our_gid": gid,
        "our_unit_price": price,
        "price_source": price_source,
        "match_quality": "Manuelt valgt",
        "matched_from": "manual",
    }

    # Calculate prices for new candidate
    if price is not None and total_units > 0:
        new_cand["our_comparable_line_price"] = round(total_units * price, 2)
        if competitor_line is not None:
            new_cand["savings_amount"] = round(competitor_line - new_cand["our_comparable_line_price"], 2)
        else:
            new_cand["savings_amount"] = None
    else:
        new_cand["our_comparable_line_price"] = None
        new_cand["savings_amount"] = None

    # Check if artnr already exists in candidates
    existing_idx = None
    for c in candidates:
        if c.get("our_artnr") == our_artnr:
            existing_idx = c["candidate_idx"]
            break

    if existing_idx is not None:
        # Select existing candidate
        selected_idx = existing_idx
    else:
        # Add new candidate
        candidates.append(new_cand)
        selected_idx = new_cand["candidate_idx"]

    row["candidates"] = candidates
    row["selected_candidate_idx"] = selected_idx
    row["match_status"] = "matched"

    # Persist updated review.json and selection
    rv.save_review(job_id, review_rows)
    rv.save_selection(job_id, int(dedup_idx), selected_idx)

    # Apply overrides to get the final state for this row
    overridden = rv.apply_overrides([row], job_id)
    final_row = overridden[0] if overridden else row

    return {
        "ok": True,
        "dedup_idx": dedup_idx,
        "row": {
            "candidates": final_row.get("candidates", []),
            "selected_candidate_idx": final_row.get("selected_candidate_idx"),
            "our_unit_price": final_row.get("our_unit_price"),
            "our_comparable_line_price": final_row.get("our_comparable_line_price"),
            "savings_amount": final_row.get("savings_amount"),
        },
    }


@v2_router.post("/review/{job_id}/delete")
async def v2_delete_row(job_id: str, request: Request):
    """Soft-delete a row. Body: {dedup_idx: int}"""
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    if dedup_idx is None:
        return JSONResponse({"error": "dedup_idx er påkrevd"}, status_code=400)

    rv.save_deletion(job_id, int(dedup_idx))
    return {"ok": True, "dedup_idx": dedup_idx, "deleted": True}


@v2_router.post("/review/{job_id}/restore")
async def v2_restore_row(job_id: str, request: Request):
    """Restore a soft-deleted row. Body: {dedup_idx: int}"""
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    dedup_idx = body.get("dedup_idx")
    if dedup_idx is None:
        return JSONResponse({"error": "dedup_idx er påkrevd"}, status_code=400)

    rv.remove_deletion(job_id, int(dedup_idx))
    return {"ok": True, "dedup_idx": dedup_idx, "deleted": False}


@v2_router.post("/review/{job_id}/add-row")
async def v2_add_row(job_id: str, request: Request):
    """Add a manual row to the review. Body: {description, price, competitor_name?, competitor_artnr?, specification?, quantity?}

    Creates a new review row, runs matching, and returns the full row.
    """
    t, err = _require_review_job(job_id)
    if err:
        return err

    body = await request.json()
    description = (body.get("description") or "").strip()
    price = body.get("price")
    if not description:
        return JSONResponse({"error": "Beskrivelse er påkrevd"}, status_code=400)
    if price is None:
        return JSONResponse({"error": "Pris er påkrevd"}, status_code=400)
    try:
        price = float(price)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Ugyldig pris"}, status_code=400)

    competitor_name = (body.get("competitor_name") or "").strip()
    competitor_artnr = (body.get("competitor_artnr") or "").strip()
    specification = (body.get("specification") or "").strip()
    quantity = body.get("quantity")
    if quantity is not None:
        try:
            quantity = float(quantity)
        except (ValueError, TypeError):
            quantity = 1.0
    else:
        quantity = 1.0

    review_rows = rv.load_review(job_id)
    if review_rows is None:
        return JSONResponse({"error": "Review-data ikke funnet"}, status_code=404)

    # Assign next dedup_idx
    max_idx = max((r.get("dedup_idx", 0) for r in review_rows), default=-1)
    new_idx = max_idx + 1

    total_units = quantity
    competitor_line_amount = round(price * total_units, 2)

    # Build the new row with all fields used by export/UI
    new_row = {
        "dedup_idx": new_idx,
        "description": description,
        "competitor": competitor_name,
        "competitor_artnr": competitor_artnr,
        "specification": specification,
        "quantity_purchased": quantity,
        "packaging_text": "",
        "packaging_count": 1,
        "total_units": total_units,
        "competitor_unit_price": price,
        "competitor_line_amount": competitor_line_amount,
        "merged_from_count": 1,
        "merge_warning": False,
        "inconsistent_fields": [],
        "manual_row": True,
        # Review fields
        "review_status": "pending",
        "selected_candidate_idx": None,
        "comment": "",
        "deleted": False,
        "candidates": [],
        "best_candidate_idx": None,
        "match_status": "no_match",
        "our_unit_price": None,
        "our_comparable_line_price": None,
        "savings_amount": None,
    }

    # Run matching if catalog is available
    bundle = _get_catalog_bundle()
    if bundle:
        try:
            from v2.matching import _match_single_row
            matched = _match_single_row(new_row, bundle, top_n=5, prefer_own_brands=True)
            # Merge matched fields into new_row
            new_row["candidates"] = matched.get("candidates", [])
            new_row["best_candidate_idx"] = matched.get("best_candidate_idx")
            new_row["match_status"] = matched.get("match_status", "no_match")
            new_row["our_unit_price"] = matched.get("our_unit_price")
            new_row["our_comparable_line_price"] = matched.get("our_comparable_line_price")
            new_row["savings_amount"] = matched.get("savings_amount")
            if new_row["best_candidate_idx"] is not None:
                new_row["selected_candidate_idx"] = new_row["best_candidate_idx"]
        except Exception as e:
            logger.warning(f"V2 add-row matching failed: {e}")

    # Init review fields
    from v2.persistence import init_review
    review_ready = init_review(job_id, [new_row])
    final_row = review_ready[0] if review_ready else new_row

    # Append to review.json
    review_rows.append(final_row)
    rv.save_review(job_id, review_rows)

    # Apply overrides to return the proper state
    overridden = rv.apply_overrides([final_row], job_id)
    out = overridden[0] if overridden else final_row

    return {"ok": True, "row": out}


@v2_router.post("/review/{job_id}/lock")
async def v2_lock(job_id: str, request: Request):
    """Lock the job to prevent further edits."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    rv.set_lock(job_id, True, locked_by=body.get("locked_by", ""))
    return {"ok": True, "locked": True}


@v2_router.post("/review/{job_id}/unlock")
def v2_unlock(job_id: str):
    """Unlock the job to allow edits."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    rv.set_lock(job_id, False)
    return {"ok": True, "locked": False}


# ============================================================
# V2 LEARNING – simple learning from review decisions
# ============================================================
_LEARNING_DIR = V2_DIR / "learning"
_LEARNING_DIR.mkdir(parents=True, exist_ok=True)
_LEARNING_FILE = _LEARNING_DIR / "learnings.jsonl"


def _save_learning(entry: Dict[str, Any]) -> None:
    """Append a learning entry to the JSONL file."""
    entry["learned_at"] = _utc_now_iso()
    try:
        with open(_LEARNING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"V2 learning: kunne ikke lagre: {e}")


def load_learnings() -> List[Dict[str, Any]]:
    """Load all learning entries."""
    if not _LEARNING_FILE.exists():
        return []
    entries = []
    try:
        with open(_LEARNING_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return entries


def record_selection_learning(job_id: str, row: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    """Record when a user selects a match (positive signal)."""
    _save_learning({
        "type": "selection",
        "job_id": job_id,
        "competitor_description": row.get("description", ""),
        "competitor_artnr": row.get("competitor_artnr", ""),
        "competitor": row.get("competitor", ""),
        "our_artnr": candidate.get("our_artnr", ""),
        "our_description": candidate.get("our_description", ""),
        "match_quality": candidate.get("match_quality", ""),
    })


def record_approval_learning(job_id: str, row: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    """Record when a user approves a row (strong positive signal)."""
    _save_learning({
        "type": "approval",
        "job_id": job_id,
        "competitor_description": row.get("description", ""),
        "competitor_artnr": row.get("competitor_artnr", ""),
        "competitor": row.get("competitor", ""),
        "our_artnr": candidate.get("our_artnr", ""),
        "our_description": candidate.get("our_description", ""),
        "match_quality": candidate.get("match_quality", ""),
    })


@v2_router.get("/learning")
def v2_get_learnings(limit: int = 200):
    """Get recorded learnings."""
    entries = load_learnings()
    return {"count": len(entries), "learnings": entries[-limit:]}


@v2_router.get("/review/{job_id}/ui", response_class=HTMLResponse)
def v2_review_ui(job_id: str):
    """Serve the review UI page."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    html_path = Path(__file__).parent / "templates" / "review.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ============================================================
# V2 EXPORT
# ============================================================

V2_EXPORTS_DIR = V2_DIR / "exports"
V2_EXPORTS_DIR.mkdir(exist_ok=True)


@v2_router.get("/export/{job_id}")
def v2_export(job_id: str, format: str = "xlsx", show_line_prices: bool = True):
    """Generate and download export from review state.

    Query params:
        format: 'xlsx' (default) or 'pdf'
        show_line_prices: true (default) or false
    """
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t.get("status") not in ("review", "matched"):
        return JSONResponse(
            {"error": f"Eksport ikke tilgjengelig (status: {t.get('status')})"},
            status_code=400,
        )

    review_rows = rv.load_review(job_id)
    if review_rows is None:
        return JSONResponse({"error": "Review-data ikke funnet"}, status_code=404)

    rows = rv.apply_overrides(review_rows, job_id)

    try:
        if format == "pdf":
            pdf_bytes = generate_export_pdf(rows, show_line_prices=show_line_prices)
            suffix = "_med_priser" if show_line_prices else "_uten_priser"
            filename = f"V2_prissammenligning_{job_id[:12]}{suffix}.pdf"
            return StreamingResponse(
                BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        else:
            xlsx_bytes = generate_export_xlsx(rows, show_line_prices=show_line_prices)

            # Persist export to disk
            export_path = V2_EXPORTS_DIR / f"{job_id}.xlsx"
            try:
                export_path.write_bytes(xlsx_bytes)
            except Exception as e:
                logger.warning(f"V2: Kunne ikke lagre eksport til disk: {e}")

            suffix = "_med_priser" if show_line_prices else "_uten_priser"
            filename = f"V2_prissammenligning_{job_id[:12]}{suffix}.xlsx"
            return StreamingResponse(
                BytesIO(xlsx_bytes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
    except Exception as e:
        logger.exception(f"V2 eksport feilet for job={job_id}")
        return JSONResponse({"error": f"Eksport feilet: {e}"}, status_code=500)


# ============================================================
# V2 PRICE DATABASE
# ============================================================

@v2_router.post("/pricedb/commit/{job_id}")
def v2_pricedb_commit(job_id: str):
    """Commit approved review rows to the price database."""
    t = _get_task(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    if t.get("status") not in ("review", "matched"):
        return JSONResponse(
            {"error": f"Kan ikke committe jobb med status '{t.get('status')}'"},
            status_code=400,
        )

    review_rows = rv.load_review(job_id)
    if review_rows is None:
        return JSONResponse({"error": "Review-data ikke funnet"}, status_code=404)

    rows = rv.apply_overrides(review_rows, job_id)
    count = pricedb.commit_job(job_id, rows)
    return {"ok": True, "job_id": job_id, "committed": count}


@v2_router.get("/pricedb")
def v2_pricedb_search(
    q: str = "",
    competitor: str = "",
    competitor_artnr: str = "",
    our_artnr: str = "",
    limit: int = 500,
):
    """Search the price database."""
    records = pricedb.search(
        text=q or None,
        competitor=competitor or None,
        competitor_artnr=competitor_artnr or None,
        our_artnr=our_artnr or None,
        limit=limit,
    )
    return {"count": len(records), "records": records}


@v2_router.get("/pricedb/ui", response_class=HTMLResponse)
def v2_pricedb_ui():
    """Serve the price database UI page."""
    html_path = Path(__file__).parent / "templates" / "pricedb.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ============================================================
# V2 FRONTEND
# ============================================================
V2_INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Prisammenligning V2</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 32px; color:#1a1a1a; }
    .card { border:1px solid #e6e6e6; padding:24px; border-radius:12px; max-width:900px; }
    a { color:#1F4E79; text-decoration:none; }
    a:hover { text-decoration:underline; }
    button { background:#1F4E79; color:white; border:0; padding:10px 14px; border-radius:8px; cursor:pointer; }
    button:disabled { opacity:0.5; cursor:not-allowed; }
    .muted { color:#666; }
    .file-list { margin:12px 0; }
    .file-item { padding:6px 10px; border:1px solid #e6e6e6; border-radius:6px; margin:4px 0; display:flex; justify-content:space-between; align-items:center; }
    .file-item.error { border-color:#e74c3c; background:#fdf2f2; }
    .file-item.ok { border-color:#27ae60; background:#f2fdf5; }
    .file-item.parsing { border-color:#f39c12; background:#fffdf2; }
    .tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }
    .tag-pdf { background:#fff3e0; color:#e65100; }
    .tag-xlsx { background:#e3f2fd; color:#1565c0; }
    .tag-error { background:#ffebee; color:#c62828; }
    .tag-parsed { background:#e8f5e9; color:#2e7d32; }
    .tag-ocr { background:#f3e5f5; color:#6a1b9a; }
    .result-box { margin-top:16px; padding:14px; background:#f8f9fa; border-radius:8px; border:1px solid #e6e6e6; display:none; }
    table { width:100%; border-collapse:collapse; margin-top:12px; }
    th, td { text-align:left; border-bottom:1px solid #eee; padding:8px 6px; }
    th { font-size:13px; color:#444; }
    .right { text-align:right; }
    .parse-detail { font-size:12px; color:#888; margin-top:2px; }
  </style>
</head>
<body>
  <div style="margin-bottom:12px;"><a href="/">&larr; Tilbake til V1</a> &nbsp; <a href="/v2/pricedb/ui">Prisdatabase</a></div>
  <div class="card">
    <h2>Prisammenligning V2</h2>

    <h3>Last opp filer</h3>
    <p class="muted">Velg en eller flere PDF-fakturaer og/eller Excel-filer (.xlsx).</p>
    <input id="files" type="file" accept=".xlsx,.pdf" multiple />
    <div id="selectedFiles" class="file-list"></div>
    <button id="btnUpload" disabled>Last opp og analyser</button>
    <span id="uploadStatus" class="muted" style="margin-left:12px;"></span>

    <div id="resultBox" class="result-box">
      <h4 style="margin-top:0;">Jobb opprettet</h4>
      <div><b>Jobb-ID:</b> <code id="resultJobId"></code></div>
      <div><b>Status:</b> <span id="resultStatus"></span></div>
      <div id="resultFiles" class="file-list"></div>
      <div id="matchActions" style="margin-top:12px;display:none;">
        <button id="btnMatch" onclick="startMatching()">Start matching</button>
        <span id="matchStatus" class="muted" style="margin-left:12px;"></span>
      </div>
    </div>

    <hr style="border:none;border-top:1px solid #eee;margin:24px 0;"/>

    <h3>V2-jobber</h3>
    <div id="jobsList"></div>
  </div>

<script>
const fileInput = document.getElementById('files');
const btnUpload = document.getElementById('btnUpload');
const selectedFilesEl = document.getElementById('selectedFiles');

fileInput.addEventListener('change', () => {
  const fl = fileInput.files;
  btnUpload.disabled = fl.length === 0;
  let html = '';
  for (const f of fl) {
    const ext = f.name.split('.').pop().toLowerCase();
    const tagClass = ext === 'pdf' ? 'tag-pdf' : 'tag-xlsx';
    const size = (f.size / 1024).toFixed(1);
    html += '<div class="file-item"><span>' + esc(f.name) +
      ' <span class="tag ' + tagClass + '">' + esc(ext.toUpperCase()) + '</span></span>' +
      '<span class="muted">' + size + ' KB</span></div>';
  }
  selectedFilesEl.innerHTML = html;
});

btnUpload.addEventListener('click', async () => {
  const fl = fileInput.files;
  if (fl.length === 0) return;

  btnUpload.disabled = true;
  document.getElementById('uploadStatus').textContent = 'Laster opp...';

  const fd = new FormData();
  for (const f of fl) fd.append('files', f);

  try {
    const resp = await fetch('/v2/upload', { method: 'POST', body: fd });
    const data = await resp.json();

    if (!resp.ok) {
      document.getElementById('uploadStatus').textContent = 'Feil: ' + (data.error || 'ukjent');
      showFileResults(data.files || []);
      btnUpload.disabled = false;
      return;
    }

    document.getElementById('uploadStatus').textContent = '';
    const jobId = data.job_id;
    document.getElementById('resultJobId').textContent = jobId;
    document.getElementById('resultStatus').textContent = 'Parsing...';
    showFileResults(data.files || []);
    document.getElementById('resultBox').style.display = 'block';

    fileInput.value = '';
    selectedFilesEl.innerHTML = '';

    // Poll for parse completion
    pollParseStatus(jobId);
  } catch (e) {
    document.getElementById('uploadStatus').textContent = 'Nettverksfeil: ' + e.message;
  }
  btnUpload.disabled = false;
});

let currentJobId = null;

async function pollParseStatus(jobId) {
  currentJobId = jobId;
  try {
    const resp = await fetch('/v2/status/' + jobId);
    const data = await resp.json();
    updateStatusDisplay(data);
    showDetailedFileResults(data.files || []);

    if (data.status === 'parsing') {
      setTimeout(() => pollParseStatus(jobId), 1500);
      return;
    }

    // Show match button when parsing is done
    if (data.status === 'parsed' || data.status === 'partial_error') {
      document.getElementById('matchActions').style.display = 'block';
      document.getElementById('btnMatch').disabled = false;
      document.getElementById('matchStatus').textContent = '';
    }

    loadJobs();
  } catch (e) {
    document.getElementById('resultStatus').textContent = 'Polling feilet';
  }
}

function statusLabel(s) {
  const map = {
    'uploaded': 'Lastet opp',
    'parsing': 'Analyserer...',
    'parsed': 'Ferdig analysert',
    'partial_error': 'Delvis feil',
    'matching': 'Matcher...',
    'matched': 'Matchet',
    'review': 'Klar for review',
    'error': 'Feil',
  };
  return map[s] || s;
}

function updateStatusDisplay(data) {
  let label = statusLabel(data.status);
  if (data.total_rows > 0) {
    label += ' (' + data.total_rows + ' rader';
    if (data.deduped_count != null && data.deduped_count !== data.total_rows)
      label += ', ' + data.deduped_count + ' unike';
    label += ')';
  }
  if (data.status === 'matching' && data.match_progress != null) {
    label += ' ' + Math.round(data.match_progress * 100) + '%';
  }
  if (data.status === 'matched' || data.status === 'review') {
    label += ' — ' + (data.matched_ok || 0) + ' treff, ' + (data.no_match || 0) + ' uten';
  }
  if (data.match_error) {
    label += ' — Feil: ' + data.match_error;
  }
  document.getElementById('resultStatus').textContent = label;
}

async function startMatching() {
  if (!currentJobId) return;
  document.getElementById('btnMatch').disabled = true;
  document.getElementById('matchStatus').textContent = 'Starter matching...';

  try {
    const resp = await fetch('/v2/match/' + currentJobId, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      document.getElementById('matchStatus').textContent = 'Feil: ' + (data.error || 'ukjent');
      document.getElementById('btnMatch').disabled = false;
      return;
    }
    document.getElementById('matchStatus').textContent = '';
    pollMatchStatus(currentJobId);
  } catch (e) {
    document.getElementById('matchStatus').textContent = 'Nettverksfeil: ' + e.message;
    document.getElementById('btnMatch').disabled = false;
  }
}

async function pollMatchStatus(jobId) {
  try {
    const resp = await fetch('/v2/status/' + jobId);
    const data = await resp.json();
    updateStatusDisplay(data);

    if (data.status === 'matching') {
      setTimeout(() => pollMatchStatus(jobId), 2000);
      return;
    }
    // Show review link when matching is done
    if (data.status === 'review' || data.status === 'matched') {
      document.getElementById('matchActions').style.display = 'block';
      document.getElementById('matchActions').innerHTML = '<a href="/v2/review/' + jobId + '/ui"><button>Apne review</button></a>';
    } else if (data.status === 'error') {
      // Re-enable match button so user can retry
      document.getElementById('matchActions').style.display = 'block';
      document.getElementById('btnMatch').disabled = false;
    } else {
      document.getElementById('matchActions').style.display = 'none';
    }
    loadJobs();
  } catch (e) {
    document.getElementById('matchStatus').textContent = 'Polling feilet';
  }
}

function showFileResults(files) {
  let html = '';
  for (const f of files) {
    const ok = f.upload_status === 'uploaded';
    const cls = ok ? 'ok' : 'error';
    const info = ok
      ? '<span class="tag tag-' + (f.type || 'xlsx') + '">' + esc((f.type || '').toUpperCase()) + '</span> ' + ((f.size_bytes / 1024).toFixed(1)) + ' KB'
      : '<span class="tag tag-error">' + esc(f.error || 'Feil') + '</span>';
    html += '<div class="file-item ' + cls + '"><span>' + esc(f.filename) + '</span><span>' + info + '</span></div>';
  }
  document.getElementById('resultFiles').innerHTML = html;
}

function showDetailedFileResults(files) {
  let html = '';
  for (const f of files) {
    const uploadOk = f.upload_status === 'uploaded';
    if (!uploadOk) {
      html += '<div class="file-item error"><span>' + esc(f.filename) + '</span><span class="tag tag-error">' + esc(f.error || 'Opplasting feilet') + '</span></div>';
      continue;
    }
    const parseStatus = f.parse_status || 'pending';
    const cls = parseStatus === 'parsed' ? 'ok' : (parseStatus === 'error' ? 'error' : 'parsing');
    const meta = f.parse_meta || {};

    let tags = '<span class="tag tag-' + (f.type || 'xlsx') + '">' + esc((f.type || '').toUpperCase()) + '</span> ';
    if (parseStatus === 'parsed') tags += '<span class="tag tag-parsed">Analysert</span> ';
    if (parseStatus === 'error') tags += '<span class="tag tag-error">' + esc(f.parse_error || 'Feil') + '</span> ';
    if (meta.ocr_used) tags += '<span class="tag tag-ocr">OCR</span> ';

    let detail = '';
    if (f.type === 'pdf' && parseStatus === 'parsed') {
      detail = (meta.page_count || '?') + ' sider, ' + (meta.text_length || 0) + ' tegn';
      if (meta.detected_source) detail += ', kilde: ' + meta.detected_source;
      if (meta.ocr_needed && !meta.ocr_used) detail += ', OCR ikke tilgjengelig';
    }
    if (f.type === 'xlsx' && parseStatus === 'parsed') {
      const sheets = meta.sheets || [];
      detail = sheets.length + ' ark, ' + (meta.total_rows || 0) + ' rader';
    }
    if (f.row_count != null && f.row_count > 0) detail += (detail ? ', ' : '') + f.row_count + ' produktrader';

    html += '<div class="file-item ' + cls + '"><div><span>' + esc(f.filename) + '</span> ' + tags;
    if (detail) html += '<div class="parse-detail">' + esc(detail) + '</div>';
    html += '</div><span class="muted">' + ((f.size_bytes / 1024).toFixed(1)) + ' KB</span></div>';
  }
  document.getElementById('resultFiles').innerHTML = html;
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function formatOsloTime(iso) {
  if (!iso) return '';
  try {
    return new Intl.DateTimeFormat('nb-NO', {
      timeZone: 'Europe/Oslo', year:'numeric', month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', second:'2-digit',
    }).format(new Date(iso));
  } catch { return iso; }
}

async function loadJobs() {
  try {
    const resp = await fetch('/v2/jobs?limit=50');
    const data = await resp.json();
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      document.getElementById('jobsList').innerHTML = '<p class="muted">Ingen V2-jobber enda.</p>';
      return;
    }
    let html = '<table><thead><tr><th>Tidspunkt</th><th>Status</th><th class="right">Filer</th><th class="right">Rader</th><th class="right">Match</th><th></th><th>Jobb-ID</th></tr></thead><tbody>';
    for (const j of jobs) {
      const st = statusLabel(j.status);
      html += '<tr><td>' + esc(formatOsloTime(j.created_at)) + '</td><td>' + esc(st) +
        '</td><td class="right">' + esc(j.uploaded_files || 0) + ' lastet opp';
      if (j.parsed_files != null) html += ', ' + esc(j.parsed_files) + ' analysert';
      if (j.parse_error_files) html += ', ' + esc(j.parse_error_files) + ' feil';
      html += '</td><td class="right">' + esc(j.total_rows || 0);
      html += '</td><td class="right">';
      if (j.matched_ok != null) html += esc(j.matched_ok) + ' treff';
      if (j.no_match) html += ', ' + esc(j.no_match) + ' uten';
      html += '</td><td>';
      if (j.status === 'review' || j.status === 'matched') html += '<a href="/v2/review/' + esc(j.job_id) + '/ui">Review</a>';
      html += '</td><td><code>' + esc((j.job_id || '').substring(0, 12)) + '&hellip;</code></td></tr>';
    }
    html += '</tbody></table>';
    document.getElementById('jobsList').innerHTML = html;
  } catch (e) {
    document.getElementById('jobsList').innerHTML = '<p class="muted">Kunne ikke laste jobber.</p>';
  }
}

loadJobs();
</script>
</body>
</html>"""
