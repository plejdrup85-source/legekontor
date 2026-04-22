import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse

from matcher import Catalog
from sso_auth import require_sso, sso_callback, sso_logout, User
from v2.router import v2_router

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

LOGO_PATH = Path(os.getenv("LOGO_PATH", str((Path(__file__).parent / "onemed_logo.png").resolve())))

CATALOG_PATH = DATA_DIR / "catalog.xlsx"
CATALOG_MAPPING_PATH = DATA_DIR / "catalog_mapping.json"

MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(50 * 1024 * 1024)))


def _check_upload_size(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "Fil er for stor")

# ============================================================
# APP STATE
# ============================================================
app = FastAPI(title=APP_TITLE, version=os.getenv("APP_VERSION", "2.6"))

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(v2_router, prefix="/v2")

# ============================================================
# SSO ROUTES (handoff callback + logout)
# ============================================================
@app.get("/sso/callback")
def handle_sso_callback(token: str, redirect: str = "/"):
    return sso_callback(token, redirect)


@app.get("/logout")
def handle_logout():
    return sso_logout()


CATALOG_BUNDLE = None  # Legekontor: holder to kataloger + prisoppslag


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


# ============================================================
# CATALOG COLUMN MAPPING DEFINITIONS
# ============================================================
# System fields that the catalog loader expects, with metadata for the UI.
# Each entry: (internal_name, label, required, description, auto_match_patterns)
CATALOG_SYSTEM_FIELDS = [
    {
        "key": "Artikkelnummer",
        "label": "Artikkelnummer",
        "required": True,
        "description": "Unik produktidentifikator (art.nr / varenr / SKU)",
        "patterns": ["artikkelnummer", "art.nr", "art nr", "artikkel nr", "varenr",
                      "varenummer", "produktnr", "produkt nr", "item no", "itemno",
                      "article number", "sku", "item number"],
    },
    {
        "key": "Item Description",
        "label": "Produktnavn / beskrivelse",
        "required": True,
        "description": "Hovedbeskrivelse av produktet (brukes i sok og matching)",
        "patterns": ["item description", "beskrivelse", "description", "varetekst",
                      "produktnavn", "produkt navn", "product name"],
    },
    {
        "key": "Specification",
        "label": "Spesifikasjon",
        "required": False,
        "description": "Teknisk spesifikasjon (storrelse, materiale, etc.)",
        "patterns": ["specification", "spesifikasjon", "spec", "produktspesifikasjon"],
    },
    {
        "key": "Producer Name",
        "label": "Produsent",
        "required": False,
        "description": "Navn pa produsent / leverandor / merke",
        "patterns": ["producer name", "produsent", "leverandor", "brand", "merke",
                      "manufacturer", "supplier"],
    },
    {
        "key": "Producer Item Number",
        "label": "Produsent art.nr",
        "required": False,
        "description": "Produsentens eget artikkelnummer (MPN)",
        "patterns": ["producer item number", "produsentnr", "produsent nr", "mpn",
                      "manufacturer part"],
    },
    {
        "key": "GID",
        "label": "GID (Global Item ID)",
        "required": True,
        "description": "Brukes til a bygge produktlenker (onemed.no/nb-no/products/{GID}). Uten GID far produktene ingen klikkbar lenke.",
        "patterns": ["gid", "global item id", "global item identifier"],
    },
    {
        "key": "ALC",
        "label": "ALC (kostnadsniva)",
        "required": False,
        "description": "Average Landed Cost - brukes til kostnadsbasert rangering",
        "patterns": ["alc", "average landed cost", "landed cost"],
    },
    {
        "key": "Pris",
        "label": "Pris / enhetspris",
        "required": False,
        "description": "Pris etter rabatt (LK-ark) eller Net Price (full katalog). Brukes i prissammenligning.",
        "patterns": ["pris etter rabatt", "pris", "price", "net price", "netprice",
                      "net purch price", "price after discount", "enhetspris", "unit price"],
    },
    {
        "key": "Item group",
        "label": "Produktgruppe",
        "required": False,
        "description": "Kategori / produktgruppe (forbedrer sok)",
        "patterns": ["item group", "produktgruppe", "varegruppe", "category"],
    },
    {
        "key": "Web Title",
        "label": "Web-tittel",
        "required": False,
        "description": "Tittel brukt pa nett (forbedrer fritekst-sok)",
        "patterns": ["web title", "tittel", "web tittel"],
    },
    {
        "key": "Web Text",
        "label": "Web-tekst",
        "required": False,
        "description": "Utfyllende webtekst (forbedrer fritekst-sok)",
        "patterns": ["web text", "webtekst"],
    },
    {
        "key": "Storage instruction",
        "label": "Lagringsanvisning",
        "required": False,
        "description": "Oppbevaringsinstruksjoner",
        "patterns": ["storage instruction", "lagring", "oppbevaring"],
    },
]


def _auto_suggest_mapping(file_columns: List[str]) -> Dict[str, str]:
    """Suggest mapping from system field keys to file column names.

    Returns dict: {system_field_key: file_column_name}
    """
    from matcher import _norm_key
    suggestions: Dict[str, str] = {}
    used_cols: set = set()

    # Build normalized lookup for file columns
    norm_to_orig: Dict[str, str] = {}
    for c in file_columns:
        nk = _norm_key(str(c))
        if nk not in norm_to_orig:
            norm_to_orig[nk] = c

    for field_def in CATALOG_SYSTEM_FIELDS:
        key = field_def["key"]
        # Check each pattern against normalized file columns
        for pattern in field_def["patterns"]:
            norm_pat = _norm_key(pattern)
            if norm_pat in norm_to_orig:
                col = norm_to_orig[norm_pat]
                if col not in used_cols:
                    suggestions[key] = col
                    used_cols.add(col)
                    break
        if key not in suggestions:
            # Fallback: check if any file column contains the pattern tokens
            for pattern in field_def["patterns"]:
                norm_pat = _norm_key(pattern)
                for nk, orig in norm_to_orig.items():
                    if orig in used_cols:
                        continue
                    if norm_pat in nk or nk in norm_pat:
                        suggestions[key] = orig
                        used_cols.add(orig)
                        break
                if key in suggestions:
                    break

    return suggestions


def _apply_column_mapping(df: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    """Apply user column mapping: rename file columns to standardized 'Katalog: *' names.

    mapping: {system_field_key: file_column_name}
    """
    rename: Dict[str, str] = {}
    # Map system keys to the internal column names the loader expects
    key_to_internal = {
        "Artikkelnummer": "Katalog: Artikkelnummer",
        "Item Description": "Katalog: Item Description",
        "Specification": "Katalog: Specification",
        "Producer Name": "Katalog: Producer Name",
        "Producer Item Number": "Katalog: Producer Item Number",
        "GID": "Katalog: GID",
        "ALC": "Katalog: ALC",
        "Pris": None,  # Price column name varies; keep original
        "Item group": "Katalog: Item group",
        "Web Title": "Katalog: Web Title",
        "Web Text": "Katalog: Web Text",
        "Storage instruction": "Katalog: Storage instruction",
    }

    for sys_key, file_col in mapping.items():
        if not file_col or file_col == "__none__":
            continue
        internal = key_to_internal.get(sys_key)
        if internal and file_col in df.columns and internal not in df.columns:
            rename[file_col] = internal
        elif sys_key == "Pris" and file_col in df.columns:
            # Keep price column as-is; it's found by _find_col dynamically
            pass

    if rename:
        df = df.rename(columns=rename)
    return df


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


# ============================================================
# UTIL
# ============================================================


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
# PDF INPUT (FAKTURA) - enkel tekstuttrekk + linjeheuristikk
# ============================================================


def _is_pdf_bytes(b: bytes) -> bool:
    return bool(b) and b[:4] == b"%PDF"

def _is_xlsx_bytes(b: bytes) -> bool:
    # XLSX is a ZIP container -> starts with PK
    return bool(b) and b[:2] == b"PK"





# ============================================================
# INPUT TEMPLATE
# ============================================================


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


def _load_saved_mapping() -> Optional[Dict[str, str]]:
    """Load column mapping from disk if it exists."""
    if CATALOG_MAPPING_PATH.exists():
        try:
            data = json.loads(CATALOG_MAPPING_PATH.read_text("utf-8"))
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return None


def load_catalog_from_disk() -> None:
    global CATALOG_BUNDLE
    if not CATALOG_PATH.exists():
        logger.info("Ingen katalog på disk enda.")
        CATALOG_BUNDLE = None
        EMBEDDINGS_STATUS.update({"state": "idle", "started_at": None, "finished_at": None, "error": None})
        return

    logger.info(f"Laster legekontor-katalog fra disk: {CATALOG_PATH}")

    # Load saved column mapping (if any)
    saved_mapping = _load_saved_mapping()
    if saved_mapping:
        logger.info(f"Bruker lagret kolonnemapping: {list(saved_mapping.keys())}")

    xls = pd.ExcelFile(CATALOG_PATH)
    sheets = [s for s in xls.sheet_names]

    lk_sheet = LK_SHEET_NAME if LK_SHEET_NAME in sheets else (sheets[0] if sheets else 0)
    full_sheet = FULL_SHEET_NAME if FULL_SHEET_NAME in sheets else (sheets[1] if len(sheets) > 1 else lk_sheet)

    global CURRENT_LK_SHEET, CURRENT_FULL_SHEET
    CURRENT_LK_SHEET = lk_sheet
    CURRENT_FULL_SHEET = full_sheet

    df_lk = pd.read_excel(CATALOG_PATH, sheet_name=lk_sheet)
    df_full = pd.read_excel(CATALOG_PATH, sheet_name=full_sheet)

    # Apply column mapping if saved
    if saved_mapping:
        df_lk = _apply_column_mapping(df_lk, saved_mapping)
        df_full = _apply_column_mapping(df_full, saved_mapping)

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
    lk_cat = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=False, sheet_name=lk_sheet, column_mapping=saved_mapping)
    full_cat = Catalog.from_excel(str(CATALOG_PATH), use_embeddings=False, sheet_name=full_sheet, column_mapping=saved_mapping)

    CATALOG_BUNDLE = LegekontorCatalogBundle(lk_catalog=lk_cat, full_catalog=full_cat, price_lookup=price_lookup)

    logger.info(f"Legekontor-katalog lastet: LK={len(lk_cat.items)} produkter, Full={len(full_cat.items)} produkter")
    logger.info(f"Prisoppslag: {len(price_lookup)} art.nr med pris")

    _start_embeddings_build_background()


@app.on_event("startup")
def startup_event():
    load_catalog_from_disk()
    logger.info("Startup complete (catalog loaded without embeddings). Ready to accept requests.")


# ============================================================
# COMPETITOR REGISTER
# ============================================================



# ============================================================
# UI
# ============================================================
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>__APP_TITLE__</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 32px; color:#1a1a1a; }
    .card { border:1px solid #e6e6e6; padding:16px; border-radius:12px; max-width: 1100px; }
    input[type=file] { margin: 8px 0; }
    button { background:#1F4E79; color:white; border:0; padding:10px 14px; border-radius:8px; cursor:pointer; font-size:13px; }
    button.secondary { background:#6b7280; }
    button:disabled { opacity: 0.5; cursor:not-allowed; }
    .row { display:flex; gap:24px; flex-wrap: wrap; }
    .col { flex: 1; min-width: 320px; }
    .muted { color:#666; }
    .bar-wrap { width:100%; background:#f2f2f2; border-radius: 999px; overflow:hidden; height: 14px; }
    .bar { height: 14px; width: 0%; background: #1F4E79; transition: width .3s; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align:left; border-bottom:1px solid #eee; padding:10px 8px; vertical-align: top; }
    th { font-size: 13px; color:#444; }
    .right { text-align:right; }
    a { color:#1F4E79; text-decoration:none; }
    a:hover { text-decoration:underline; }
    pre { background:#fafafa; border:1px solid #eee; padding:12px; border-radius:8px; overflow:auto; }

    /* Column mapping modal */
    .modal-overlay { display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.5); z-index:1000; justify-content:center; align-items:flex-start; padding:40px 20px; overflow-y:auto; }
    .modal-overlay.active { display:flex; }
    .modal { background:white; border-radius:12px; max-width:820px; width:100%; padding:24px; box-shadow:0 8px 32px rgba(0,0,0,0.2); }
    .modal h3 { margin:0 0 6px 0; }
    .modal .help-text { font-size:13px; color:#555; margin-bottom:16px; line-height:1.5; }
    .mapping-table { width:100%; border-collapse:collapse; margin:12px 0; }
    .mapping-table th { background:#f8f9fa; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; color:#555; padding:8px 10px; text-align:left; }
    .mapping-table td { padding:8px 10px; border-bottom:1px solid #f0f0f0; vertical-align:middle; }
    .mapping-table .field-name { font-weight:600; font-size:13px; }
    .mapping-table .field-desc { font-size:11px; color:#888; margin-top:2px; }
    .mapping-table .required-badge { display:inline-block; font-size:10px; background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:8px; margin-left:6px; font-weight:600; }
    .mapping-table .optional-badge { display:inline-block; font-size:10px; background:#f3f4f6; color:#6b7280; padding:1px 6px; border-radius:8px; margin-left:6px; }
    .mapping-table select { width:100%; padding:6px 8px; border:1px solid #d1d5db; border-radius:5px; font-size:13px; font-family:inherit; }
    .mapping-table select.mapped { border-color:#16a34a; background:#f0fdf4; }
    .mapping-table select.unmapped-required { border-color:#dc2626; background:#fef2f2; }
    .sheet-info { background:#f0f7ff; border:1px solid #bfdbfe; border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
    .sheet-info b { color:#1e40af; }
    .modal-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
    .sample-preview { background:#fafafa; border:1px solid #e5e7eb; border-radius:6px; padding:8px 10px; margin-top:8px; font-size:11px; overflow-x:auto; max-height:100px; }
    .sample-preview table { font-size:11px; }
    .sample-preview th { font-size:10px; padding:3px 6px; background:#eee; }
    .sample-preview td { padding:3px 6px; font-size:11px; white-space:nowrap; max-width:150px; overflow:hidden; text-overflow:ellipsis; }
  </style>
</head>
<body>
  <div style="margin-bottom:18px;"><img src="/static/logo.png" alt="Logo" onerror="this.style.display='none';"/></div>
  <div style="margin: -6px 0 14px 0; color:#555;" id="subtitle">__APP_SUBTITLE__</div>
  <div style="margin-bottom:12px;"><a href="/v2/">Prisammenligning V2 &rarr;</a></div>
  <div class="card">
    <div class="row">
      <div class="col">
        <h3>1) Katalog (Legekontor)</h3>
        <div id="catalogInfo" class="muted"></div>
        <input id="catalog" type="file" accept=".xlsx"/>
        <button id="btnCatalog">Last opp med kolonnemapping</button>
        <button id="btnCatalogDirect" class="secondary" style="margin-left:4px;" title="Last opp direkte uten mapping (bruker automatisk kolonnegjenkjenning)">Direkte opplasting</button>
        <div id="catStatus" class="muted" style="margin-top:6px;"></div>
        <div class="muted" style="margin-top:8px; font-size:12px; line-height:1.5;">
          <b>Kolonnemapping</b> lar deg velge hvilke kolonner i din fil som tilsvarer systemets felter.
          Anbefales hvis kolonnene har uvanlige navn. <b>Direkte opplasting</b> bruker automatisk gjenkjenning.
        </div>
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
        <input id="input" type="file" accept=".xlsx,.pdf"/>
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

  <!-- Column Mapping Modal -->
  <div class="modal-overlay" id="mappingModal">
    <div class="modal">
      <h3>Kolonnemapping</h3>
      <div class="help-text">
        Koble kolonnene fra din fil til systemets felter. Systemet foreslaar en mapping basert paa kolonnenavnene
        i filen. Juster der det trengs.<br/>
        <b>Pakrevde felt</b> maa vaere mappet for du kan fortsette. Spesielt viktig er <b>GID</b> (Global Item ID)
        som brukes til aa bygge produktlenker paa onemed.no.
      </div>
      <div id="sheetInfo"></div>
      <div id="samplePreview"></div>
      <table class="mapping-table">
        <thead>
          <tr><th style="width:45%;">Systemfelt</th><th>Din kolonne</th></tr>
        </thead>
        <tbody id="mappingRows"></tbody>
      </table>
      <div id="mappingError" style="color:#dc2626; font-size:13px; margin-top:8px; display:none;"></div>
      <div class="modal-actions">
        <button class="secondary" onclick="closeMappingModal()">Avbryt</button>
        <button id="btnConfirmMapping" onclick="confirmMapping()">Last opp med denne mappingen</button>
      </div>
    </div>
  </div>

<script>
let taskId = null;
let pendingCatalogFile = null;
let previewData = null;

function esc(s) {
  return String(s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function formatOsloTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return new Intl.DateTimeFormat("nb-NO", {
      timeZone: "Europe/Oslo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(d);
  } catch {
    return iso;
  }
}

async function refreshCatalogInfo() {
  const resp = await fetch("/catalog_status");
  const data = await resp.json();
  document.getElementById("debug").textContent = JSON.stringify(data, null, 2);

  const el = document.getElementById("catalogInfo");
  if (data.path) {
    el.innerHTML =
      '<div><b>Path:</b> ' + esc(data.path) + '</div>' +
      '<div><b>Finnes paa disk:</b> ' + (data.exists ? "Ja" : "Nei") + '</div>' +
      '<div><b>Lastet i minne:</b> ' + (data.loaded ? "Ja" : "Nei") + '</div>' +
      '<div><b>Produkter:</b> ' + esc(data.items) + '</div>' +
      '<div><b>Sist oppdatert (Oslo):</b> ' + esc(formatOsloTime(data.updated_at)) + '</div>' +
      '<div><b>Embeddings:</b> ' + esc((data.embeddings && data.embeddings.state) || "") + '</div>';
  }

  document.getElementById("btnMatch").disabled = !data.loaded;
}

// --- Column Mapping Upload Flow ---

async function uploadCatalogWithMapping() {
  const file = document.getElementById("catalog").files[0];
  if (!file) return alert("Velg en katalogfil (.xlsx)");

  document.getElementById("catStatus").textContent = "Leser kolonneinformasjon...";
  pendingCatalogFile = file;

  const fd = new FormData();
  fd.append("file", file);

  try {
    const resp = await fetch("/preview_catalog", { method: "POST", body: fd });
    const data = await resp.json();

    if (!resp.ok) {
      document.getElementById("catStatus").textContent = "Feil: " + (data.error || "ukjent");
      return;
    }

    previewData = data;
    document.getElementById("catStatus").textContent = "";
    showMappingModal(data);
  } catch (e) {
    document.getElementById("catStatus").textContent = "Feil ved forhåndsvisning: " + e.message;
  }
}

function showMappingModal(data) {
  // Show sheet info
  const sheetHtml = data.sheets.map(function(s) {
    return '<b>' + esc(s.name) + '</b>: ' + s.columns.length + ' kolonner, ' + s.row_count + ' rader';
  }).join(' &nbsp;|&nbsp; ');
  document.getElementById("sheetInfo").innerHTML = '<div class="sheet-info">Ark i filen: ' + sheetHtml + '</div>';

  // Show sample data preview
  if (data.sheets.length > 0 && data.sheets[0].sample.length > 0) {
    const s = data.sheets[0];
    let tbl = '<div class="sample-preview"><div style="margin-bottom:4px;font-weight:600;">Eksempeldata (' + esc(s.name) + '):</div><table><thead><tr>';
    for (const c of s.columns.slice(0, 8)) {
      tbl += '<th>' + esc(c) + '</th>';
    }
    if (s.columns.length > 8) tbl += '<th>...</th>';
    tbl += '</tr></thead><tbody>';
    for (const row of s.sample) {
      tbl += '<tr>';
      for (const c of s.columns.slice(0, 8)) {
        tbl += '<td title="' + esc(row[c] || '') + '">' + esc((row[c] || '').substring(0, 40)) + '</td>';
      }
      if (s.columns.length > 8) tbl += '<td>...</td>';
      tbl += '</tr>';
    }
    tbl += '</tbody></table></div>';
    document.getElementById("samplePreview").innerHTML = tbl;
  }

  // Build mapping rows
  const fields = data.system_fields || [];
  const suggested = data.suggested_mapping || {};
  const allCols = data.all_columns || [];

  let rowsHtml = '';
  for (const f of fields) {
    const suggested_val = suggested[f.key] || '';
    const badge = f.required
      ? '<span class="required-badge">Pakrevd</span>'
      : '<span class="optional-badge">Valgfri</span>';

    rowsHtml += '<tr>';
    rowsHtml += '<td><div class="field-name">' + esc(f.label) + badge + '</div>';
    rowsHtml += '<div class="field-desc">' + esc(f.description) + '</div></td>';
    rowsHtml += '<td><select id="map-' + esc(f.key) + '" data-key="' + esc(f.key) + '" data-required="' + (f.required ? '1' : '0') + '" onchange="updateSelectStyle(this)">';
    rowsHtml += '<option value="__none__">(ingen)</option>';
    for (const col of allCols) {
      const sel = (col === suggested_val) ? ' selected' : '';
      rowsHtml += '<option value="' + esc(col) + '"' + sel + '>' + esc(col) + '</option>';
    }
    rowsHtml += '</select></td>';
    rowsHtml += '</tr>';
  }
  document.getElementById("mappingRows").innerHTML = rowsHtml;

  // Update select styles
  document.querySelectorAll('.mapping-table select').forEach(updateSelectStyle);

  document.getElementById("mappingError").style.display = 'none';
  document.getElementById("mappingModal").classList.add("active");
}

function updateSelectStyle(sel) {
  const isRequired = sel.getAttribute('data-required') === '1';
  const isMapped = sel.value && sel.value !== '__none__';
  sel.className = '';
  if (isMapped) {
    sel.className = 'mapped';
  } else if (isRequired) {
    sel.className = 'unmapped-required';
  }
}

function closeMappingModal() {
  document.getElementById("mappingModal").classList.remove("active");
  pendingCatalogFile = null;
  previewData = null;
}

async function confirmMapping() {
  if (!pendingCatalogFile) return;

  // Collect mapping from selects
  const mapping = {};
  const selects = document.querySelectorAll('.mapping-table select');
  const missingRequired = [];

  selects.forEach(function(sel) {
    const key = sel.getAttribute('data-key');
    const isRequired = sel.getAttribute('data-required') === '1';
    const val = sel.value;
    if (val && val !== '__none__') {
      mapping[key] = val;
    } else if (isRequired) {
      missingRequired.push(key);
    }
  });

  if (missingRequired.length > 0) {
    const errEl = document.getElementById("mappingError");
    errEl.textContent = "Pakrevde felt mangler mapping: " + missingRequired.join(", ");
    errEl.style.display = 'block';
    return;
  }

  // Check for duplicate column selections
  const usedCols = Object.values(mapping);
  const dupes = usedCols.filter(function(c, i) { return usedCols.indexOf(c) !== i; });
  if (dupes.length > 0) {
    const errEl = document.getElementById("mappingError");
    errEl.textContent = "Samme kolonne er valgt for flere felt: " + [...new Set(dupes)].join(", ");
    errEl.style.display = 'block';
    return;
  }

  document.getElementById("btnConfirmMapping").disabled = true;
  document.getElementById("btnConfirmMapping").textContent = "Laster opp...";

  const fd = new FormData();
  fd.append("file", pendingCatalogFile);
  fd.append("mapping", JSON.stringify(mapping));

  try {
    const resp = await fetch("/upload_catalog_mapped", { method: "POST", body: fd });
    const data = await resp.json().catch(function() { return {}; });

    if (!resp.ok) {
      document.getElementById("catStatus").textContent = "Feil: " + (data.error || "ukjent");
    } else {
      document.getElementById("catStatus").textContent = "Katalog oppdatert med mapping. Produkter: " + data.items;
    }
  } catch (e) {
    document.getElementById("catStatus").textContent = "Feil: " + e.message;
  }

  closeMappingModal();
  document.getElementById("btnConfirmMapping").disabled = false;
  document.getElementById("btnConfirmMapping").textContent = "Last opp med denne mappingen";
  await refreshCatalogInfo();
}

// --- Direct Upload (no mapping) ---

async function uploadCatalogDirect() {
  const file = document.getElementById("catalog").files[0];
  if (!file) return alert("Velg en katalogfil (.xlsx)");
  document.getElementById("catStatus").textContent = "Laster opp katalog...";

  const fd = new FormData();
  fd.append("file", file);

  const resp = await fetch("/upload_catalog", { method: "POST", body: fd });
  const data = await resp.json().catch(function() { return {}; });

  if (!resp.ok) {
    document.getElementById("catStatus").textContent = "Feil: " + (data.error || "ukjent");
    await refreshCatalogInfo();
    return;
  }

  document.getElementById("catStatus").textContent = "Katalog oppdatert. Produkter: " + data.items;
  await refreshCatalogInfo();
}

// --- Matching ---

async function startMatch() {
  const file = document.getElementById("input").files[0];
  if (!file) return alert("Velg en inputfil (.xlsx eller .pdf)");

  document.getElementById("matchStatus").textContent = "Starter matching...";
  document.getElementById("btnCancel").disabled = false;
  document.getElementById("download").innerHTML = "";
  document.getElementById("bar").style.width = "1%";

  const fd = new FormData();
  fd.append("file", file);
  const pref = document.querySelector("input[name=prefer]:checked");
  fd.append("prefer_own_brands", pref ? pref.value : "1");

  const resp = await fetch("/match", { method: "POST", body: fd });
  const data = await resp.json().catch(function() { return {}; });

  if (!resp.ok) {
    document.getElementById("matchStatus").textContent = "Feil: " + (data.error || "ukjent");
    document.getElementById("btnCancel").disabled = true;
    return;
  }

  taskId = data.task_id;
  pollProgress();
}

async function pollProgress() {
  const resp = await fetch('/progress/' + taskId);
  const data = await resp.json().catch(function() { return {}; });

  const p = Math.round((data.progress || 0) * 100);
  document.getElementById("bar").style.width = p + '%';

  if (data.status === "cancel_requested") {
    document.getElementById("matchStatus").textContent = "Avbryt forespurt...";
  } else {
    document.getElementById("matchStatus").textContent = data.status || "running";
  }

  if (data.status === "done") {
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("bar").style.width = "100%";
    document.getElementById("download").innerHTML = '<a href="/download/' + esc(taskId) + '">Last ned resultat</a>';
    await loadHistory();
    return;
  }

  if (data.status === "cancelled") {
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("matchStatus").textContent = "Avbrutt";
    await loadHistory();
    return;
  }

  if (data.status === "error") {
    document.getElementById("btnCancel").disabled = true;
    document.getElementById("matchStatus").textContent = "Feil: " + (data.error || "");
    await loadHistory();
    return;
  }

  setTimeout(pollProgress, 2500);
}

async function loadHistory() {
  const resp = await fetch("/history?limit=200");
  const data = await resp.json();

  let html = "<table>";
  html += "<thead><tr>";
  html += "<th>Tidspunkt (Oslo)</th><th>Filnavn</th><th>Status</th><th class='right'>Rader</th><th>Last ned</th><th>Avbryt</th>";
  html += "</tr></thead><tbody>";

  for (const j of (data.jobs || [])) {
    const created = esc(formatOsloTime(j.created_at || ""));
    const fn = esc(j.input_filename || "");
    const st = esc(j.status || "");
    const rows = esc(j.rows_out ?? "");
    const dl = j.status === "done" ? '<a href="/download/' + esc(j.task_id) + '">Last ned</a>' : "";
    const cancel = (j.status === "running" || j.status === "cancel_requested") ? '<button class="secondary" onclick="cancelJob(&apos;' + esc(j.task_id) + '&apos;)">Avbryt</button>' : "";
    html += '<tr><td>' + created + '</td><td>' + fn + '</td><td>' + st + '</td><td class="right">' + rows + '</td><td>' + dl + '</td><td>' + cancel + '</td></tr>';
  }

  html += "</tbody></table>";
  document.getElementById("history").innerHTML = html;
}

async function cancelJob(tid) {
  if (!tid) return;
  try {
    await fetch('/cancel/' + tid, { method: "POST" });
  } catch (e) {}
  if (tid === taskId) {
    document.getElementById("matchStatus").textContent = "Avbryt forespurt...";
  }
  setTimeout(loadHistory, 600);
}

document.getElementById("btnCatalog").addEventListener("click", uploadCatalogWithMapping);
document.getElementById("btnCatalogDirect").addEventListener("click", uploadCatalogDirect);
document.getElementById("btnMatch").addEventListener("click", startMatch);
document.getElementById("btnCancel").addEventListener("click", async function() {
  if (!taskId) return;
  await fetch('/cancel/' + taskId, { method: "POST" });
  document.getElementById("matchStatus").textContent = "Avbryt forespurt...";
});

refreshCatalogInfo();
loadHistory();
</script>
</body>
</html>
""".replace("__APP_TITLE__", APP_TITLE).replace("__APP_SUBTITLE__", APP_SUBTITLE or "")



@app.get("/static/logo.png")
def static_logo(_: User = Depends(require_sso)):
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


@app.get("/", response_class=HTMLResponse)
def index(_: User = Depends(require_sso)):
    return RedirectResponse(url="/v2/", status_code=302)
@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: User = Depends(require_sso)):
    return HTMLResponse(INDEX_HTML)


@app.get("/catalog_status")
def catalog_status(_: User = Depends(require_sso)):
    meta = catalog_meta()
    meta["exists"] = CATALOG_PATH.exists()
    meta["loaded"] = CATALOG_BUNDLE is not None
    meta["embeddings"] = embeddings_meta()
    return meta


@app.post("/preview_catalog")
async def preview_catalog(request: Request, file: UploadFile = File(...), _: User = Depends(require_sso)):
    """Preview catalog file: return sheet names, columns, sample data, and auto-mapping suggestions."""
    _check_upload_size(request)
    content = await file.read()
    filename = (file.filename or "").lower()

    if not (_is_xlsx_bytes(content) or filename.endswith(".xlsx")):
        return JSONResponse({"error": "Katalog ma vaere en Excel-fil (.xlsx)."}, status_code=400)

    try:
        buf = BytesIO(content)
        xls = pd.ExcelFile(buf)
        sheets_info = []
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(buf, sheet_name=sheet_name, nrows=5)
            columns = [str(c) for c in df.columns]
            # Sample: first 3 rows as dicts
            sample = []
            for _, row in df.head(3).iterrows():
                sample.append({str(k): ("" if pd.isna(v) else str(v)) for k, v in row.items()})
            sheets_info.append({
                "name": sheet_name,
                "columns": columns,
                "row_count": len(pd.read_excel(buf, sheet_name=sheet_name, usecols=[0])),
                "sample": sample,
            })

        # Auto-suggest mapping using columns from all sheets combined
        all_columns = []
        seen = set()
        for s in sheets_info:
            for c in s["columns"]:
                if c not in seen:
                    all_columns.append(c)
                    seen.add(c)

        suggested = _auto_suggest_mapping(all_columns)

        return {
            "ok": True,
            "sheets": sheets_info,
            "all_columns": all_columns,
            "system_fields": CATALOG_SYSTEM_FIELDS,
            "suggested_mapping": suggested,
        }
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lese katalogfil: {e}"}, status_code=400)


@app.post("/upload_catalog")
async def upload_catalog(request: Request, file: UploadFile = File(...), _: User = Depends(require_sso)):
    """
    Katalog-opplasting (kun .xlsx).
    NB: Denne endpointen skal IKKE forsoeke a parse PDF - det er kun for /match.
    """
    global CATALOG_BUNDLE

    _check_upload_size(request)
    content = await file.read()
    filename = (file.filename or "").lower()

    # Sniff: XLSX er zip (PK). Noen ganger kan filendelsen vaere feil, sa vi sjekker begge.
    if not (_is_xlsx_bytes(content) or filename.endswith(".xlsx")):
        return JSONResponse({"error": "Katalog ma vaere en Excel-fil (.xlsx)."}, status_code=400)

    try:
        CATALOG_PATH.write_bytes(content)
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lagre katalog: {e}"}, status_code=500)

    # Clear any previous mapping (direct upload without mapping step)
    if CATALOG_MAPPING_PATH.exists():
        CATALOG_MAPPING_PATH.unlink(missing_ok=True)

    try:
        load_catalog_from_disk()
    except Exception as e:
        CATALOG_BUNDLE = None
        return JSONResponse({"error": f"Katalog lagret, men feilet a laste: {e}"}, status_code=500)

    return {"ok": True, "items": CATALOG_BUNDLE.items_count() if CATALOG_BUNDLE else 0}


@app.post("/upload_catalog_mapped")
async def upload_catalog_mapped(request: Request, _: User = Depends(require_sso)):
    """Upload catalog with explicit column mapping.

    Expects multipart form: file + mapping (JSON string).
    """
    global CATALOG_BUNDLE

    _check_upload_size(request)
    form = await request.form()
    file = form.get("file")
    mapping_json = form.get("mapping", "{}")

    if not file:
        return JSONResponse({"error": "Ingen fil mottatt."}, status_code=400)

    content = await file.read()
    filename = (file.filename or "").lower()

    if not (_is_xlsx_bytes(content) or filename.endswith(".xlsx")):
        return JSONResponse({"error": "Katalog ma vaere en Excel-fil (.xlsx)."}, status_code=400)

    # Parse mapping
    try:
        mapping = json.loads(mapping_json) if isinstance(mapping_json, str) else {}
    except (json.JSONDecodeError, TypeError):
        mapping = {}

    # Validate required fields are mapped
    missing_required = []
    for field_def in CATALOG_SYSTEM_FIELDS:
        if field_def["required"]:
            mapped_col = mapping.get(field_def["key"], "")
            if not mapped_col or mapped_col == "__none__":
                missing_required.append(field_def["label"])
    if missing_required:
        return JSONResponse(
            {"error": f"Pakrevde felt mangler mapping: {', '.join(missing_required)}"},
            status_code=400,
        )

    # Save file
    try:
        CATALOG_PATH.write_bytes(content)
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lagre katalog: {e}"}, status_code=500)

    # Save mapping
    try:
        CATALOG_MAPPING_PATH.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        return JSONResponse({"error": f"Kunne ikke lagre mapping: {e}"}, status_code=500)

    # Load with mapping
    try:
        load_catalog_from_disk()
    except Exception as e:
        CATALOG_BUNDLE = None
        return JSONResponse({"error": f"Katalog lagret, men feilet a laste: {e}"}, status_code=500)

    return {"ok": True, "items": CATALOG_BUNDLE.items_count() if CATALOG_BUNDLE else 0}








