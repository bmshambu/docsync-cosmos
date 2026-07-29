"""RFP question extraction (Tab 4 — RFP upload).

Users can now hand the app a client RFP (PDF/DOCX) instead of a pre-made
questions CSV. This module turns the RFP into the SAME parsed structure the CSV
parser produces — ``{fieldnames, rows, question_column, count}`` — so the whole
existing answer pipeline (/run → answer_questions → /download) is reused
unchanged.

The RFP itself is NEVER ingested into the answer corpus; it is only read to pull
out the bidder's questions.
"""

from __future__ import annotations

import asyncio
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.extractor import _parse_json_object
from app.llm.prompts import build_rfp_questions_prompt
from app.services.extract import extract_paragraphs

SUPPORTED_RFP_EXTS = (".pdf", ".docx", ".doc")

# One LLM call per window of RFP text. ~6k chars keeps each call small and fast
# while giving the model enough context to spot multi-line questions.
_WINDOW_CHARS = 6000
_MAX_WINDOWS = 40          # hard cap so a huge RFP can't spawn unbounded calls


def _windows(paragraphs: list[dict]) -> list[str]:
    """Group extracted paragraphs into ~_WINDOW_CHARS text windows."""
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
            buf, size = [], 0
    if buf:
        out.append("\n".join(buf))
    return out[:_MAX_WINDOWS]


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", q or "").strip().lower()


async def _extract_window(text: str, settings: Settings, semaphore: asyncio.Semaphore) -> list[str]:
    system, user = build_rfp_questions_prompt(text)
    chat = get_chat(settings.model_extract, temperature=0.0,
                    max_tokens=settings.max_extract_tokens, json_mode=True)
    async with semaphore:
        resp = await chat.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = _parse_json_object(raw) or {}
    qs = data.get("questions") or []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()]


async def extract_questions(
    filename: str,
    data: bytes,
    settings: Settings,
    max_questions: int = 500,
) -> list[str]:
    """Extract the bidder questions from an RFP file's bytes (deduped, ordered)."""
    paragraphs = extract_paragraphs(filename, data)
    if paragraphs is None:
        raise ValueError(
            f"Unsupported file type for '{filename}'. Upload a PDF or DOCX RFP."
        )
    windows = _windows(paragraphs)
    if not windows:
        raise ValueError("No readable text found in the document (is it a scanned PDF?).")

    semaphore = asyncio.Semaphore(settings.max_llm_concurrency)
    results = await asyncio.gather(
        *[_extract_window(w, settings, semaphore) for w in windows],
        return_exceptions=True,
    )

    seen: set[str] = set()
    ordered: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue                       # a bad window shouldn't sink the whole file
        for q in r:
            key = _norm(q)
            if key and key not in seen:
                seen.add(key)
                ordered.append(q)
    return ordered[:max_questions]


def to_parsed(questions: list[str]) -> dict:
    """Wrap an extracted question list in the CSV-parser's shape so the existing
    /run → answer_questions → /download pipeline consumes it unchanged."""
    return {
        "fieldnames": ["Question"],
        "rows": [{"Question": q} for q in questions],
        "question_column": "Question",
        "count": len(questions),
    }


async def extract_rfp_to_parsed(
    filename: str,
    data: bytes,
    settings: Settings,
    max_questions: int = 500,
) -> dict:
    questions = await extract_questions(filename, data, settings, max_questions)
    if not questions:
        raise ValueError(
            "No bidder questions were found in this document. If it is a scanned "
            "PDF, OCR it first; otherwise check it is the RFP/questionnaire."
        )
    return to_parsed(questions)
