"""Plagiarism Checker — Commons guild tool.

Open-source replacement for Turnitin / Grammarly Plagiarism. Splits the
input into n-gram phrases, queries DuckDuckGo for each phrase, and
reports the percentage of phrases that appear verbatim in indexed pages.

Auth: service-key + X-Agent-Id (same as the rest of `/commons/...`). The
public `/health` endpoint is unauthenticated so the Commons landing page
can probe availability.

SC cost: check = 8, check-doc = 10. Both audited to `tool_calls`.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/commons/plagiarism", tags=["commons-plagiarism"])


SC_COST = {
    "check": float(os.environ.get("PLAGIARISM_SC_CHECK", "8")),
    "check_doc": float(os.environ.get("PLAGIARISM_SC_CHECK_DOC", "10")),
}

MAX_UPLOAD_BYTES = int(os.environ.get("PLAGIARISM_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_TEXT_CHARS = int(os.environ.get("PLAGIARISM_MAX_TEXT_CHARS", str(50_000)))
SEARCH_RATE_PER_SEC = float(os.environ.get("PLAGIARISM_SEARCH_RPS", "3"))
CACHE_TTL_SEC = int(os.environ.get("PLAGIARISM_CACHE_TTL", "1800"))

# Sensitivity → (n-gram size, max phrases to query). Lower n-grams + more
# queries = stricter detection but more network.
SENSITIVITY = {
    "low":  {"ngram": 10, "max_phrases": 8},
    "med":  {"ngram": 7,  "max_phrases": 14},
    "high": {"ngram": 5,  "max_phrases": 24},
}


# ── Rate limiter (token bucket / sliding window) ───────────────────────────

class _RateLimiter:
    """Thread-safe sliding-window limiter. Caps to N events / second."""

    def __init__(self, rate_per_sec: float):
        self.rate = rate_per_sec
        self.window = 1.0
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= self.window:
                    self._events.popleft()
                if len(self._events) < self.rate:
                    self._events.append(now)
                    return
                wait = self.window - (now - self._events[0])
            time.sleep(max(wait, 0.01))


_search_limiter = _RateLimiter(SEARCH_RATE_PER_SEC)


# ── Cache (30 min TTL) ─────────────────────────────────────────────────────

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> list[dict] | None:
    with _cache_lock:
        ent = _cache.get(key)
        if ent is None:
            return None
        ts, val = ent
        if time.time() - ts > CACHE_TTL_SEC:
            _cache.pop(key, None)
            return None
        return val


def _cache_put(key: str, val: list[dict]) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val)
        if len(_cache) > 4096:
            # Prune oldest 25% by timestamp
            keep = sorted(_cache.items(), key=lambda kv: kv[1][0])[len(_cache) // 4:]
            _cache.clear()
            _cache.update(dict(keep))


# ── Text helpers ───────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _tokenize(text_in: str) -> list[str]:
    return _WORD_RE.findall(text_in.lower())


def _ngrams(tokens: list[str], n: int) -> list[str]:
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _sample_phrases(phrases: list[str], k: int) -> list[str]:
    """Pick up to k phrases, evenly spaced and de-duped, preserving order."""
    if not phrases:
        return []
    seen: set[str] = set()
    uniq: list[str] = []
    for p in phrases:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    if len(uniq) <= k:
        return uniq
    step = len(uniq) / k
    out: list[str] = []
    for i in range(k):
        out.append(uniq[int(i * step)])
    return out


# ── Capability probe ───────────────────────────────────────────────────────

def _capabilities() -> dict[str, bool]:
    caps = {"search": False, "pdf": False, "docx": False}
    try:
        import ddgs  # noqa: F401
        caps["search"] = True
    except ImportError:
        try:
            import duckduckgo_search  # noqa: F401
            caps["search"] = True
        except ImportError:
            pass
    try:
        import pypdf  # noqa: F401
        caps["pdf"] = True
    except ImportError:
        pass
    try:
        import docx  # noqa: F401
        caps["docx"] = True
    except ImportError:
        pass
    return caps


def _ddgs_search(phrase: str, max_results: int = 5) -> list[dict]:
    """Quoted phrase search via DDGS. Cached + rate-limited."""
    key = hashlib.sha256(phrase.encode()).hexdigest()
    hit = _cache_get(key)
    if hit is not None:
        return hit

    _search_limiter.acquire()
    try:
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        with DDGS() as d:
            results = list(d.text(f'"{phrase}"', max_results=max_results)) or []
    except Exception:
        results = []

    norm = [
        {"title": r.get("title", ""), "url": r.get("href", "") or r.get("url", ""),
         "body": r.get("body", "")}
        for r in results
    ]
    _cache_put(key, norm)
    return norm


# ── Audit + billing ────────────────────────────────────────────────────────

def _json_dump(d: dict) -> str:
    import json
    return json.dumps(d, default=str)


async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": _json_dump(input_summary),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    tool: str,
    ref: str,
) -> float:
    cost = SC_COST[tool]
    if cost <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-cost,
            reason=f"commons.plagiarism.{tool}",
            ref_id=ref,
            note=f"Plagiarism Checker: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": cost,
                "tool": f"commons.plagiarism.{tool}",
            },
        )
    return cost


# ── Core scoring ───────────────────────────────────────────────────────────

def _score(text_in: str, sensitivity: str) -> dict:
    cfg = SENSITIVITY[sensitivity]
    tokens = _tokenize(text_in)
    if len(tokens) < cfg["ngram"]:
        return {
            "match_pct": 0.0,
            "matched_phrases": [],
            "phrases_checked": 0,
            "phrases_total": 0,
            "tokens": len(tokens),
            "sensitivity": sensitivity,
            "ngram": cfg["ngram"],
            "note": "text_too_short",
        }

    all_grams = _ngrams(tokens, cfg["ngram"])
    sampled = _sample_phrases(all_grams, cfg["max_phrases"])

    matched: list[dict] = []
    for phrase in sampled:
        results = _ddgs_search(phrase, max_results=5)
        for r in results:
            body = (r.get("body", "") or "").lower()
            title = (r.get("title", "") or "").lower()
            if phrase in body or phrase in title:
                matched.append({"phrase": phrase, "source_url": r.get("url", "")})
                break

    pct = round(100.0 * len(matched) / len(sampled), 2) if sampled else 0.0
    return {
        "match_pct": pct,
        "matched_phrases": matched,
        "phrases_checked": len(sampled),
        "phrases_total": len(all_grams),
        "tokens": len(tokens),
        "sensitivity": sensitivity,
        "ngram": cfg["ngram"],
    }


# ── /health (public) ───────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict:
    caps = _capabilities()
    return {
        "service": "commons.plagiarism",
        "version": "1.0.0",
        "available": caps,
        "all_available": all(caps.values()),
        "sc_cost": SC_COST,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_text_chars": MAX_TEXT_CHARS,
        "search_rps": SEARCH_RATE_PER_SEC,
        "cache_ttl_sec": CACHE_TTL_SEC,
        "sensitivities": list(SENSITIVITY.keys()),
    }


# ── /check ─────────────────────────────────────────────────────────────────

class CheckIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)
    sensitivity: str = Field("med", pattern="^(low|med|high)$")


@router.post("/check")
async def check_text(
    body: CheckIn,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    if not _capabilities()["search"]:
        raise HTTPException(status_code=503, detail="search_unavailable")

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="check", ref=ref)

    try:
        result = _score(body.text, body.sensitivity)
        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.plagiarism.check",
            input_summary={
                "text_len": len(body.text),
                "sensitivity": body.sensitivity,
                "phrases_checked": result["phrases_checked"],
                "match_pct": result["match_pct"],
            },
            output_len=len(result["matched_phrases"]),
            sc_cost=SC_COST["check"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return {**result, "sc_cost": SC_COST["check"], "ref": ref}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "check_failed", "msg": str(e)[:200]})


# ── /check-doc ─────────────────────────────────────────────────────────────

async def _read_upload(f: UploadFile, *, label: str) -> bytes:
    data = await f.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{label}_empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "upload_too_large", "limit_bytes": MAX_UPLOAD_BYTES,
                    "got_bytes": len(data), "field": label},
        )
    return data


def _extract_text(name: str, data: bytes) -> str:
    n = (name or "").lower()
    if n.endswith(".txt"):
        try:
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": "bad_txt", "msg": str(e)[:200]})
    if n.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": "bad_pdf", "msg": str(e)[:200]})
    if n.endswith(".docx"):
        try:
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception as e:
            raise HTTPException(status_code=400, detail={"error": "bad_docx", "msg": str(e)[:200]})
    raise HTTPException(
        status_code=400,
        detail={"error": "unsupported_format", "msg": "use .txt, .pdf, or .docx", "filename": name},
    )


@router.post("/check-doc")
async def check_doc(
    file: Annotated[UploadFile, File(description="txt / pdf / docx (≤5 MB)")],
    sensitivity: str = "med",
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    if sensitivity not in SENSITIVITY:
        raise HTTPException(status_code=400, detail="bad_sensitivity")
    if not _capabilities()["search"]:
        raise HTTPException(status_code=503, detail="search_unavailable")

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="check_doc", ref=ref)

    data = await _read_upload(file, label="file")
    try:
        extracted = _extract_text(file.filename or "", data)
        if len(extracted) > MAX_TEXT_CHARS:
            extracted = extracted[:MAX_TEXT_CHARS]
        if not extracted.strip():
            raise HTTPException(status_code=400, detail="no_text_extracted")

        result = _score(extracted, sensitivity)
        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.plagiarism.check_doc",
            input_summary={
                "filename": file.filename,
                "in_bytes": len(data),
                "text_len": len(extracted),
                "sensitivity": sensitivity,
                "phrases_checked": result["phrases_checked"],
                "match_pct": result["match_pct"],
            },
            output_len=len(result["matched_phrases"]),
            sc_cost=SC_COST["check_doc"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return {**result, "sc_cost": SC_COST["check_doc"], "ref": ref,
                "filename": file.filename, "extracted_chars": len(extracted)}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "check_doc_failed", "msg": str(e)[:200]})
