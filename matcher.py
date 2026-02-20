# matcher.py
# ------------------------------------------------------------
# Forbedret produktmatching med hybrid embedding + BM25 + Claude (AI-first)
#
# NYTT i denne versjonen:
#  1) Norsk-smarte tokens:
#     - splitter sammensatte ord (inkontinensbind -> inkontinens + bind)
#     - synonym-boost (bind <-> innlegg/pads/absorbent, inkontinens <-> urinlekkasje)
#  2) Char-ngram fallback:
#     - hvis retrieval er svak: ranger kandidater med char 3-5grams (Jaccard) på en kandidatpool
#     - dette redder tilfeller der ordform/komposita gjør BM25/embedding svake
#  3) Halver Claude-kost:
#     - CLAUDE_TOP_K default 25 (ikke 50)
#     - Claude returnerer kun TOP 3 (ikke full liste med reasons på alle)
#     - (valgfritt) skip Claude når vi er "nær-identisk sikre" via embedding+margin
#
# Miljøvariabler:
#   ANTHROPIC_API_KEY (valgfritt men sterkt anbefalt)
#   CLAUDE_MODEL (default: claude-sonnet-4-20250514)
#   EMBED_MODEL (default: intfloat/multilingual-e5-small)
#
#   BM25_MIN_SCORE (default: 2.0) - terskel når Claude ikke brukes
#   EMBED_MIN_SCORE (default: 0.35) - terskel når Claude ikke brukes
#
#   CLAUDE_TOP_K (default: 25) - kandidater sendt til Claude (kost-driver #1)
#   CLAUDE_ALWAYS_VERIFY (default: 1) - Claude bestemmer når key finnes
#   CLAUDE_FALLBACK_BM25_K (default: 160) - kandidatpool ved svak/tom retrieval (til ngram-fallback)
#   CLAUDE_MIN_ACCEPT_SCORE (default: 55) - under dette => "Ingen"
#
#   NGRAM_FALLBACK_POOL (default: 260) - hvor mange kandidater som char-ngram rangeres over (fra loose BM25)
#   NGRAM_TOP_N (default: 80) - hvor mange som tas videre etter ngram-rangering
#   NGRAM_MIN_JACCARD (default: 0.04) - under dette regnes som for svakt (men AI-first kan fortsatt bruke Claude)
#
#   NEAR_IDENTICAL_GATE (default: 1) - skip Claude hvis vi er svært sikre via embedding+margin+tekstlikhet
#   EXACT_MATCH_MIN_EMBED (default: 0.86)
#   EXACT_MATCH_MIN_MARGIN (default: 0.10)
#   EXACT_MATCH_SEQ_RATIO (default: 0.93)

# Hvor mye webtekst som tas med i kandidattekst (for å unngå støy/kost)

#
# app.py forventer: from matcher import Catalog
# ------------------------------------------------------------
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Hvor mye webtekst som tas med i kandidattekst (for å unngå støy/kost)
WEB_TEXT_MAX_CHARS = int(os.getenv('WEB_TEXT_MAX_CHARS', '1200'))

__all__ = ["Catalog", "CancelledError"]

logger = logging.getLogger(__name__)

# -----------------------------
# Avbryt / cancel støtte
# -----------------------------
class CancelledError(Exception):
    """Kastes når bruker avbryter en match-jobb."""
    pass

def _check_cancel(cancel_event=None, cancel_cb=None) -> None:
    """Kast CancelledError hvis cancel_event er satt eller cancel_cb() returnerer True."""
    try:
        if cancel_event is not None and getattr(cancel_event, "is_set", None) and cancel_event.is_set():
            raise CancelledError("Avbrutt av bruker")
    except CancelledError:
        raise
    except Exception:
        # hvis cancel_event er noe annet / mangler is_set
        pass

    try:
        if cancel_cb is not None and callable(cancel_cb) and bool(cancel_cb()):
            raise CancelledError("Avbrutt av bruker")
    except CancelledError:
        raise
    except Exception:
        pass


# -----------------------------
# Output-skjema
# -----------------------------
OUTPUT_COLUMNS: List[str] = [
    "Kommentar",
    "Produktnavn",
    "Beskrivelse",
    "Spesifikasjon",
    "Konkurrent art.nr",
    "Produsent art.nr",
    "Match-kvalitet",
    "Matchet art.nr",
    "Katalog: Item Description",
    "Katalog: Specification",
    "Katalog: Producer Name",
    "Katalog: Producer Item Number",
    "Katalog: Storage instruction",
    "Katalog: Item group",
    "Katalog: Web Title",
    "Katalog: Web Text",
    "Katalog: ALC",
    "Alternativer (topp 3 art.nr)",
]


CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")

# Terskler (brukes primært hvis Claude ikke er tilgjengelig)
BM25_MIN_SCORE = float(os.getenv("BM25_MIN_SCORE", "2.0"))
EMBED_MIN_SCORE = float(os.getenv("EMBED_MIN_SCORE", "0.35"))

# Halver kost: default 25 (ikke 50)
CLAUDE_TOP_K = int(os.getenv("CLAUDE_TOP_K", "25"))

# AI-first
CLAUDE_ALWAYS_VERIFY = os.getenv("CLAUDE_ALWAYS_VERIFY", "1").strip() == "1"
CLAUDE_FALLBACK_BM25_K = int(os.getenv("CLAUDE_FALLBACK_BM25_K", "160"))
CLAUDE_MIN_ACCEPT_SCORE = int(os.getenv("CLAUDE_MIN_ACCEPT_SCORE", "55"))

# Char-ngram fallback
NGRAM_FALLBACK_POOL = int(os.getenv("NGRAM_FALLBACK_POOL", "260"))
NGRAM_TOP_N = int(os.getenv("NGRAM_TOP_N", "80"))
NGRAM_MIN_JACCARD = float(os.getenv("NGRAM_MIN_JACCARD", "0.04"))

# Near-identical gate (skip Claude når vi er svært sikre)
NEAR_IDENTICAL_GATE = os.getenv("NEAR_IDENTICAL_GATE", "1").strip() == "1"
EXACT_MATCH_MIN_EMBED = float(os.getenv("EXACT_MATCH_MIN_EMBED", "0.86"))
EXACT_MATCH_MIN_MARGIN = float(os.getenv("EXACT_MATCH_MIN_MARGIN", "0.10"))
EXACT_MATCH_SEQ_RATIO = float(os.getenv("EXACT_MATCH_SEQ_RATIO", "0.93"))

# -----------------------------
# Tekst / tokenisering
# -----------------------------
_WORD_RE = re.compile(r"[A-Za-zÆØÅæøå0-9]+", re.UNICODE)
STOPWORDS = {
    "og", "i", "på", "til", "for", "med", "uten", "som", "den", "det", "de", "en", "ei", "et",
    "ca", "str", "the", "and", "or", "a", "an", "of", "to", "in", "with", "without",
}

# Kjente generiske kategorinavn som IKKE bør brukes som søketekst
GENERIC_CATEGORIES = {
    "inkontinens", "urologisk utstyr", "desinfeksjon", "hygiene",
    "diagnostikk og labprodukter", "ernæring", "smittevern",
    "sprøyter og kanyler", "sutur", "sårbehandling og førstehjelp",
    "pleieprodukter", "renholdsmidler og kjemikaler", "respirasjon",
}

# Komposita-endelser: inkontinensbind -> inkontinens + bind
COMPOUND_SUFFIXES = [
    "bind", "innlegg", "bleie", "bleier", "pad", "pads", "slip", "underbukse",
    "kateter", "sprøyte", "kanyle", "kompress", "bandasje",
    "støttebandasje", "støtte", "plaster", "hanske", "hansker",
]

# Synonym-boost (token-nivå): gir BM25 flere "hooks"
SYNONYMS: Dict[str, List[str]] = {
    "bind": ["innlegg", "pad", "pads", "absorbent", "absorberende"],
    "innlegg": ["bind", "pad", "pads", "absorbent", "absorberende"],
    "pad": ["pads", "bind", "innlegg"],
    "pads": ["pad", "bind", "innlegg"],
    "inkontinens": ["urinlekkasje", "urinlekkasjer", "lekkasje", "urin"],
    "urinlekkasje": ["inkontinens", "urin"],
    "bleie": ["bleier", "slip", "absorberende", "absorbent"],
    "bleier": ["bleie", "slip", "absorberende", "absorbent"],
    "mann": ["herre", "menn", "male", "men"],
    "herre": ["mann", "menn", "male", "men"],
    "kvinne": ["dame", "lady", "female", "women"],
    "dame": ["kvinne", "lady", "female", "women"],
}

# -----------------------------
# Preferanse for egne merkevarer
# -----------------------------
# Brukes som en mild score-bonus i retrieval + tie-break i sortering.
# Målet er å prioritere egne merker når alternativene ellers er svært like.
PREFERRED_BRANDS: List[str] = ["Evercare", "Vitri", "Selefa", "Embra"]
BRAND_BONUS_STEP = float(os.getenv("BRAND_BONUS_STEP", "0.015"))  # 0.015 gir maks ~0.06 for Evercare
BRAND_BONUS_MAX = float(os.getenv("BRAND_BONUS_MAX", "0.06"))
PREFER_OWN_BRANDS_DEFAULT = os.getenv("PREFER_OWN_BRANDS_DEFAULT", "1").strip() == "1"

# -----------------------------
# ALC (kost/score) preferanse
# -----------------------------
# Lavere ALC skal gi en mild fordel når kandidatene ellers er like.
ALC_FIELD_CANDIDATES = ["Katalog: ALC", "ALC", "alc", "Alc"]
ALC_BONUS_WEIGHT = float(os.getenv("ALC_BONUS_WEIGHT", "0.03"))  # mild bonus, ikke overstyr relevans

# -----------------------------
# Claude-kontroll for hastighet
# -----------------------------
# "always"  => verifiser alle rader med Claude
# "uncertain" => verifiser kun når retrieval er usikkert (default)
CLAUDE_VERIFY_MODE = os.getenv("CLAUDE_VERIFY_MODE", "uncertain").strip().lower()
CLAUDE_VERIFY_MIN_EMBED = float(os.getenv("CLAUDE_VERIFY_MIN_EMBED", "0.72"))
CLAUDE_VERIFY_MIN_MARGIN = float(os.getenv("CLAUDE_VERIFY_MIN_MARGIN", "0.06"))
CLAUDE_TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "35"))

def _brand_rank(text: str) -> int:
    t = _norm(text).lower()
    for i, b in enumerate(PREFERRED_BRANDS):
        if b.lower() in t:
            return i
    return 999

def _brand_bonus(item: "_CatalogItem") -> float:
    # Søk i mest informative felter
    hay = " ".join([
        _norm(item.row.get("Katalog: Producer Name", "")),
        _norm(item.row.get("Katalog: Item Description", "")),
        _norm(item.row.get("Katalog: Web Title", "")),
        _norm(item.row.get("Katalog: Web Text", "")),
        item.text or "",
    ])
    r = _brand_rank(hay)
    if r >= 999:
        return 0.0
    bonus = (len(PREFERRED_BRANDS) - r) * BRAND_BONUS_STEP
    return min(float(bonus), BRAND_BONUS_MAX)

def _alc_value(item: "_CatalogItem") -> float:
    """Hent ALC (lavere = bedre). Returnerer +inf hvis mangler/ugyldig."""
    for k in ALC_FIELD_CANDIDATES:
        v = item.row.get(k)
        if v is None:
            continue
        s = _norm(v)
        if not s:
            continue
        try:
            # håndter '1,23' også
            s2 = s.replace(",", ".")
            return float(s2)
        except Exception:
            continue
    return float("inf")


def _alc_bonus(item: "_CatalogItem") -> float:
    """Mild bonus basert på lav ALC. 0 hvis ALC mangler."""
    alc = _alc_value(item)
    if not np.isfinite(alc):
        return 0.0
    # 1/(1+alc) gir avtakende effekt; ALC_BONUS_WEIGHT styrer styrken
    return float(ALC_BONUS_WEIGHT * (1.0 / (1.0 + max(0.0, alc))))



def _norm_key(s: str) -> str:
    """Normaliser kolonnenavn for robust matching."""
    s = (s or "").strip().lower()
    # Bytt ut vanlige skilletegn med mellomrom
    s = re.sub(r"[\u00a0\t\r\n]+", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _norm(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    if s.strip().lower() == "nan":
        return ""
    return s.replace("\u00a0", " ").strip()


def _is_generic_category(text: str) -> bool:
    return text.strip().lower() in GENERIC_CATEGORIES


def _split_compounds(token: str) -> List[str]:
    out = [token]
    for suf in COMPOUND_SUFFIXES:
        if token.endswith(suf) and len(token) > len(suf) + 3:
            stem = token[: -len(suf)]
            # unngå ekstremt korte "stammer"
            if len(stem) >= 4:
                out.append(stem)
                out.append(suf)
    return out


def _tokenize(s: str) -> List[str]:
    s = _norm(s).lower()
    toks = _WORD_RE.findall(s)

    expanded: List[str] = []
    for t in toks:
        if t in STOPWORDS or len(t) <= 1:
            continue

        # split sammensatte ord
        parts = _split_compounds(t)
        for p in parts:
            if not p or p in STOPWORDS or len(p) <= 1:
                continue
            expanded.append(p)

            # synonym-boost
            for syn in SYNONYMS.get(p, []):
                if syn and syn not in STOPWORDS and len(syn) > 1:
                    expanded.append(syn)

    return expanded


def _norm_text_for_sim(s: str) -> str:
    s = _norm(s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


# -----------------------------
# Query builders
# -----------------------------
def _build_query_text(row: Dict[str, Any]) -> str:
    parts: List[str] = []

    produktnavn = _norm(row.get("Produktnavn", ""))
    beskrivelse = _norm(row.get("Beskrivelse", ""))
    spesifikasjon = _norm(row.get("Spesifikasjon", ""))
    konkurrent_artnr = _norm(row.get("Konkurrent art.nr", ""))
    produsent_artnr = _norm(row.get("Produsent art.nr", ""))

    if produktnavn and not _is_generic_category(produktnavn):
        parts.append(produktnavn)

    # Beskrivelse viktigst -> dobbel vekt
    if beskrivelse:
        parts.append(beskrivelse)
        parts.append(beskrivelse)

    if spesifikasjon:
        parts.append(spesifikasjon)

    if konkurrent_artnr:
        parts.append(f"art.nr {konkurrent_artnr}")
    if produsent_artnr:
        parts.append(f"produsent {produsent_artnr}")

    return " ".join(parts).strip()


def _build_query_text_for_claude(row: Dict[str, Any]) -> str:
    parts: List[str] = []

    produktnavn = _norm(row.get("Produktnavn", ""))
    beskrivelse = _norm(row.get("Beskrivelse", ""))
    spesifikasjon = _norm(row.get("Spesifikasjon", ""))
    konkurrent_artnr = _norm(row.get("Konkurrent art.nr", ""))
    produsent_artnr = _norm(row.get("Produsent art.nr", ""))

    if produktnavn:
        is_cat = _is_generic_category(produktnavn)
        parts.append(f"Kategori: {produktnavn}" if is_cat else f"Produktnavn: {produktnavn}")

    if beskrivelse:
        parts.append(f"Beskrivelse: {beskrivelse}")
    if spesifikasjon:
        parts.append(f"Spesifikasjon: {spesifikasjon}")
    if konkurrent_artnr:
        parts.append(f"Konkurrent art.nr: {konkurrent_artnr}")
    if produsent_artnr:
        parts.append(f"Produsent art.nr: {produsent_artnr}")

    return "\n".join(parts).strip()


def _build_candidate_text(cat_row: Dict[str, Any]) -> str:
    """
    Bygger tekstgrunnlag for katalogprodukter som brukes av både BM25 og embeddings.

    Viktig:
    - Tar med Web Title / Web Text / Item Group dersom de finnes (gir mye bedre treff på fritekst-krav).
    - Trunker Web Text for å unngå for mye støy og for å holde embedding-kost nede.
    """
    parts: List[str] = []

    # Primærfelter (alltid)
    for k in (
        "Katalog: Item Description",
        "Katalog: Specification",
        "Katalog: Producer Name",
        "Katalog: Producer Item Number",
        "Katalog: Artikkelnummer",
    ):
        v = _norm(cat_row.get(k, ""))
        if v:
            parts.append(v)

    # Nye / ekstra felter (hvis de finnes i katalogen)
    item_group = _norm(cat_row.get("Katalog: Item Group", cat_row.get("Item Group", cat_row.get("Item group", ""))))
    if item_group:
        parts.append(item_group)

    web_title = _norm(cat_row.get("Katalog: Web Title", cat_row.get("Web Title", cat_row.get("Web title", ""))))
    if web_title:
        parts.append(web_title)

    web_text = _norm(cat_row.get("Katalog: Web Text", cat_row.get("Web Text", cat_row.get("Web text", ""))))
    if web_text:
        if len(web_text) > WEB_TEXT_MAX_CHARS:
            web_text = web_text[:WEB_TEXT_MAX_CHARS]
        parts.append(web_text)

    storage = _norm(cat_row.get("Katalog: Storage instruction", cat_row.get("Storage instruction", cat_row.get("Storage Instruction", ""))))
    if storage:
        parts.append(storage)


    return " | ".join([p for p in parts if p]).strip()


# -----------------------------
# Catalog item
# -----------------------------
@dataclass
class _CatalogItem:
    artnr: str
    row: Dict[str, Any]
    text: str
    toks: List[str]
    embedding: Optional[np.ndarray] = field(default=None, repr=False)
    text_norm: str = field(default="", repr=False)  # for ngram/sim


# -----------------------------
# BM25-ish indeks
# -----------------------------
class _BM25Index:
    def __init__(self, docs: List[_CatalogItem], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        self.doc_freq: Dict[str, int] = {}
        self.doc_len: List[int] = []
        self.avgdl: float = 0.0
        self._build()

    def _build(self) -> None:
        N = len(self.docs)
        df: Dict[str, int] = {}
        lengths: List[int] = []
        for d in self.docs:
            seen = set(d.toks)
            lengths.append(len(d.toks))
            for t in seen:
                df[t] = df.get(t, 0) + 1
        self.doc_freq = df
        self.doc_len = lengths
        self.avgdl = (sum(lengths) / N) if N else 0.0

    def _idf(self, term: str) -> float:
        N = len(self.docs)
        n = self.doc_freq.get(term, 0)
        return math.log(1 + (N - n + 0.5) / (n + 0.5)) if N else 0.0

    def _score(self, q_toks: List[str], doc: _CatalogItem, doc_idx: int) -> float:
        tf: Dict[str, int] = {}
        for t in doc.toks:
            tf[t] = tf.get(t, 0) + 1

        dl = self.doc_len[doc_idx] if doc_idx < len(self.doc_len) else len(doc.toks)
        denom_base = self.k1 * (1 - self.b + self.b * (dl / self.avgdl)) if self.avgdl else 1.0

        s = 0.0
        for t in q_toks:
            if t not in tf:
                continue
            idf = self._idf(t)
            f = tf[t]
            s += idf * (f * (self.k1 + 1)) / (f + denom_base)
        return s

    def top_n(self, query_text: str, n: int = 30) -> List[Tuple[_CatalogItem, float]]:
        q_toks = _tokenize(query_text)
        if not q_toks:
            return []
        scored: List[Tuple[_CatalogItem, float]] = []
        for idx, d in enumerate(self.docs):
            sc = self._score(q_toks, d, idx)
            if sc > 0:
                scored.append((d, sc))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:n]

    def top_n_loose(self, query_text: str, n: int = 160) -> List[Tuple[_CatalogItem, float]]:
        q_toks = _tokenize(query_text)
        if not q_toks:
            return []
        scored: List[Tuple[_CatalogItem, float]] = []
        for idx, d in enumerate(self.docs):
            sc = self._score(q_toks, d, idx)
            scored.append((d, sc))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:n]


# -----------------------------
# Embedding indeks
# -----------------------------

# -----------------------------
# Embedding indeks (med persistent disk-cache)
# -----------------------------
class _EmbeddingIndex:
    """Wrapper rundt sentence-transformers + numpy for cosine-similarity.
    Cache'r embeddings til disk så de ikke må bygges på nytt ved hver restart.
    """

    def __init__(self, items: List[_CatalogItem], model_name: str = EMBED_MODEL) -> None:
        self.items = items
        self.model = None
        self.embeddings: Optional[np.ndarray] = None
        self.model_name = model_name

        self.cache_dir = os.getenv("EMBED_CACHE_DIR", os.path.join(os.getenv("DATA_DIR", "/data"), "embed_cache"))
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception:
            pass

        self._build_or_load()

    def _cache_paths(self) -> Tuple[str, str]:
        return (
            os.path.join(self.cache_dir, "embeddings.npy"),
            os.path.join(self.cache_dir, "meta.json"),
        )

    def _fingerprint(self) -> str:
        """Fingeravtrykk basert på artnr-rekkefølge (endrer seg når katalog endrer seg)."""
        import hashlib
        joined = "|".join([it.artnr for it in self.items]).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()

    def _try_load_cache(self) -> bool:
        emb_path, meta_path = self._cache_paths()
        if not (os.path.exists(emb_path) and os.path.exists(meta_path)):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("model") != self.model_name:
                return False
            if meta.get("fingerprint") != self._fingerprint():
                return False

            emb = np.load(emb_path, mmap_mode="r")
            if emb.shape[0] != len(self.items):
                return False

            self.embeddings = emb

            # last modell for queries (lettvekts)
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(self.model_name)

            logger.info("Embedding-indeks lastet fra disk-cache ✅")
            return True
        except Exception as e:
            logger.warning(f"Kunne ikke laste embedding-cache, bygger på nytt: {e}")
            return False

    def _save_cache(self, emb: np.ndarray) -> None:
        emb_path, meta_path = self._cache_paths()
        try:
            np.save(emb_path, emb.astype("float32"))
            meta = {
                "model": self.model_name,
                "count": int(emb.shape[0]),
                "dim": int(emb.shape[1]),
                "fingerprint": self._fingerprint(),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Kunne ikke skrive embedding-cache: {e}")

    def _build_or_load(self) -> None:
        # 1) forsøk cache først
        if self._try_load_cache():
            return

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning("sentence-transformers ikke installert, embedding-søk deaktivert")
            return

        logger.info(f"Laster embedding-modell: {self.model_name}")
        self.model = SentenceTransformer(self.model_name)

        texts = ["passage: " + item.text for item in self.items]
        logger.info(f"Beregner embeddings for {len(texts)} katalogprodukter...")

        batch_size = int(os.getenv('EMBED_BATCH_SIZE', '16'))
        emb = self.model.encode(texts, batch_size=batch_size, show_progress_bar=False, convert_to_numpy=True)
        emb = emb.astype("float32")

        norms = np.linalg.norm(emb, axis=1, keepdims=True) + 1e-12
        emb = emb / norms
        self.embeddings = emb

        # behold i items (brukes noen steder), men unngå dobbel lagring om ønskelig
        for i, item in enumerate(self.items):
            item.embedding = emb[i]

        self._save_cache(emb)
        logger.info("Embedding-indeks klar (cachet til disk) ✅")

    @property
    def available(self) -> bool:
        return self.model is not None and self.embeddings is not None

    def top_n(self, query_text: str, n: int = 30) -> List[Tuple[_CatalogItem, float]]:
        if not self.available:
            return []

        q = "query: " + query_text
        q_emb = self.model.encode([q], convert_to_numpy=True).astype("float32")
        q_norm = np.linalg.norm(q_emb, axis=1, keepdims=True) + 1e-12
        q_emb = q_emb / q_norm

        scores = (self.embeddings @ q_emb.T).flatten()
        top_indices = np.argsort(scores)[::-1][:n]

        results = []
        for idx in top_indices:
            results.append((self.items[idx], float(scores[idx])))
        return results
# -----------------------------
# Hybrid retrieval
# -----------------------------
def _hybrid_retrieve(
    query_text: str,
    bm25_index: _BM25Index,
    embed_index: Optional[_EmbeddingIndex],
    top_n: int = 30,
    bm25_weight: float = 0.3,
    embed_weight: float = 0.7,
    prefer_own_brands: bool = True,
) -> List[Tuple[_CatalogItem, float]]:
    """
    Kombiner BM25 og embedding-resultater med reciprocal rank fusion.
    """
    bm25_results = bm25_index.top_n(query_text, n=top_n * 2)

    embed_results = []
    if embed_index and embed_index.available:
        embed_results = embed_index.top_n(query_text, n=top_n * 2)

    if not embed_results:
        return bm25_results[:top_n]

    k = 60
    rrf_scores: Dict[str, float] = {}
    item_map: Dict[str, _CatalogItem] = {}
    embed_score_map: Dict[str, float] = {}
    bm25_score_map: Dict[str, float] = {}

    for rank, (item, score) in enumerate(bm25_results):
        rrf_scores[item.artnr] = rrf_scores.get(item.artnr, 0) + bm25_weight / (k + rank + 1)
        item_map[item.artnr] = item
        bm25_score_map[item.artnr] = score

    for rank, (item, score) in enumerate(embed_results):
        rrf_scores[item.artnr] = rrf_scores.get(item.artnr, 0) + embed_weight / (k + rank + 1)
        item_map[item.artnr] = item
        embed_score_map[item.artnr] = score

    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for artnr, _rrf_score in sorted_items[:top_n]:
        item = item_map[artnr]
        embed_sc = embed_score_map.get(artnr, 0.0)
        bm25_sc = bm25_score_map.get(artnr, 0.0)
        combined = embed_sc * embed_weight + min(bm25_sc / 10.0, 1.0) * bm25_weight
        if prefer_own_brands:
            combined += _brand_bonus(item)
        # ALC-bonus (lavere ALC => litt høyere score)
        combined += _alc_bonus(item)
        results.append((item, combined))

    # Stabil sort: først score, deretter lav ALC
    results.sort(key=lambda x: (-x[1], _alc_value(x[0])))
    return results


# -----------------------------
# Char-ngram fallback
# -----------------------------
def _char_ngrams(s: str, n: int) -> List[str]:
    if not s:
        return []
    s = re.sub(r"\s+", " ", s.strip())
    if len(s) < n:
        return [s]
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def _ngram_set(s: str) -> set:
    s = _norm_text_for_sim(s)
    # fjerner veldig støyete tegn
    s = re.sub(r"[^a-z0-9æøå\s/+\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    grams = []
    for n in (3, 4, 5):
        grams.extend(_char_ngrams(s, n))
    return set(grams)


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union if union else 0.0


def _ngram_rerank(
    query_text: str,
    candidates: List[Tuple[_CatalogItem, float]],
    take_pool: int,
    out_n: int,
) -> List[Tuple[_CatalogItem, float]]:
    """
    Rerank en kandidatliste (typisk fra loose BM25) med char-ngram Jaccard.
    Dette er spesielt nyttig for sammensatte ord og bøyninger.
    """
    pool = candidates[: max(1, take_pool)]
    qset = _ngram_set(query_text)
    scored: List[Tuple[_CatalogItem, float]] = []

    for it, _sc in pool:
        tset = _ngram_set(it.text_norm or it.text)
        j = _jaccard(qset, tset)
        scored.append((it, float(j)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(1, out_n)]


# -----------------------------
# Claude verifier (TOP3-only for halv kost)
# -----------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}\s*$", re.S)


def _safe_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


def _claude_choose_top3(query_text: str, candidates: List[Dict[str, str]], cancel_event=None, cancel_cb=None) -> Dict[str, Any]:
    """
    Returnerer kun TOP 3 for å kutte output-tokens kraftig.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY mangler")

    _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)

    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=CLAUDE_TIMEOUT_SECONDS, max_retries=2)
    payload = [
        {"id": i, "artnr": c.get("artnr", ""), "text": c.get("text", "")}
        for i, c in enumerate(candidates, start=1)
    ]

    system = """Du er en presis medisinsk-produktmatcher for en norsk helse-innkjøpsportal.
Din oppgave er å velge beste match blant kandidatprodukter fra katalogen for en innkjøpsforespørsel.
RETURNER KUN gyldig JSON, ingen annen tekst."""

    user = f"""Vurder om kandidatproduktene matcher forespørselen nedenfor.

VIKTIG KONTEKST:
- "Produktnavn" / "Kategori" er ofte en GENERISK kategori.
  Den faktiske produktbeskrivelsen er i "Beskrivelse" og "Spesifikasjon".
- Du MÅ matche på faktisk produkttype, bruksområde og spesifikasjoner.

Returner JSON eksakt slik:
{{
  "query_product_type": "kort beskrivelse av hva forespørselen faktisk ber om",
  "reject_all": false,
  "top": [
    {{"id": 12, "artnr": "...", "score": 0, "reason": "maks 12-15 ord"}},
    {{"id": 7,  "artnr": "...", "score": 0, "reason": "maks 12-15 ord"}},
    {{"id": 3,  "artnr": "...", "score": 0, "reason": "maks 12-15 ord"}}
  ]
}}

REGLER:
- "top" skal inneholde maks 3 kandidater sortert best først.
- Hvis ingen kandidater er relevante: reject_all=true og top=[].
- score 0-100:
  90-100 nesten eksakt match, 70-89 god match, 50-69 akseptabel, 1-49 dårlig.
- Sett reject_all=true hvis alle kandidater er feil produkttype.
- reason skal være veldig kort (maks 12-15 ord).

Forespørsel:
{query_text}

Kandidater:
{json.dumps(payload, ensure_ascii=False)}""".strip()

    _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=900,  # lavere output: top3 only
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    out_text = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            out_text += block.text

    return _safe_json(out_text)


# -----------------------------
# Kvalitetsvurdering
# -----------------------------
def _assess_quality(embed_score: float, bm25_score: float, claude_score: Optional[int]) -> str:
    if claude_score is not None:
        if claude_score >= 80:
            return "Høy"
        elif claude_score >= 50:
            return "Medium"
        elif claude_score >= 1:
            return "Lav"
        else:
            return "Ingen"

    if embed_score >= 0.65:
        return "Høy"
    elif embed_score >= 0.50:
        return "Medium"
    elif embed_score >= EMBED_MIN_SCORE:
        return "Lav"
    return "Ingen"


# -----------------------------
# Katalogfelt -> output
# -----------------------------
def _fill_catalog_fields(out_row: Dict[str, Any], best: Optional[_CatalogItem]) -> None:
    # Init
    out_row["Katalog: Artikkelnummer"] = ""
    out_row["Katalog: Specification"] = ""
    out_row["Katalog: Item Description"] = ""
    out_row["Katalog: Producer Name"] = ""
    out_row["Katalog: Producer Item Number"] = ""
    out_row["Katalog: Storage instruction"] = ""
    out_row["Katalog: Item group"] = ""
    out_row["Katalog: Web Title"] = ""
    out_row["Katalog: Web Text"] = ""
    out_row["Katalog: ALC"] = ""

    if not best:
        return

    cat = best.row
    out_row["Katalog: Artikkelnummer"] = _norm(cat.get("Katalog: Artikkelnummer", ""))
    out_row["Katalog: Specification"] = _norm(cat.get("Katalog: Specification", cat.get("Specification", "")))
    out_row["Katalog: Item Description"] = _norm(cat.get("Katalog: Item Description", cat.get("Item Description", "")))
    out_row["Katalog: Producer Name"] = _norm(cat.get("Katalog: Producer Name", cat.get("Producer Name", "")))
    out_row["Katalog: Producer Item Number"] = _norm(
        cat.get("Katalog: Producer Item Number", cat.get("Producer Item Number", cat.get("Procuser Item Number", "")))
    )
    out_row["Katalog: Storage instruction"] = _norm(cat.get("Katalog: Storage instruction", cat.get("Storage instruction", "")))
    out_row["Katalog: Item group"] = _norm(cat.get("Katalog: Item group", cat.get("Item group", cat.get("Item Group", ""))))
    out_row["Katalog: Web Title"] = _norm(cat.get("Katalog: Web Title", cat.get("Web Title", "")))
    out_row["Katalog: Web Text"] = _norm(cat.get("Katalog: Web Text", cat.get("Web Text", "")))
    out_row["Katalog: ALC"] = _norm(cat.get("Katalog: ALC", cat.get("ALC", cat.get("alc", cat.get("Alc", "")))))



def _load_catalog_excel(path: str, sheet_name: str | int | None = None, **_kwargs) -> List[_CatalogItem]:
    """Les katalog fra Excel og bygg items.
    - sheet_name: valgfritt; hvis None leses første ark.
    - **_kwargs: ignoreres for bakoverkompatibilitet.
    """
    # Pandas: sheet_name=None => dict av ark; vi vil ha første ark i så fall
    try:
        cat_obj = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    except TypeError:
        # engine kan feile i noen miljø; prøv uten
        cat_obj = pd.read_excel(path, sheet_name=sheet_name)

    if isinstance(cat_obj, dict):
        # velg første ark hvis sheet_name=None
        if not cat_obj:
            raise ValueError("Katalogfila inneholder ingen ark.")
        cat = next(iter(cat_obj.values()))
    else:
        cat = cat_obj

    if cat is None or getattr(cat, "empty", False):
        raise ValueError("Katalogfila ga 0 rader.")

    # Bygg normert kolonnenavn-mapping
    cols = list(cat.columns)
    norm_map = {_norm_key(str(c)): str(c) for c in cols}

    def pick_col(candidates: List[str]) -> str | None:
        for cand in candidates:
            key = _norm_key(cand)
            if key in norm_map:
                return norm_map[key]
        return None

    # Finn artikkelnummer-kolonne robust
    art_col = pick_col([
        "Katalog: Artikkelnummer", "Artikkelnummer", "Art.nr", "Art nr", "Artikkel nr",
        "Varenr", "Varenummer", "Produktnr", "Produkt nr", "Item NO", "Item No", "ItemNo",
        "Article Number", "SKU", "SkU", "Item number"
    ])
    if not art_col:
        # fallback: prøv å finne noe som inneholder art/varenr/itemno
        for c in cols:
            nk = _norm_key(str(c))
            if any(tok in nk for tok in ["art", "varenr", "varenummer", "item no", "itemno", "sku", "article number", "produktnr"]):
                art_col = str(c)
                break

    if not art_col:
        raise ValueError("Fant ikke kolonne for artikkelnummer (Art.nr / Artikkelnummer / Varenr / Item No).")

    # Standardiser navn (uten å ødelegge eksisterende 'Katalog:'-kolonner)
    rename_map: Dict[str, str] = {}
    if art_col != "Katalog: Artikkelnummer" and "Katalog: Artikkelnummer" not in cat.columns:
        rename_map[art_col] = "Katalog: Artikkelnummer"

    def map_if_exists(src_candidates: List[str], dst: str):
        if dst in cat.columns:
            return
        c = pick_col([dst] + src_candidates)
        if c and c in cat.columns:
            rename_map[c] = dst

    map_if_exists(["Item Description", "Beskrivelse", "Description", "Varetekst"], "Katalog: Item Description")
    map_if_exists(["Specification", "Spesifikasjon", "Spec", "Produktspesifikasjon"], "Katalog: Specification")
    map_if_exists(["Producer Name", "Produsent", "Leverandør", "Brand", "Merke"], "Katalog: Producer Name")
    map_if_exists(["Producer Item Number", "Produsentnr", "Produsent nr", "Mpn", "MPN"], "Katalog: Producer Item Number")
    map_if_exists(["Storage instruction", "Storage Instruction", "Lagring", "Oppbevaring"], "Katalog: Storage instruction")
    map_if_exists(["Item group", "Item Group", "Produktgruppe", "Varegruppe"], "Katalog: Item group")
    map_if_exists(["Web Title", "Web title", "Tittel"], "Katalog: Web Title")
    map_if_exists(["Web Text", "Web text", "Webtekst"], "Katalog: Web Text")
    map_if_exists(["ALC", "alc", "Alc"], "Katalog: ALC")

    if rename_map:
        cat = cat.rename(columns=rename_map)

    items: List[_CatalogItem] = []
    for _, r in cat.iterrows():
        artnr_raw = r.get("Katalog: Artikkelnummer", "")
        artnr = _norm(artnr_raw)

        # håndter excel-tall som 123.0
        if isinstance(artnr_raw, (int, float)) and not pd.isna(artnr_raw):
            try:
                artnr_int = int(float(artnr_raw))
                artnr = str(artnr_int)
            except Exception:
                pass

        if not artnr or str(artnr).lower() == "nan":
            continue

        row_dict = {k: ("" if pd.isna(r.get(k)) else r.get(k)) for k in cat.columns}
        # sørg for at artnr ligger i row også
        row_dict["Katalog: Artikkelnummer"] = artnr

        text = _build_candidate_text(row_dict)
        toks = _tokenize(text)
        it = _CatalogItem(artnr=artnr, row=row_dict, text=text, toks=toks)
        it.text_norm = _norm_text_for_sim(text)
        items.append(it)

    if not items:
        raise ValueError("Katalogfila ga 0 gyldige produkter.")
    return items
def _near_identical_gate(
    row: Dict[str, Any],
    top_item: _CatalogItem,
    best_embed: float,
    second_embed: float,
) -> bool:
    if not NEAR_IDENTICAL_GATE:
        return False
    if best_embed < EXACT_MATCH_MIN_EMBED:
        return False
    if (best_embed - second_embed) < EXACT_MATCH_MIN_MARGIN:
        return False

    q = _norm_text_for_sim(_build_query_text_for_claude(row))
    c = _norm_text_for_sim(top_item.text)
    if len(q) < 20 or len(c) < 20:
        return False
    ratio = SequenceMatcher(None, q, c).ratio()
    return ratio >= EXACT_MATCH_SEQ_RATIO


def _match_one_row(
    row: Dict[str, Any],
    bm25_index: _BM25Index,
    embed_index: Optional[_EmbeddingIndex],
    top_n: int,
    prefer_own_brands: bool = True,
    cancel_event=None,
    cancel_cb=None,
) -> Tuple[str, str, Optional[_CatalogItem], str]:
    _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)

    query_text = _build_query_text(row)
    if not query_text:
        return "", "", None, "Ingen"

    # 1) normal retrieval
    scored = _hybrid_retrieve(query_text, bm25_index, embed_index, top_n=top_n, prefer_own_brands=prefer_own_brands)

    # 2) "svak retrieval" -> bygg pool fra loose BM25
    weak_retrieval = (not scored) or (len(scored) < min(10, top_n))

    loose_pool: List[Tuple[_CatalogItem, float]] = []
    if weak_retrieval:
        loose_pool = bm25_index.top_n_loose(query_text, n=max(CLAUDE_FALLBACK_BM25_K, NGRAM_FALLBACK_POOL, top_n))

        # 2b) char-ngram rerank pool -> bedre kandidater ved komposita/bøyninger
        if loose_pool:
            ngram_ranked = _ngram_rerank(
                query_text=query_text,
                candidates=loose_pool,
                take_pool=min(len(loose_pool), NGRAM_FALLBACK_POOL),
                out_n=min(len(loose_pool), NGRAM_TOP_N),
            )
            # Hvis ngram faktisk gir mening, bruk den som "scored"
            if ngram_ranked and ngram_ranked[0][1] >= NGRAM_MIN_JACCARD:
                # map til score-struktur (item, score)
                scored = [(it, sc) for (it, sc) in ngram_ranked][: max(top_n, 30)]
            else:
                # fallback: behold loose pool som scored
                scored = [(it, sc) for (it, sc) in loose_pool][: max(CLAUDE_FALLBACK_BM25_K, top_n)]

    if not scored:
        return "", "", None, "Ingen"

    # Stabil sort: score først, så lav ALC
    scored.sort(key=lambda x: (-x[1], _alc_value(x[0])))

    # Embedding score for kvalitet (ikke blokkér AI-first)
    best_embed_score = 0.0
    second_embed_score = 0.0
    if embed_index and embed_index.available:
        emb2 = embed_index.top_n(query_text, n=2)
        if emb2:
            best_embed_score = float(emb2[0][1])
        if len(emb2) > 1:
            second_embed_score = float(emb2[1][1])

    # Near-identical gate: skip Claude hvis vi er ekstremt sikre
    top_item = scored[0][0]
    if _near_identical_gate(row, top_item, best_embed_score, second_embed_score):
        alts = ", ".join([it.artnr for it, _ in scored[:3]])
        return top_item.artnr, alts, top_item, "Høy"

    # 3) Claude-verifisering (kan være dyrt). For å unngå timesvis med kjøring:
    # - default: verifiser kun når retrieval er "usikkert"
    # - sett CLAUDE_VERIFY_MODE=always for å tvinge verifisering på alt
    should_verify = False
    if ANTHROPIC_API_KEY and CLAUDE_ALWAYS_VERIFY:
        if CLAUDE_VERIFY_MODE == "always":
            should_verify = True
        else:
            # "uncertain": verifiser hvis embedding-score er lav, eller marginen er lav
            if (best_embed_score < CLAUDE_VERIFY_MIN_EMBED) or ((best_embed_score - second_embed_score) < CLAUDE_VERIFY_MIN_MARGIN):
                should_verify = True

    _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)

    if should_verify:
        # Send færre kandidater (billigere), men gi litt mer "luft" hvis retrieval var svak
        k = CLAUDE_TOP_K
        if weak_retrieval:
            k = min(max(CLAUDE_TOP_K, 25) + 5, 35)  # liten boost kun ved svak retrieval

        candidates_for_claude = min(k, len(scored))
        candidate_items = [it for it, _ in scored[:candidates_for_claude]]
        claude_candidates = [{"artnr": it.artnr, "text": it.text} for it in candidate_items]

        try:
            claude_query = _build_query_text_for_claude(row)
            resp = _claude_choose_top3(query_text=claude_query, candidates=claude_candidates, cancel_event=cancel_event, cancel_cb=cancel_cb)

            _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)

            reject_all = bool(resp.get("reject_all", False)) if isinstance(resp, dict) else False
            top = resp.get("top", []) if isinstance(resp, dict) else []

            if reject_all or not isinstance(top, list) or not top:
                return "", "", None, "Ingen"

            # Velg beste
            best = top[0] if isinstance(top[0], dict) else None
            if not best:
                return "", "", None, "Ingen"

            best_id = int(best.get("id", 0) or 0)
            best_score = int(best.get("score", 0) or 0)

            if best_score < CLAUDE_MIN_ACCEPT_SCORE:
                return "", "", None, "Ingen"

            if not (1 <= best_id <= len(candidate_items)):
                return "", "", None, "Ingen"

            best_item = candidate_items[best_id - 1]
            alts = ", ".join([str(x.get("artnr", "")).strip() for x in top if isinstance(x, dict) and x.get("artnr")])[:200]
            quality = _assess_quality(best_embed_score, 0.0, best_score)
            return best_item.artnr, alts, best_item, quality

        except Exception as e:
            logger.warning(f"Claude-verifisering feilet (fallback til retrieval): {e}")

    # 4) Hvis Claude ikke er tilgjengelig/feiler -> fall tilbake til retrieval med terskler
    top_item, top_score = scored[0]

    if embed_index and embed_index.available:
        if best_embed_score < EMBED_MIN_SCORE:
            return "", "", None, "Ingen"
    else:
        bm1 = bm25_index.top_n(query_text, n=1)
        if bm1 and bm1[0][1] < BM25_MIN_SCORE:
            return "", "", None, "Ingen"

    alts = ", ".join([it.artnr for it, _ in scored[:3]])
    quality = _assess_quality(best_embed_score, float(top_score), None)
    return top_item.artnr, alts, top_item, quality


class Catalog:
    """
    app.py-kompatibel klasse:
      from matcher import Catalog
      catalog = Catalog.from_excel("catalog.xlsx")
      df_out = catalog.apply_to_dataframe(df_in, top_n=30, progress_cb=...)
    """

    def __init__(self, items: List[_CatalogItem], use_embeddings: bool = True) -> None:
        self.items = items
        self.bm25_index = _BM25Index(items)
        self.embed_index: Optional[_EmbeddingIndex] = None

        if use_embeddings:
            try:
                self.embed_index = _EmbeddingIndex(items)
                if not self.embed_index.available:
                    logger.warning("Embedding-indeks ikke tilgjengelig, bruker kun BM25")
                    self.embed_index = None
            except Exception as e:
                logger.warning(f"Kunne ikke laste embedding-modell: {e}")
                self.embed_index = None

    @classmethod
    def from_excel(cls, catalog_path: str, use_embeddings: bool = True, sheet_name: str | int | None = None) -> "Catalog":
        try:
            items = _load_catalog_excel(catalog_path, sheet_name=sheet_name)
        except TypeError:
            # bakoverkompatibilitet hvis loader ikke støtter sheet_name
            items = _load_catalog_excel(catalog_path)
        return cls(items, use_embeddings=use_embeddings)

    def match_row(self, row: Dict[str, Any], top_n: int = 30, prefer_own_brands: bool = True) -> Tuple[str, str, Optional[Dict[str, Any]], str]:
        artnr, alts, best_item, quality = _match_one_row(
            row=row,
            bm25_index=self.bm25_index,
            embed_index=self.embed_index,
            top_n=top_n,
            prefer_own_brands=prefer_own_brands,
        )
        return artnr, alts, (best_item.row if best_item else None), quality

    def apply_to_dataframe(
        self,
        df: pd.DataFrame,
        top_n: int = 30,
        progress_cb: Optional[Any] = None,
        prefer_own_brands: bool = True,
        cancel_event=None,
        cancel_cb=None,
    ) -> pd.DataFrame:
        for col in OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        df_work = df.reindex(columns=OUTPUT_COLUMNS, fill_value="").copy()
        total = len(df_work)

        out_rows: List[Dict[str, Any]] = []
        for i, (_, r) in enumerate(df_work.iterrows()):
            out_row = r.to_dict()
            artnr, alts, best_item, quality = _match_one_row(
                out_row,
                self.bm25_index,
                self.embed_index,
                top_n,
                prefer_own_brands=prefer_own_brands,
                cancel_event=cancel_event,
                cancel_cb=cancel_cb,
            )
            out_row["Matchet art.nr"] = artnr
            out_row["Match-kvalitet"] = quality
            out_row["Alternativer (topp 3 art.nr)"] = alts
            _fill_catalog_fields(out_row, best_item)
            out_rows.append(out_row)

            if progress_cb and total > 0:
                try:
                    progress_cb(0.05 + 0.85 * ((i + 1) / total))
                except Exception:
                    pass

        return pd.DataFrame(out_rows).reindex(columns=OUTPUT_COLUMNS)


# ------------------------------------------------------------
# FIX: app.py importerer noen ganger _match_rows_batched fra matcher
# ------------------------------------------------------------
def _match_rows_batched(*args, **kwargs):
    """
    Kompatibilitets-wrapper.
    Noen app.py-versjoner forventer _match_rows_batched i matcher.py.

    Støtter typiske kall:
      _match_rows_batched(cat, df_work, top_n=30, progress_cb=None)
      _match_rows_batched(df_work, bm25_index, embed_index, top_n=30, progress_cb=None)

    Returnerer: (df_out, meta)
    """
    progress_cb = kwargs.get("progress_cb")
    cancel_cb = kwargs.get("cancel_cb")
    cancel_event = kwargs.get("cancel_event")
    top_n = int(kwargs.get("top_n", 30))
    prefer_own_brands = bool(kwargs.get("prefer_own_brands", PREFER_OWN_BRANDS_DEFAULT))

    cat = None
    df_work = None
    bm25_index = None
    embed_index = None

    if len(args) >= 2 and hasattr(args[0], "bm25_index") and isinstance(args[1], pd.DataFrame):
        cat = args[0]
        df_work = args[1]
        bm25_index = cat.bm25_index
        embed_index = cat.embed_index

    elif len(args) >= 3 and isinstance(args[0], pd.DataFrame):
        df_work = args[0]
        bm25_index = args[1]
        embed_index = args[2]

    else:
        raise TypeError(
            "Ukjent signatur for _match_rows_batched. "
            "Forventet (cat, df_work, ...) eller (df_work, bm25_index, embed_index, ...)."
        )

    total_rows = len(df_work)
    out_rows = []

    for i, (_, r) in enumerate(df_work.iterrows()):
        _check_cancel(cancel_event=cancel_event, cancel_cb=cancel_cb)
        out_row = r.to_dict()
        artnr, alts, best_item, quality = _match_one_row(
            out_row,
            bm25_index,
            embed_index,
            top_n=top_n,
            prefer_own_brands=prefer_own_brands,
            cancel_event=cancel_event,
            cancel_cb=cancel_cb,
        )
        out_row["Matchet art.nr"] = artnr
        out_row["Match-kvalitet"] = quality
        out_row["Alternativer (topp 3 art.nr)"] = alts
        _fill_catalog_fields(out_row, best_item)
        out_rows.append(out_row)

        if progress_cb and total_rows > 0:
            try:
                progress_cb((i + 1) / total_rows)
            except Exception:
                pass

    df_out = pd.DataFrame(out_rows).reindex(columns=OUTPUT_COLUMNS)

    meta = {
        "rows_in": int(total_rows),
        "rows_out": int(len(df_out)),
        "claude_enabled": bool(ANTHROPIC_API_KEY),
        "embedding_enabled": bool(embed_index is not None and getattr(embed_index, "available", False)),
        "ai_first": bool(ANTHROPIC_API_KEY) and CLAUDE_ALWAYS_VERIFY,
        "claude_top_k": int(CLAUDE_TOP_K),
        "claude_fallback_bm25_k": int(CLAUDE_FALLBACK_BM25_K),
        "claude_min_accept_score": int(CLAUDE_MIN_ACCEPT_SCORE),
        "ngram_pool": int(NGRAM_FALLBACK_POOL),
        "ngram_top_n": int(NGRAM_TOP_N),
        "near_identical_gate": bool(NEAR_IDENTICAL_GATE),
    }

    return df_out, meta