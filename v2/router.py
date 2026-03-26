import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

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
    """Accept one or more PDF/XLSX files, create a V2 job."""
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
                entry["status"] = "error"
                entry["error"] = "Tom fil"
                file_results.append(entry)
                continue

            lower = filename.lower()
            is_pdf = lower.endswith(".pdf") or (len(content) >= 4 and content[:4] == b"%PDF")
            is_xlsx = lower.endswith(".xlsx") or (len(content) >= 2 and content[:2] == b"PK")

            if not (is_pdf or is_xlsx):
                entry["status"] = "error"
                entry["error"] = "Ugyldig filformat. Kun .xlsx og .pdf er støttet."
                file_results.append(entry)
                continue

            file_type = "pdf" if is_pdf else "xlsx"
            entry["type"] = file_type
            entry["size_bytes"] = len(content)

            # Save file to disk
            safe_name = f"{len(file_results):03d}_{filename}"
            dest = job_upload_dir / safe_name
            dest.write_bytes(content)
            entry["stored_as"] = safe_name
            entry["status"] = "uploaded"

        except Exception as e:
            entry["status"] = "error"
            entry["error"] = str(e)

        file_results.append(entry)

    uploaded_count = sum(1 for fr in file_results if fr.get("status") == "uploaded")
    error_count = sum(1 for fr in file_results if fr.get("status") == "error")

    if uploaded_count == 0:
        # Clean up empty directory
        try:
            job_upload_dir.rmdir()
        except Exception:
            pass
        return JSONResponse(
            {"error": "Ingen gyldige filer ble lastet opp.", "files": file_results},
            status_code=400,
        )

    # Register job
    task_entry = {
        "job_id": job_id,
        "created_at": now,
        "status": "uploaded",
        "files": file_results,
        "total_files": len(file_results),
        "uploaded_files": uploaded_count,
        "error_files": error_count,
    }

    V2_TASKS[job_id] = task_entry
    _append_v2_job(task_entry)

    return {"ok": True, "job_id": job_id, "files": file_results}


@v2_router.get("/status/{job_id}")
def v2_status(job_id: str):
    """Get status for a V2 job."""
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
    }


@v2_router.get("/jobs")
def v2_jobs(limit: int = 50):
    """List V2 jobs (most recent first)."""
    return {"jobs": _load_v2_jobs(limit=limit)}


# ============================================================
# V2 FRONTEND (inline HTML)
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
    .tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600; }
    .tag-pdf { background:#fff3e0; color:#e65100; }
    .tag-xlsx { background:#e3f2fd; color:#1565c0; }
    .tag-error { background:#ffebee; color:#c62828; }
    .result-box { margin-top:16px; padding:14px; background:#f8f9fa; border-radius:8px; border:1px solid #e6e6e6; display:none; }
    table { width:100%; border-collapse:collapse; margin-top:12px; }
    th, td { text-align:left; border-bottom:1px solid #eee; padding:8px 6px; }
    th { font-size:13px; color:#444; }
    .right { text-align:right; }
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
    <button id="btnUpload" disabled>Last opp</button>
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
const selectedFiles = document.getElementById('selectedFiles');

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
  selectedFiles.innerHTML = html;
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
    document.getElementById('resultJobId').textContent = data.job_id;
    document.getElementById('resultStatus').textContent = 'Lastet opp';
    showFileResults(data.files || []);
    document.getElementById('resultBox').style.display = 'block';

    fileInput.value = '';
    selectedFiles.innerHTML = '';
    loadJobs();
  } catch (e) {
    document.getElementById('uploadStatus').textContent = 'Nettverksfeil: ' + e.message;
  }
  btnUpload.disabled = false;
});

function showFileResults(files) {
  let html = '';
  for (const f of files) {
    const ok = f.status === 'uploaded';
    const cls = ok ? 'ok' : 'error';
    const info = ok
      ? '<span class="tag tag-' + (f.type || 'xlsx') + '">' + esc((f.type || '').toUpperCase()) + '</span> ' + ((f.size_bytes / 1024).toFixed(1)) + ' KB'
      : '<span class="tag tag-error">' + esc(f.error || 'Feil') + '</span>';
    html += '<div class="file-item ' + cls + '"><span>' + esc(f.filename) + '</span><span>' + info + '</span></div>';
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
    let html = '<table><thead><tr><th>Tidspunkt</th><th>Status</th><th class="right">Filer</th><th>Jobb-ID</th></tr></thead><tbody>';
    for (const j of jobs) {
      html += '<tr><td>' + esc(formatOsloTime(j.created_at)) + '</td><td>' + esc(j.status) +
        '</td><td class="right">' + esc(j.uploaded_files || 0) + ' ok';
      if (j.error_files) html += ', ' + esc(j.error_files) + ' feil';
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
