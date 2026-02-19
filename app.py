import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Tuple, List

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse

from matcher import Catalog, CancelledError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reduser logg-spam fra /progress polling
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# ============================================================
# APP MODE / BRANDING
# ============================================================
APP_MODE = os.getenv("APP_MODE", "standard").strip().lower()
APP_TITLE = os.getenv("APP_TITLE", "Produktmatching").strip() or "Produktmatching"
APP_SUBTITLE = os.getenv("APP_SUBTITLE", "").strip()

# ============================================================
# PATHS / PERSISTENT STORAGE
# ============================================================
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOGO_PATH = Path(os.getenv("LOGO_PATH", str((Path(__file__).parent / "logo.png").resolve())))

RESULTS_DIR = DATA_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

CATALOG_PATH = DATA_DIR / "catalog.xlsx"
JOBS_INDEX = DATA_DIR / "jobs.jsonl"
COMPETITOR_REGISTER_PATH = DATA_DIR / "competitor_price_register.xlsx"

# ============================================================
# APP STATE
# ============================================================
app = FastAPI(title=APP_TITLE, version=os.getenv("APP_VERSION", "2.6"))
CATALOG_BUNDLE = None  # Legekontor: holder to kataloger + prisoppslag

TASKS: Dict[str, Dict[str, Any]] = {}

EMBEDDINGS_STATUS: Dict[str, Any] = {
    "state": "idle",          # idle | building | ready | failed
    "started_at": None,
    "finished_at": None,
    "error": None,
}

# ============================================================
# LEGEKONTOR: TO PRISLISTER + PRISOPPSLAG
# ============================================================
LK_SHEET_NAME = os.getenv("LK_SHEET_NAME", "Legekontor pri")
FULL_SHEET_NAME = os.getenv("FULL_SHEET_NAME", "Ikke på prisliste")
CURRENT_LK_SHEET = None
CURRENT_FULL_SHEET = None


def _norm_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9æøå]+", "", s)
    return s


def _find_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    cols = list(df.columns)
    norm_map = {_norm_key(str(c)): str(c) for c in cols}
    for cand in candidates:
        k = _norm_key(cand)
        if k in norm_map:
            return norm_map[k]
    # loose: contains
    for cand in candidates:
        k = _norm_key(cand)
        for nk, orig in norm_map.items():
            if k and k in nk:
                return orig
    return None


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if not s or s.lower() == "nan":
            return None
        s = s.replace(" ", "").replace(",", ".")
        return float(s)
    except Exception:
        return None


class LegekontorCatalogBundle:
    def __init__(self, lk_catalog: Catalog, full_catalog: Catalog, price_lookup: Dict[str, Dict[str, Any]]):
        self.lk = lk_catalog
        self.full = full_catalog
        self.price_lookup = price_lookup  # artnr -> {price: float, source: 'lk'|'full'}

    def items_count(self) -> int:
        return len(self.full.items) if self.full else 0

    def embeddings_enabled(self) -> bool:
        return bool(getattr(self.full, "embed_index", None) or getattr(self.lk, "embed_index", None))

    def embeddings_available(self) -> bool:
        ok_full = bool(getattr(self.full, "embed_index", None)) and bool(getattr(self.full.embed_index, "available", False))
        ok_lk = bool(getattr(self.lk, "embed_index", None)) and bool(getattr(self.lk.embed_index, "available", False))
        return ok_full or ok_lk

    def match_competitor_row(self, comp_row: Dict[str, Any], top_n: int = 30, prefer_own_brands: bool = True):
        # Bygg matcher-row fra konkurrent-felter (matcher er laget for Beskrivelse/Spesifikasjon)
        mrow = {
            "Produktnavn": "",
            "Beskrivelse": (comp_row.get("Konkurrent Item Description") or comp_row.get("Beskrivelse") or ""),
            "Spesifikasjon": (comp_row.get("Konkurrent Specification") or comp_row.get("Spesifikasjon") or ""),
            "Konkurrent art.nr": (comp_row.get("Konkurrent Art.Nr") or comp_row.get("Konkurrent art.nr") or ""),
            "Produsent art.nr": "",
            "Kommentar": "",
        }

        # 1) prøv først LK-prislista
        artnr, _alts, best_row, quality = self.lk.match_row(mrow, top_n=top_n, prefer_own_brands=prefer_own_brands)
        if artnr:
            return artnr, best_row, quality, "lk"

        # 2) fallback: full katalog
        artnr, _alts, best_row, quality = self.full.match_row(mrow, top_n=top_n, prefer_own_brands=prefer_own_brands)
        return artnr, best_row, quality, "full"

    def price_for_artnr(self, artnr: str) -> Tuple[Optional[float], str]:
        d = self.price_lookup.get(str(artnr).strip())
        if not d:
            return None, ""
        return d.get("price"), d.get("source") or ""


# ============================================================
# WATCHDOG (stuck detector)
# ============================================================
WATCHDOG_STUCK_SECONDS = int(os.getenv("WATCHDOG_STUCK_SECONDS", "600"))  # 10 min
WATCHDOG_POLL_SECONDS = int(os.getenv("WATCHDOG_POLL_SECONDS", "30"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watchdog_loop():
    """Markerer tasks som 'error' hvis progress ikke har endret seg på en stund."""
    import time
    while True:
        try:
            now = datetime.now(timezone.utc).timestamp()
            for tid, t in list(TASKS.items()):
                if t.get("status") not in ("running", "cancel_requested"):
                    continue
                last = t.get("last_progress_ts") or t.get("started_ts") or now
                if (now - float(last)) > WATCHDOG_STUCK_SECONDS:
                    logger.warning(
                        f"Watchdog: task {tid} uten fremdrift >{WATCHDOG_STUCK_SECONDS}s. Markerer som error."
                    )
                    t["status"] = "error"
                    t["error"] = f"stuck: ingen fremdrift på {WATCHDOG_STUCK_SECONDS} sek"
                    t["finished_at"] = utc_now_iso()
                    ev = t.get("_cancel_event")
                    if ev is not None:
                        try:
                            ev.set()
                        except Exception:
                            pass
                    append_job({
                        "task_id": tid,
                        "status": "error",
                        "error": t["error"],
                        "finished_at": t["finished_at"],
                    })
        except Exception as e:
            logger.warning(f"Watchdog-feil: {e}")
        time.sleep(WATCHDOG_POLL_SECONDS)


# ============================================================
# UTIL
# ============================================================
def append_job(event: Dict[str, Any]) -> None:
    try:
        with open(JOBS_INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Kunne ikke skrive jobs-index: {e}")


def load_jobs(limit: int = 200) -> List[Dict[str, Any]]:
    if not JOBS_INDEX.exists():
        return []

    jobs: Dict[str, Dict[str, Any]] = {}
    try:
        with open(JOBS_INDEX, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    jid = e.get("task_id")
                    if not jid:
                        continue
                    jobs.setdefault(jid, {}).update(e)
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"Kunne ikke lese jobs-index: {e}")
        return []

    out = list(jobs.values())
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out[:limit]


def catalog_meta() -> Dict[str, Any]:
    if not CATALOG_PATH.exists():
        return {"loaded": False, "items": 0, "updated_at": None, "path": str(CATALOG_PATH)}

    try:
        st = CATALOG_PATH.stat()
        items = 0
        loaded = False
        if CATALOG_BUNDLE is not None:
            loaded = True
            items = int(CATALOG_BUNDLE.items_count())
        return {
            "loaded": loaded,
            "items": items,
            "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "path": str(CATALOG_PATH),
            "size_bytes": st.st_size,
        }
    except Exception:
        return {
            "loaded": CATALOG_BUNDLE is not None,
            "items": int(CATALOG_BUNDLE.items_count()) if CATALOG_BUNDLE else 0,
            "updated_at": None,
            "path": str(CATALOG_PATH),
        }


def embeddings_meta() -> Dict[str, Any]:
    meta = dict(EMBEDDINGS_STATUS)
    meta["enabled_in_catalog"] = bool(CATALOG_BUNDLE and CATALOG_BUNDLE.embeddings_enabled())
    meta["available"] = bool(CATALOG_BUNDLE and CATALOG_BUNDLE.embeddings_available())
    return meta


# ============================================================
# INPUT TEMPLATE
# ============================================================
def generate_input_template_bytes() -> bytes:
    cols = [
        "Konkurrent Navn",
        "Konkurrent Art.Nr",
        "Konkurrent Item Description",
        "Konkurrent Specification",
        "Konkurrent Pris",
        "Konkurrent salgsenhet",
        "Antall",
    ]
    example_rows = [
        {
            "Konkurrent Navn": "Konkurrent X",
            "Konkurrent Art.Nr": "12345",
            "Konkurrent Item Description": "Inkontinensinnlegg for menn / herrebind",
            "Konkurrent Specification": "Oppsugning: 195 ml, lengde 29 cm",
            "Konkurrent Pris": 12.5,
            "Konkurrent salgsenhet": "stk",
            "Antall": 10,
        }
    ]
    df = pd.DataFrame(example_rows, columns=cols)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Input")
    return buf.getvalue()


# ============================================================
# CATALOG AUTOLOAD (NON-BLOCKING)
# ============================================================
def _start_embeddings_build_background() -> None:
    """Build embeddings asynchronously so Render can detect open port quickly."""
    global CATALOG_BUNDLE

    if CATALOG_BUNDLE is None:
        return

    # Already ready?
    if CATALOG_BUNDLE.embeddings_available():
        EMBEDDINGS_STATUS.update({
            "state": "ready",
            "started_at": EMBEDDINGS_STATUS.get("started_at") or utc_now_iso(),
            "finished_at": EMBEDDINGS_STATUS.get("finished_at") or utc_now_iso(),
            "error": None,
        })
        return

    if EMBEDDINGS_STATUS.get("state") == "building":
        return

    EMBEDDINGS_STATUS.update({
        "state": "building",
        "started_at": utc_now_iso(),
        "finished_at": None,
        "error": None,
    })

    def _worker():
        global CATALOG_BUNDLE
        try:
            logger.info("Starter embedding-bygging i bakgrunnen (LK + Full)...")

            lk_sheet = CURRENT_LK_SHEET if CURRENT_LK_SHEET is not None else LK_SHEET_NAME
            full_sheet = CURRENT_FULL_SHEET if CURRENT_FULL_SHEET is not None else FULL_SHEET_NAME

            lk2 = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=True, sheet_name=lk_sheet)
            full2 = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=True, sheet_name=full_sheet)

            CATALOG_BUNDLE.lk.embed_index = getattr(lk2, "embed_index", None)
            CATALOG_BUNDLE.full.embed_index = getattr(full2, "embed_index", None)

            ok = CATALOG_BUNDLE.embeddings_available()
            EMBEDDINGS_STATUS.update({
                "state": "ready" if ok else "failed",
                "finished_at": utc_now_iso(),
                "error": None if ok else "Embedding-indeks ikke tilgjengelig",
            })
            logger.info("Embedding-indeks klar (bakgrunn)." if ok else "Embedding-indeks feilet (bakgrunn).")

        except Exception as e:
            EMBEDDINGS_STATUS.update({
                "state": "failed",
                "finished_at": utc_now_iso(),
                "error": str(e),
            })
            logger.warning(f"Embedding-bygging feilet i bakgrunnen: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def load_catalog_from_disk() -> None:
    global CATALOG_BUNDLE
    if not CATALOG_PATH.exists():
        logger.info("Ingen katalog på disk enda.")
        CATALOG_BUNDLE = None
        EMBEDDINGS_STATUS.update({"state": "idle", "started_at": None, "finished_at": None, "error": None})
        return

    logger.info(f"Laster legekontor-katalog fra disk: {CATALOG_PATH}")

    xls = pd.ExcelFile(CATALOG_PATH)
    sheets = [s for s in xls.sheet_names]

    lk_sheet = LK_SHEET_NAME if LK_SHEET_NAME in sheets else (sheets[0] if sheets else 0)
    full_sheet = FULL_SHEET_NAME if FULL_SHEET_NAME in sheets else (sheets[1] if len(sheets) > 1 else lk_sheet)

    global CURRENT_LK_SHEET, CURRENT_FULL_SHEET
    CURRENT_LK_SHEET = lk_sheet
    CURRENT_FULL_SHEET = full_sheet

    df_lk = pd.read_excel(CATALOG_PATH, sheet_name=lk_sheet)
    df_full = pd.read_excel(CATALOG_PATH, sheet_name=full_sheet)

    lk_price_col = _find_col(df_lk, "Pris Etter Rabatt", "Pris etter rabatt", "Pris etter rabatt (NOK)", "Price after discount")
    full_price_col = _find_col(df_full, "Net Price", "Net price", "NetPrice", "Net_Price", "Net Purch Price")

    art_col_lk = _find_col(df_lk, "Artikkelnummer", "Art.nr", "Art nr", "Article Number", "Item NO", "Item No")
    art_col_full = _find_col(df_full, "Artikkelnummer", "Art.nr", "Art nr", "Article Number", "Item NO", "Item No")

    if not art_col_lk or not art_col_full:
        raise ValueError("Finner ikke artikkelnummer-kolonne i ett av arkene (trenger f.eks. Artikkelnummer / Art.nr / Item No).")

    if not lk_price_col:
        logger.warning("Fant ikke 'Pris Etter Rabatt' i LK-arket. Priser fra LK kan bli tomme.")
    if not full_price_col:
        logger.warning("Fant ikke 'Net Price' i full-katalog-arket. Fallback-priser kan bli tomme.")

    price_lookup: Dict[str, Dict[str, Any]] = {}

    # Full først
    for _, r in df_full.iterrows():
        art = str(r.get(art_col_full, "")).strip()
        if not art or art.lower() == "nan":
            continue
        p = _to_float(r.get(full_price_col)) if full_price_col else None
        if p is None:
            continue
        price_lookup[art] = {"price": float(p), "source": "full"}

    # LK overstyrer
    for _, r in df_lk.iterrows():
        art = str(r.get(art_col_lk, "")).strip()
        if not art or art.lower() == "nan":
            continue
        p = _to_float(r.get(lk_price_col)) if lk_price_col else None
        if p is None:
            continue
        price_lookup[art] = {"price": float(p), "source": "lk"}

    # Ikke bygg embeddings ved startup
    lk_cat = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=False, sheet_name=lk_sheet)
    full_cat = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=False, sheet_name=full_sheet)

    CATALOG_BUNDLE = LegekontorCatalogBundle(lk_catalog=lk_cat, full_catalog=full_cat, price_lookup=price_lookup)

    logger.info(f"Legekontor-katalog lastet: LK={len(lk_cat.items)} produkter, Full={len(full_cat.items)} produkter")
    logger.info(f"Prisoppslag: {len(price_lookup)} art.nr med pris")

    _start_embeddings_build_background()


@app.on_event("startup")
def startup_event():
    load_catalog_from_disk()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    logger.info("Startup complete (catalog loaded without embeddings). Ready to accept requests.")


# ============================================================
# COMPETITOR REGISTER
# ============================================================
def append_competitor_register(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        new_df = pd.DataFrame(rows)
        if COMPETITOR_REGISTER_PATH.exists():
            try:
                old_df = pd.read_excel(COMPETITOR_REGISTER_PATH)
                out_df = pd.concat([old_df, new_df], ignore_index=True)
            except Exception:
                out_df = new_df
        else:
            out_df = new_df

        with pd.ExcelWriter(COMPETITOR_REGISTER_PATH, engine="openpyxl") as writer:
            out_df.to_excel(writer, index=False, sheet_name="Register")
    except Exception as e:
        logger.warning(f"Kunne ikke oppdatere konkurrentregister: {e}")


# ============================================================
# MATCH CORE
# ============================================================
OUTPUT_COLUMNS_LK = [
    "Konkurrent Navn",
    "Konkurrent Art.Nr",
    "Konkurrent Item Description",
    "Konkurrent Specification",
    "Konkurrent Pris",
    "Konkurrent salgsenhet",
    "Item NO",
    "Item Description",
    "Speciciation",
    "Antall",
    "Pris per enhet",
    "Total pris",
    "Pris Konkurrent vs oss",
]


def match_excel(
    bundle: LegekontorCatalogBundle,
    content: bytes,
    input_filename: str,
    progress_cb: Optional[Callable[[float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    prefer_own_brands: bool = True,
) -> Tuple[bytes, Dict[str, Any]]:
    df_in = pd.read_excel(BytesIO(content))
    total = len(df_in)

    # ensure input cols exist
    for c in [
        "Konkurrent Navn",
        "Konkurrent Art.Nr",
        "Konkurrent Item Description",
        "Konkurrent Specification",
        "Konkurrent Pris",
        "Konkurrent salgsenhet",
        "Antall",
    ]:
        if c not in df_in.columns:
            df_in[c] = ""

    rows_out: List[Dict[str, Any]] = []
    reg_rows: List[Dict[str, Any]] = []

    sum_comp_total = 0.0
    sum_our_total = 0.0

    run_date = datetime.now(timezone.utc).date().isoformat()

    for idx, r in df_in.iterrows():
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Avbrutt av bruker")

        comp = r.to_dict()
        comp_name = str(comp.get("Konkurrent Navn", "") or "").strip()
        comp_art = str(comp.get("Konkurrent Art.Nr", "") or "").strip()
        comp_desc = str(comp.get("Konkurrent Item Description", "") or "").strip()
        comp_spec = str(comp.get("Konkurrent Specification", "") or "").strip()
        comp_unit = _to_float(comp.get("Konkurrent Pris")) or 0.0
        comp_unit_name = str(comp.get("Konkurrent salgsenhet", "") or "").strip()
        qty = _to_float(comp.get("Antall")) or 0.0

        artnr, best_row, quality, matched_from = bundle.match_competitor_row(
            comp,
            top_n=30,
            prefer_own_brands=prefer_own_brands,
        )

        our_unit, price_source = (None, "")
        if artnr:
            our_unit, price_source = bundle.price_for_artnr(artnr)

        our_unit_val = float(our_unit) if our_unit is not None else 0.0

        comp_total = float(comp_unit) * float(qty)
        our_total = float(our_unit_val) * float(qty)
        diff_total = comp_total - our_total

        sum_comp_total += comp_total
        sum_our_total += our_total

        # try to read best_row fields robustly
        item_desc = ""
        item_spec = ""
        if isinstance(best_row, dict):
            item_desc = str(best_row.get("Item Description") or best_row.get("Beskrivelse") or best_row.get("Description") or "")
            item_spec = str(best_row.get("Specification") or best_row.get("Spesifikasjon") or "")

        out = {
            "Konkurrent Navn": comp_name,
            "Konkurrent Art.Nr": comp_art,
            "Konkurrent Item Description": comp_desc,
            "Konkurrent Specification": comp_spec,
            "Konkurrent Pris": comp_unit,
            "Konkurrent salgsenhet": comp_unit_name,
            "Item NO": str(artnr or ""),
            "Item Description": item_desc,
            "Speciciation": item_spec,
            "Antall": qty,
            "Pris per enhet": our_unit if our_unit is not None else "",
            "Total pris": our_total,
            "Pris Konkurrent vs oss": diff_total,
        }
        rows_out.append(out)

        reg_rows.append({
            "Dato": run_date,
            "Inputfil": input_filename,
            "Konkurrent Navn": comp_name,
            "Konkurrent Art.Nr": comp_art,
            "Konkurrent Item Description": comp_desc,
            "Konkurrent Specification": comp_spec,
            "Konkurrent Pris": comp_unit,
            "Konkurrent salgsenhet": comp_unit_name,
            "Antall": qty,
            "Matchet Item NO": str(artnr or ""),
            "Matchet Item Description": item_desc,
            "Matchet Specification": item_spec,
            "Vår pris per enhet": our_unit if our_unit is not None else "",
            "Vår pris kilde": price_source or matched_from,
            "Match kvalitet": quality,
        })

        if progress_cb and total > 0:
            progress_cb((idx + 1) / total)

    # Summary rows
    rows_out.append({c: "" for c in OUTPUT_COLUMNS_LK})
    rows_out.append({
        "Konkurrent Item Description": "Total pris for konkurrent",
        "Konkurrent Pris": sum_comp_total,
        "Total pris": "",
        "Pris Konkurrent vs oss": "",
    })
    rows_out.append({
        "Konkurrent Item Description": "Total pris for oss",
        "Konkurrent Pris": "",
        "Total pris": sum_our_total,
        "Pris Konkurrent vs oss": "",
    })
    rows_out.append({
        "Konkurrent Item Description": "Differanse (konkurrent - oss)",
        "Konkurrent Pris": "",
        "Total pris": "",
        "Pris Konkurrent vs oss": (sum_comp_total - sum_our_total),
    })

    df_out = pd.DataFrame(rows_out)
    for c in OUTPUT_COLUMNS_LK:
        if c not in df_out.columns:
            df_out[c] = ""
    df_out = df_out.reindex(columns=OUTPUT_COLUMNS_LK)

    # Update competitor register (persistent)
    append_competitor_register(reg_rows)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_out.to_excel(writer, index=False, sheet_name="Output")

    meta = {
        "rows_in": int(total),
        "rows_out": int(total),
        "timestamp": utc_now_iso(),
        "prefer_own_brands": bool(prefer_own_brands),
        "register_path": str(COMPETITOR_REGISTER_PATH),
    }
    return buf.getvalue(), meta


# ============================================================
# UI
# ============================================================
INDEX_HTML = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{APP_TITLE}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 32px; color:#1a1a1a; }}
    .card {{ border:1px solid #e6e6e6; padding:16px; border-radius:12px; max-width: 1100px; }}
    input[type=file] {{ margin: 8px 0; }}
    button {{ background:#1F4E79; color:white; border:0; padding:10px 14px; border-radius:8px; cursor:pointer; }}
    button.secondary {{ background:#6b7280; }}
    button:disabled {{ opacity: 0.5; cursor:not-allowed; }}
    .row {{ display:flex; gap:24px; flex-wrap: wrap; }}
    .col {{ flex: 1; min-width: 320px; }}
    .muted {{ color:#666; }}
    .bar-wrap {{ width:100%; background:#f2f2f2; border-radius: 999px; overflow:hidden; height: 14px; }}
    .bar {{ height: 14px; width: 0%; background: #1F4E79; transition: width .3s; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align:left; border-bottom:1px solid #eee; padding:10px 8px; vertical-align: top; }}
    th {{ font-size: 13px; color:#444; }}
    .right {{ text-align:right; }}
    a {{ color:#1F4E79; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    pre {{ background:#fafafa; border:1px solid #eee; padding:12px; border-radius:8px; overflow:auto; }}
  </style>
</head>
<body>
  <div style="margin-bottom:18px;"><img src="/static/logo.png" alt="Logo" style="max-height:110px; width:auto;" onerror="this.style.display='none';"/></div>
  <div style="margin: -6px 0 14px 0; color:#555;" id="subtitle">{APP_SUBTITLE}</div>
  <div class="card">
    <div class="row">
      <div class="col">
        <h3>1) Katalog (Legekontor)</h3>
        <div id="catalogInfo" class="muted"></div>
        <input id="catalog" type="file" accept=".xlsx"/>
        <button id="btnCatalog">Last opp / oppdater katalog</button>
        <div id="catStatus" class="muted"></div>
      </div>

      <div class="col">
        <h3>2) Prissammenligning</h3>
        <p class="muted">Last opp konkurrentfil i input-format og start matching.</p>
        <p class="muted"><a href="/template">Last ned input-template.xlsx</a></p>
        <div class="muted" style="margin:10px 0 6px 0;">
          <b>Preferer egne merkevarer?</b>
          <label style="margin-left:10px;"><input type="radio" name="prefer" value="1" checked> Ja</label>
          <label style="margin-left:10px;"><input type="radio" name="prefer" value="0"> Nei</label>
        </div>
        <input id="input" type="file" accept=".xlsx"/>
        <button id="btnMatch" disabled>Start matching</button>
        <button id="btnCancel" disabled class="secondary">Avbryt</button>
        <div id="matchStatus" class="muted"></div>
        <div style="margin-top:12px;">
          <div class="bar-wrap"><div id="bar" class="bar"></div></div>
        </div>
        <div id="download" style="margin-top:10px;"></div>
      </div>
    </div>

    <hr style="border:none;border-top:1px solid #eee;margin:18px 0;"/>

    <h3>Historikk</h3>
    <p class="muted">Tidspunkt vises i Oslo-tid.</p>
    <div id="history"></div>

    <hr style="border:none;border-top:1px solid #eee;margin:18px 0;"/>
    <h3>Debug</h3>
    <pre id="debug"></pre>
  </div>

<script>
let taskId = null;

function esc(s) {{
  return String(s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}}

function formatOsloTime(iso) {{
  if (!iso) return "";
  try {{
    const d = new Date(iso);
    return new Intl.DateTimeFormat("nb-NO", {{
      timeZone: "Europe/Oslo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }}).format(d);
  }} catch {{
    return iso;
  }}
}}

async function refreshCatalogInfo() {{
  const resp = await fetch("/catalog_status");
  const data = await resp.json();
  document.getElementById("debug").textContent = JSON.stringify(data, null, 2);

  const el = document.getElementById("catalogInfo");
  if (data.path) {{
    el.innerHTML = `
      <div><b>Path:</b> ${esc(data.path)}</div>
      <div><b>Finnes på disk:</b> ${data.exists ? "Ja" : "Nei"}</div>
      <div><b>Lastet i minne:</b> ${data.loaded ? "Ja" : "Nei"}</div>
      <div><b>Produkter:</b> ${esc(data.items)}</div>
      <div><b>Sist oppdatert (Oslo):</b> ${esc(formatOsloTime(data.updated_at))}</div>
      <div><b>Embeddings:</b> ${esc((data.embeddings && data.embeddings.state) || "")}</div>
    `;
  }}

  document.getElementById("btnMatch").disabled = !data.loaded;
}}

async function uploadCatalog() {{
  const file = document.getElementById("catalog").files[0];
  if (!file) return alert("Velg en katalogfil (.xlsx)");
  document.getElementById("catStatus").textContent = "Laster opp katalog...";

  const fd = new FormData();
  fd.append("file", file);

  const resp = await fetch("/upload_catalog", {{ method: "POST", body: fd }});
  const data = await resp.json().catch(() => ({{}}));

  if (!resp.ok) {{
    document.getElementById("catStatus").textContent = "Feil: " + (data.error || "ukjent");
    await refreshCatalogInfo();
    return;
  }}

  document.getElementById("catStatus").textContent = `Katalog oppdatert. Produkter: ${data.items}`;
  await refreshCatalogInfo();
}}

async function startMatch() {{
  const file = document.getElementById("input").files[0];
  if (!file) return alert("Velg en inputfil (.xlsx)");

  document.getElementById("matchStatus").textContent = "Starter matching...";
  document.getElementById("btnCancel").disabled = false;
  document.getElementById("download").innerHTML = "";
  document.getElementById("bar").style.width = "1%";

  const fd = new FormData();
  fd.append("file", file);
  const pref = document.querySelector("input[name=prefer]:checked");
  fd.append("prefer_own_brands", pref ? pref.value : "1");

  const resp = await fetch("/match", {{ method: "POST", body: fd }});
  const data = await resp.json().catch(() => ({{}}));

  if (!resp.ok) {{
    document.getElementById("matchStatus").textContent = "Feil: " + (data.error || "ukjent");
    document.getElementById("btnCancel").disabled = true;
    return;
  }}

  taskId = data.task_id;
  pollProgress();
}}

async function pollProgress() {{
  const resp = await fetch(`/progress/${taskId}`);
  const data = await resp.json().catch(() => ({{}}));

  const p = Math.round((data.progress || 0) * 100);
  document.getElementById("bar").style.width = `${p}%`;

  if (data.status === "cancel_requested") {{
    document.getElementById("matchStatus").textContent = "Avbryt forespurt…";
  }} else {{
    document.getElementById("matchStatus").textContent = data.status || "running";
  }}

  if (data.status === "done") {{
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("bar").style.width = "100%";
    document.getElementById("download").innerHTML = `<a href="/download/${esc(taskId)}">Last ned resultat</a>`;
    await loadHistory();
    return;
  }}

  if (data.status === "cancelled") {{
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("matchStatus").textContent = "Avbrutt";
    await loadHistory();
    return;
  }}

  if (data.status === "error") {{
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("matchStatus").textContent = "Feil: " + (data.error || "");
    await loadHistory();
    return;
  }}

  setTimeout(pollProgress, 2500);
}}

async function loadHistory() {{
  const resp = await fetch("/history?limit=200");
  const data = await resp.json();

  let html = "<table>";
  html += "<thead><tr>";
  html += "<th>Tidspunkt (Oslo)</th><th>Filnavn</th><th>Status</th><th class='right'>Rader</th><th>Last ned</th><th>Avbryt</th>";
  html += "</tr></thead><tbody>";

  for (const j of (data.jobs || [])) {{
    const created = esc(formatOsloTime(j.created_at || ""));
    const fn = esc(j.input_filename || "");
    const st = esc(j.status || "");
    const rows = esc(j.rows_out ?? "");
    const dl = j.status === "done" ? `<a href="/download/${esc(j.task_id)}">Last ned</a>` : "";
    const cancel = (j.status === "running" || j.status === "cancel_requested") ? `<button class="secondary" onclick="cancelJob('${esc(j.task_id)}')">Avbryt</button>` : "";
    html += `<tr><td>${created}</td><td>${fn}</td><td>${st}</td><td class='right'>${rows}</td><td>${dl}</td><td>${cancel}</td></tr>`;
  }}

  html += "</tbody></table>";
  document.getElementById("history").innerHTML = html;
}}

async function cancelJob(tid) {{
  if (!tid) return;
  try {{
    await fetch(`/cancel/${tid}`, {{ method: "POST" }});
  }} catch (e) {{}}
  if (tid === taskId) {{
    document.getElementById("matchStatus").textContent = "Avbryt forespurt…";
  }}
  setTimeout(loadHistory, 600);
}}

document.getElementById("btnCatalog").addEventListener("click", uploadCatalog);
document.getElementById("btnMatch").addEventListener("click", startMatch);
document.getElementById("btnCancel").addEventListener("click", async () => {{
  if (!taskId) return;
  await fetch(`/cancel/${taskId}`, {{ method: "POST" }});
  document.getElementById("matchStatus").textContent = "Avbryt forespurt…";
}});

refreshCatalogInfo();
loadHistory();
</script>
</body>
</html>
"""


@app.get("/static/logo.png")
def static_logo():
    try:
        if not LOGO_PATH.exists():
            return JSONResponse({"error": "Logo ikke funnet"}, status_code=404)
        data = LOGO_PATH.read_bytes()
        return StreamingResponse(BytesIO(data), media_type="image/png")
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lese logo: {e}"}, status_code=500)


# ============================================================
# ROUTES
# ============================================================
@app.get("/template")
def template():
    xlsx = generate_input_template_bytes()
    return StreamingResponse(
        BytesIO(xlsx),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="input_template.xlsx"'},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/admin")
def admin_redirect():
    return RedirectResponse(url="/", status_code=302)


@app.get("/catalog_status")
def catalog_status():
    meta = catalog_meta()
    meta["exists"] = CATALOG_PATH.exists()
    meta["loaded"] = CATALOG_BUNDLE is not None
    meta["embeddings"] = embeddings_meta()
    return meta


@app.post("/upload_catalog")
async def upload_catalog(file: UploadFile = File(...)):
    global CATALOG_BUNDLE
    content = await file.read()

    try:
        CATALOG_PATH.write_bytes(content)
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lagre katalog: {e}"}, status_code=500)

    try:
        load_catalog_from_disk()
    except Exception as e:
        CATALOG_BUNDLE = None
        return JSONResponse({"error": f"Katalog lagret, men feilet å laste: {e}"}, status_code=500)

    return {"ok": True, "items": CATALOG_BUNDLE.items_count() if CATALOG_BUNDLE else 0}


@app.post("/match")
async def match(file: UploadFile = File(...), prefer_own_brands: str = Form("1")):
    if CATALOG_BUNDLE is None:
        return JSONResponse({"error": "Last opp katalog først"}, status_code=400)

    content = await file.read()
    task_id = uuid.uuid4().hex
    output_path = RESULTS_DIR / f"{task_id}.xlsx"

    cancel_event = threading.Event()
    now_iso = utc_now_iso()
    now_ts = datetime.now(timezone.utc).timestamp()

    TASKS[task_id] = {
        "progress": 0.0,
        "status": "running",
        "error": None,
        "cancel_requested": False,
        "_cancel_event": cancel_event,
        "created_at": now_iso,
        "started_at": now_iso,
        "finished_at": None,
        "input_filename": file.filename or "input.xlsx",
        "rows_out": None,
        "last_progress_ts": now_ts,
        "started_ts": now_ts,
    }

    append_job({
        "task_id": task_id,
        "created_at": now_iso,
        "status": "running",
        "input_filename": file.filename or "input.xlsx",
    })

    def progress(p: float):
        t = TASKS.get(task_id)
        if not t:
            return
        t["progress"] = float(max(0.0, min(1.0, p)))
        t["last_progress_ts"] = datetime.now(timezone.utc).timestamp()

    prefer = (prefer_own_brands or "1").strip() == "1"

    def _worker():
        try:
            out_xlsx, meta = match_excel(
                bundle=CATALOG_BUNDLE,
                content=content,
                input_filename=(file.filename or "input.xlsx"),
                progress_cb=progress,
                cancel_event=cancel_event,
                prefer_own_brands=prefer,
            )

            output_path.write_bytes(out_xlsx)

            t = TASKS.get(task_id)
            if t:
                t["status"] = "done"
                t["progress"] = 1.0
                t["finished_at"] = utc_now_iso()
                t["rows_out"] = meta.get("rows_out")
                t["error"] = None

            append_job({
                "task_id": task_id,
                "status": "done",
                "finished_at": utc_now_iso(),
                "rows_out": meta.get("rows_out"),
            })

            logger.info(f"Matching ferdig. Task={task_id}, rader={meta.get('rows_out')}")

        except CancelledError as ce:
            t = TASKS.get(task_id)
            if t:
                t["status"] = "cancelled"
                t["error"] = str(ce)
                t["finished_at"] = utc_now_iso()

            append_job({
                "task_id": task_id,
                "status": "cancelled",
                "finished_at": utc_now_iso(),
            })

            logger.info(f"Matching avbrutt. Task={task_id}")

        except Exception as e:
            logger.exception("Matching feilet")
            t = TASKS.get(task_id)
            if t:
                t["status"] = "error"
                t["error"] = str(e)
                t["finished_at"] = utc_now_iso()

            append_job({
                "task_id": task_id,
                "status": "error",
                "error": str(e),
                "finished_at": utc_now_iso(),
            })

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "task_id": task_id}


@app.post("/cancel/{task_id}")
def cancel(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        return JSONResponse({"error": "Ukjent task_id"}, status_code=404)

    if t.get("status") not in ("running", "cancel_requested"):
        return {"ok": True, "status": t.get("status")}

    t["cancel_requested"] = True
    t["status"] = "cancel_requested"

    ev = t.get("_cancel_event")
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass

    append_job({
        "task_id": task_id,
        "status": "cancel_requested",
        "finished_at": utc_now_iso(),
    })

    return {"ok": True, "status": "cancel_requested"}


@app.get("/progress/{task_id}")
def progress(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        return {"status": "unknown", "progress": 0.0}
    return {
        "status": t.get("status"),
        "progress": float(t.get("progress", 0.0)),
        "error": t.get("error"),
        "cancel_requested": bool(t.get("cancel_requested", False)),
    }


@app.get("/download/{task_id}")
def download(task_id: str):
    path = RESULTS_DIR / f"{task_id}.xlsx"
    if not path.exists():
        return JSONResponse({"error": "Fil finnes ikke"}, status_code=404)

    def iterfile():
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="matching_{task_id}.xlsx"'}
    )


@app.get("/history")
def history(limit: int = 200):
    return {"jobs": load_jobs(limit=limit)}
