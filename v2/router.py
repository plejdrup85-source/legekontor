import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from v2.parsing import classify_file, parse_file
from v2.normalize import deduplicate
from v2.matching import match_deduped_rows
from v2 import persistence as rv
from v2.export import generate_export_xlsx
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
# V2 IN-MEMORY TASK STATE
# ============================================================
V2_TASKS: Dict[str, Dict[str, Any]] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
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
    approved = sum(1 for r in rows if r.get("review_status") == "approved")
    rejected = sum(1 for r in rows if r.get("review_status") == "rejected")
    pending = sum(1 for r in rows if r.get("review_status") == "pending")

    return {
        "job_id": job_id,
        "status": t.get("status"),
        "lock": lock,
        "count": len(rows),
        "summary": {"approved": approved, "rejected": rejected, "pending": pending},
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
    """Update comment and/or strategy for one row.

    Body: {dedup_idx: int, comment?: str, strategy?: str}
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
        strategy=body.get("strategy"),
    )
    return {"ok": True, "dedup_idx": dedup_idx}


@v2_router.post("/review/{job_id}/lock")
async def v2_lock(job_id: str, request: Request):
    """Lock the job to prevent further edits."""
    t = V2_TASKS.get(job_id)
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
    t = V2_TASKS.get(job_id)
    if not t:
        return JSONResponse({"error": "Ukjent jobb-ID"}, status_code=404)
    rv.set_lock(job_id, False)
    return {"ok": True, "locked": False}


@v2_router.get("/review/{job_id}/ui", response_class=HTMLResponse)
def v2_review_ui(job_id: str):
    """Serve the review UI page."""
    t = V2_TASKS.get(job_id)
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
def v2_export(job_id: str):
    """Generate and download Excel export from review state."""
    t = V2_TASKS.get(job_id)
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
        xlsx_bytes = generate_export_xlsx(rows)
    except Exception as e:
        logger.exception(f"V2 eksport feilet for job={job_id}")
        return JSONResponse({"error": f"Eksport feilet: {e}"}, status_code=500)

    # Persist export to disk
    export_path = V2_EXPORTS_DIR / f"{job_id}.xlsx"
    try:
        export_path.write_bytes(xlsx_bytes)
    except Exception as e:
        logger.warning(f"V2: Kunne ikke lagre eksport til disk: {e}")

    filename = f"V2_prissammenligning_{job_id[:12]}.xlsx"
    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
# V2 PRICE DATABASE
# ============================================================

@v2_router.post("/pricedb/commit/{job_id}")
def v2_pricedb_commit(job_id: str):
    """Commit approved review rows to the price database."""
    t = V2_TASKS.get(job_id)
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
