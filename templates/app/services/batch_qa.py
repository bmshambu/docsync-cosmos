"""Batch question answering — CSV in, answers out.

Users receive RFP questions as a list (already extracted from the client RFP —
the RFP itself is never ingested into the answer corpus). They upload a CSV,
every question runs through the normal planner → retrieve → synthesise
pipeline, and the original CSV comes back with answer columns appended.

The original columns are preserved so IDs/sections/etc. survive the round trip.

Each question costs 2 LLM calls (planner + synthesis). Questions run with a
sliding-window semaphore; cancellation is checked between completions and
partial results are always returned.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time

from app.config import Settings
from app.llm.query_agent import ask
from app.services.rfp_questions import FILTERED_STATUS

# Transient LLM failures (rate limits, model overload, network blips) are
# common in long unattended runs. Without retry, one 503 permanently loses that
# question's answer and the user re-runs the whole CSV.
_TRANSIENT_MARKERS = (
    "503", "429", "500", "502", "504",
    "high demand", "unavailable", "overloaded", "rate limit",
    "quota", "timeout", "timed out", "deadline", "connection",
)
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 3.0   # seconds; doubled each attempt


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def _error_status(exc: Exception | None) -> str:
    """Actionable error text — a batch of 24 questions is 48 LLM calls, so API
    quota is the failure users hit most; it should not look like a crash."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "resource_exhausted" in text or "exceeded your current quota" in text:
        return ("ERROR — LLM API quota exceeded (not a content problem). "
                "Wait for the quota window to reset or raise your plan limit, "
                "then re-run these rows.")
    if "api key not valid" in text or "api_key_invalid" in text:
        return "ERROR — invalid LLM API key; check GOOGLE_API_KEY / AZURE_OPENAI_API_KEY."
    if "high demand" in text or "503" in text or "unavailable" in text:
        return "ERROR — LLM temporarily unavailable after retries; re-run these rows."
    return f"ERROR — {type(exc).__name__}: {str(exc)[:180]}"

# Appended to each row of the uploaded CSV. "Sources" doubles as the citation
# column. Model / Time / Retrieval Settings are recorded per the RFP-workflow
# requirement (they are constant across a run but written per row for auditability).
OUTPUT_COLUMNS = [
    "Answer",
    "Status",
    "Confidence",
    "Confidence Reasoning",
    "Sources",
    "Matching Documents",
    "Content Tracks",
    "Query Type",
    "Interpreted As",
    "Sub-Questions",
    "Model",
    "Time (s)",
    "Retrieval Settings",
]


def _retrieval_settings_str(query_type: str, platform: str | None, overrides: dict) -> str:
    """Compact, human-readable record of the retrieval config used for a run."""
    parts = [f"mode={query_type or 'auto'}"]
    if platform:
        parts.append(f"scope={platform}")
    for k in ("top_chunks", "top_communities", "max_prompt_entities", "max_prompt_relationships"):
        if k in overrides and overrides[k] is not None:
            parts.append(f"{k}={overrides[k]}")
    return "; ".join(parts)

_QUESTION_HEADERS = ("question", "questions", "rfp question", "query", "requirement")


def parse_questions_csv(raw: bytes) -> dict:
    """Parse an uploaded CSV. Returns {fieldnames, rows, question_column, count}.

    The question column is the first header matching a known name, else the
    first column. Rows with an empty question are kept (and skipped at answer
    time) so the output CSV lines up with the input row-for-row.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    sniff = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sniff, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fieldnames = [f.strip() for f in (reader.fieldnames or []) if f is not None]
    if not fieldnames:
        raise ValueError("CSV has no header row.")

    q_col = next(
        (f for f in fieldnames if f.strip().lower() in _QUESTION_HEADERS),
        fieldnames[0],
    )

    rows = []
    for r in reader:
        clean = {k.strip(): (v or "").strip() for k, v in r.items() if k is not None}
        if any(clean.values()):          # drop wholly blank lines
            rows.append(clean)

    answerable = sum(1 for r in rows if r.get(q_col))
    if not answerable:
        raise ValueError(f"No questions found in column '{q_col}'.")

    return {
        "fieldnames": fieldnames,
        "rows": rows,
        "question_column": q_col,
        "count": answerable,
    }


def _sources_of(result: dict) -> str:
    seen, out = set(), []
    for c in result.get("chunk_details", []):
        label = f"{c.get('filename', '?')} p.{c.get('page', '?')}"
        if label not in seen:
            seen.add(label)
            out.append(label)
    return " | ".join(out)


# The LLM is instructed to declare a gap when the library can't answer, but it
# phrases it freely. Retrieval finding *some* chunks doesn't mean they answered
# the question — without this check a non-answer is reported as "Answered" and
# the Status column becomes useless for triage.
_GAP_PHRASES = (
    "library has no content",
    "does not specify",
    "doesn't specify",
    "not specified in the provided",
    "does not contain",
    "doesn't contain",
    "no information",
    "not enough on",
    "does not provide",
    "doesn't provide",
    "needs new source material",
    "cannot be determined from",
)


def _status_of(result: dict) -> str:
    """Honest coverage flag — 'no content' is a valid, useful outcome: it tells
    the content team which questions need new source material written."""
    if not result.get("chunks_cited") and not result.get("entities_found"):
        return "NO CONTENT — needs new source material"

    # Evidence was retrieved but the model says it doesn't answer the question
    head = (result.get("answer") or "")[:400].lower()
    if any(p in head for p in _GAP_PHRASES):
        return "GAP — retrieved content does not answer this; needs new source material"

    if result.get("matched_documents"):
        return "Answered (matching document found)"
    return "Answered (synthesised from GPS-knowledge base)"


async def answer_questions(
    parsed: dict,
    settings: Settings,
    query_type: str = "auto",
    platform: str | None = None,             # dual-track scope for the whole batch
    retrieval_overrides: dict | None = None,
    max_concurrency: int = 3,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
) -> tuple[list[dict], bool]:
    """Answer every question in a parsed CSV. Returns (rows, was_cancelled).

    Rows are the original dicts with OUTPUT_COLUMNS filled in. Unanswered rows
    (cancelled, or blank question) keep empty answer cells.
    """
    q_col = parsed["question_column"]
    rows = [dict(r) for r in parsed["rows"]]
    overrides = retrieval_overrides or {}
    model_label = settings.active_model_label
    retrieval_str = _retrieval_settings_str(query_type, platform, overrides)

    targets = [(i, r) for i, r in enumerate(rows) if r.get(q_col)]
    total = len(targets)
    semaphore = asyncio.Semaphore(max_concurrency)
    was_cancelled = False
    done = 0

    async def _run(idx: int, question: str, answerable: bool = True) -> tuple[int, dict]:
        # RFP-extracted questions carry a SEND/SUPPRESS flag from the
        # answerability filter. SUPPRESS rows are kept in the output for
        # auditability but never sent to the LLM — instant, no cost.
        if not answerable:
            return idx, {
                "Answer": "", "Status": FILTERED_STATUS,
                "Confidence": "", "Confidence Reasoning": "",
                "Sources": "", "Matching Documents": "", "Content Tracks": "",
                "Query Type": "", "Interpreted As": "", "Sub-Questions": "",
                "Model": "", "Time (s)": "", "Retrieval Settings": "",
            }
        last_exc: Exception | None = None
        t0 = time.perf_counter()
        for attempt in range(_RETRY_ATTEMPTS):
            if cancel_event and cancel_event.is_set():
                break
            try:
                async with semaphore:
                    result = await ask(question, settings, query_type=query_type,
                                       platform=platform, **overrides)
                tracks = result.get("tracks")
                track_summary = ""
                if tracks and tracks.get("dual"):
                    track_summary = (f"{tracks['track_a']['label']} ({tracks['track_a']['chunks']}) + "
                                     f"{tracks['track_b']['label']} ({tracks['track_b']['chunks']})")
                conf = result.get("confidence")
                return idx, {
                    "Answer": result.get("answer") or "",
                    "Status": _status_of(result),
                    "Confidence": f"{conf:.2f}" if isinstance(conf, (int, float)) else "",
                    "Confidence Reasoning": result.get("confidence_reason") or "",
                    "Sources": _sources_of(result),
                    "Matching Documents": " | ".join(
                        d["filename"] for d in result.get("matched_documents", [])
                    ),
                    "Content Tracks": track_summary,
                    "Query Type": (result.get("query_type") or "").upper(),
                    "Interpreted As": result.get("rewritten_question") or "",
                    # M3 decomposition — visible in the CSV, not just the Query tab's
                    # "Interpreted as N questions" note. Blank when not decomposed.
                    "Sub-Questions": " | ".join(result.get("sub_questions") or []),
                    "Model": model_label,
                    "Time (s)": f"{time.perf_counter() - t0:.1f}",
                    "Retrieval Settings": retrieval_str,
                }
            except Exception as exc:
                last_exc = exc
                if not _is_transient(exc) or attempt == _RETRY_ATTEMPTS - 1:
                    break
                # Back off OUTSIDE the semaphore so other questions keep moving
                await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** attempt))

        return idx, {
            "Answer": "",
            "Status": _error_status(last_exc),
            "Confidence": "", "Confidence Reasoning": "",
            "Sources": "", "Matching Documents": "", "Content Tracks": "",
            "Query Type": "", "Interpreted As": "", "Sub-Questions": "",
            "Model": model_label,
            "Time (s)": f"{time.perf_counter() - t0:.1f}",
            "Retrieval Settings": retrieval_str,
        }

    tasks = [asyncio.create_task(_run(i, r[q_col], r.get("_answerable", True)))
             for i, r in targets]

    for coro in asyncio.as_completed(tasks):
        if cancel_event and cancel_event.is_set():
            was_cancelled = True
            for t in tasks:
                t.cancel()
            break
        idx, cells = await coro
        rows[idx].update(cells)
        done += 1
        if on_progress:
            # Pass the row index + fully-updated row so callers can stream live
            # results while preserving the original input order.
            on_progress(done, total, idx, rows[idx])

    # Cancel firing after the last completion is not a partial run
    if done >= total:
        was_cancelled = False

    return rows, was_cancelled


def rows_to_csv(fieldnames: list[str], rows: list[dict]) -> str:
    """Serialise answered rows: original columns first, answers appended."""
    headers = list(fieldnames) + [c for c in OUTPUT_COLUMNS if c not in fieldnames]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in headers})
    return buf.getvalue()
