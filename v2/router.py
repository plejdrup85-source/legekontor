import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from v2.parsing import classify_file, parse_file
from v2.normalize import deduplicate

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
  <div style="margin-bottom:12px;"><a href="/">&larr; Tilbake til V1</a></div>
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

async function pollParseStatus(jobId) {
  try {
    const resp = await fetch('/v2/status/' + jobId);
    const data = await resp.json();
    let label = statusLabel(data.status);
    if (data.total_rows > 0) {
      label += ' (' + data.total_rows + ' rader';
      if (data.deduped_count != null && data.deduped_count !== data.total_rows)
        label += ', ' + data.deduped_count + ' unike';
      label += ')';
    }
    document.getElementById('resultStatus').textContent = label;
    showDetailedFileResults(data.files || []);

    if (data.status === 'parsing') {
      setTimeout(() => pollParseStatus(jobId), 1500);
      return;
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
    'error': 'Feil',
  };
  return map[s] || s;
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
    let html = '<table><thead><tr><th>Tidspunkt</th><th>Status</th><th class="right">Filer</th><th class="right">Rader</th><th>Jobb-ID</th></tr></thead><tbody>';
    for (const j of jobs) {
      const st = statusLabel(j.status);
      html += '<tr><td>' + esc(formatOsloTime(j.created_at)) + '</td><td>' + esc(st) +
        '</td><td class="right">' + esc(j.uploaded_files || 0) + ' lastet opp';
      if (j.parsed_files != null) html += ', ' + esc(j.parsed_files) + ' analysert';
      if (j.parse_error_files) html += ', ' + esc(j.parse_error_files) + ' feil';
      html += '</td><td class="right">' + esc(j.total_rows || 0) +
        '</td><td><code>' + esc((j.job_id || '').substring(0, 12)) + '&hellip;</code></td></tr>';
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
