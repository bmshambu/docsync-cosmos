"""RFP question extraction (Tab 4 — RFP upload).

Turns an uploaded client RFP (PDF/DOCX) into the SAME parsed structure the CSV
parser produces — ``{fieldnames, rows, question_column, count}`` — so the whole
existing answer pipeline (/run → answer_questions → /download) is reused
unchanged. The RFP itself is NEVER ingested into the answer corpus.

Three-stage pipeline (ported from the proven office extractor, which aligns far
better with hand-curated golden sets than a single-pass prompt):

    1. EXTRACT     candidate biddable questions, windowed over the RFP text
       (dedup #1)
    2. DECOMPOSE   compound asks into standalone, separately-answerable parts
       (dedup #2 — splits can re-collide)
    3. FILTER      each question SEND / SUPPRESS — SUPPRESS = administrative,
                   form/attachment, pointer-only, not answerable from the
                   response library. Suppressed questions are KEPT (for
                   auditability) but flagged so the answer stage skips the LLM
                   call and records a FILTERED status.

Query-time decomposition (M3) still runs when these questions are answered, but
it no-ops here since they're already standalone — no double-splitting.
"""

from __future__ import annotations

import asyncio
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.extractor import _parse_json_object
from app.llm.prompts import (
    build_rfp_decompose_prompt,
    build_rfp_extract_prompt,
    build_rfp_filter_prompt,
)
from app.services.extract import extract_paragraphs

SUPPORTED_RFP_EXTS = (".pdf", ".docx", ".doc")

# One extraction call per window of RFP text. ~6k chars keeps each call small;
# a small overlap keeps a parent question together with its sub-bullets when
# they straddle a window boundary.
_WINDOW_CHARS = 6000
_WINDOW_OVERLAP_CHARS = 600
_MAX_WINDOWS = 40          # hard cap so a huge RFP can't spawn unbounded calls
# Decompose / filter run over the deduped question LIST in batches so one call
# stays small and reliable regardless of how many questions were extracted.
_LIST_BATCH = 40


def _windows(paragraphs: list[dict]) -> list[str]:
    """Group extracted paragraphs into ~_WINDOW_CHARS windows, with a small
    trailing overlap carried into the next window."""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        buf.append(t)
        size += len(t) + 1
        if size >= _WINDOW_CHARS:
            out.append("\n".join(buf))
            # carry the tail into the next window so a parent + sub-bullets that
            # straddle the cut are still seen together at least once
            tail, tail_len = [], 0
            for line in reversed(buf):
                if tail_len >= _WINDOW_OVERLAP_CHARS:
                    break
                tail.insert(0, line)
                tail_len += len(line) + 1
            buf, size = tail[:], tail_len
    if buf and "\n".join(buf) not in out:
        out.append("\n".join(buf))
    return out[:_MAX_WINDOWS]


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q or "").strip().lower()


def _dedup(questions: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in questions:
        k = _norm(q)
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out


def _numbered(questions: list[str]) -> str:
    return "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))


def _batches(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── Stage 1: extract ─────────────────────────────────────────────────────────

async def _extract_window(text: str, settings: Settings, sem: asyncio.Semaphore) -> list[str]:
    system, user = build_rfp_extract_prompt(text)
    chat = get_chat(settings.model_extract, temperature=0.0,
                    max_tokens=settings.max_extract_tokens, json_mode=True)
    async with sem:
        resp = await chat.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = _parse_json_object(raw) or {}
    qs = data.get("questions") or []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()]


async def _extract_all(paragraphs: list[dict], settings: Settings) -> list[str]:
    windows = _windows(paragraphs)
    if not windows:
        raise ValueError("No readable text found in the document (is it a scanned PDF?).")
    sem = asyncio.Semaphore(settings.max_llm_concurrency)
    results = await asyncio.gather(
        *[_extract_window(w, settings, sem) for w in windows],
        return_exceptions=True,
    )
    flat: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue                       # a bad window shouldn't sink the file
        flat.extend(r)
    return _dedup(flat)


# ── Stage 2: decompose ───────────────────────────────────────────────────────

async def _decompose_batch(questions: list[str], settings: Settings,
                           sem: asyncio.Semaphore) -> list[str]:
    system, user = build_rfp_decompose_prompt(_numbered(questions))
    chat = get_chat(settings.model_extract, temperature=0.0,
                    max_tokens=settings.max_extract_tokens, json_mode=True)
    async with sem:
        resp = await chat.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = _parse_json_object(raw) or {}
    items = data.get("items")
    if not isinstance(items, list):
        return questions                    # unparseable → keep the batch as-is
    out: list[str] = []
    for it in items:
        parts = it.get("questions") if isinstance(it, dict) else None
        if isinstance(parts, list):
            out.extend(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return out or questions


async def _decompose_all(questions: list[str], settings: Settings) -> list[str]:
    if not questions:
        return questions
    sem = asyncio.Semaphore(settings.max_llm_concurrency)
    results = await asyncio.gather(
        *[_decompose_batch(b, settings, sem) for b in _batches(questions, _LIST_BATCH)],
        return_exceptions=True,
    )
    flat: list[str] = []
    for r, b in zip(results, _batches(questions, _LIST_BATCH)):
        flat.extend(b if isinstance(r, Exception) else r)   # bad batch → keep original
    return _dedup(flat)


# ── Stage 3: answerability filter ────────────────────────────────────────────

async def _filter_batch(questions: list[str], settings: Settings,
                        sem: asyncio.Semaphore) -> list[bool]:
    """Return a SEND(True)/SUPPRESS(False) flag per question in the batch."""
    system, user = build_rfp_filter_prompt(_numbered(questions))
    chat = get_chat(settings.model_extract, temperature=0.0,
                    max_tokens=settings.max_extract_tokens, json_mode=True)
    async with sem:
        resp = await chat.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = _parse_json_object(raw) or {}
    items = data.get("items")
    flags = [True] * len(questions)         # default SEND (fail-open on parse error)
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            idx = it.get("index")
            decision = str(it.get("decision") or "").strip().upper()
            if isinstance(idx, int) and 1 <= idx <= len(questions):
                flags[idx - 1] = decision != "SUPPRESS"
    return flags


async def _filter_all(questions: list[str], settings: Settings) -> list[bool]:
    if not questions:
        return []
    sem = asyncio.Semaphore(settings.max_llm_concurrency)
    batches = _batches(questions, _LIST_BATCH)
    results = await asyncio.gather(
        *[_filter_batch(b, settings, sem) for b in batches],
        return_exceptions=True,
    )
    flags: list[bool] = []
    for r, b in zip(results, batches):
        flags.extend([True] * len(b) if isinstance(r, Exception) else r)
    return flags


# ── Public API ───────────────────────────────────────────────────────────────

FILTERED_STATUS = "FILTERED — administrative / not answerable from the library"


def to_parsed(items: list[dict]) -> dict:
    """Wrap [{'question','answerable'}] in the CSV-parser's shape. Rows carry a
    private ``_answerable`` flag the answer stage reads (stripped from output —
    it is not in fieldnames or OUTPUT_COLUMNS). CSV uploads have no such flag,
    so every CSV question is answered as before."""
    rows = [{"Question": it["question"], "_answerable": it.get("answerable", True)}
            for it in items]
    return {
        "fieldnames": ["Question"],
        "rows": rows,
        "question_column": "Question",
        "count": len(rows),
    }


async def extract_rfp_to_parsed(
    filename: str,
    data: bytes,
    settings: Settings,
    max_questions: int = 500,
) -> dict:
    """Full 3-stage extraction → parsed structure with per-row answerability.

    The returned dict adds ``answerable_count`` / ``filtered_count`` alongside
    the standard parsed keys so the upload preview can show the breakdown."""
    paragraphs = extract_paragraphs(filename, data)
    if paragraphs is None:
        raise ValueError(f"Unsupported file type for '{filename}'. Upload a PDF or DOCX RFP.")

    extracted = await _extract_all(paragraphs, settings)
    decomposed = (await _decompose_all(extracted, settings))[:max_questions]
    if not decomposed:
        raise ValueError(
            "No bidder questions were found in this document. If it is a scanned "
            "PDF, OCR it first; otherwise check it is the RFP / questionnaire."
        )

    flags = await _filter_all(decomposed, settings)
    items = [{"question": q, "answerable": bool(a)}
             for q, a in zip(decomposed, flags)]

    parsed = to_parsed(items)
    parsed["answerable_count"] = sum(1 for it in items if it["answerable"])
    parsed["filtered_count"] = len(items) - parsed["answerable_count"]
    return parsed
