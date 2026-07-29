"""Step 4 — Batch Q&A API (CSV in, answers out).

POST /api/batch/upload          → parse a questions CSV, return a preview
POST /api/batch/upload-rfp      → extract questions from an RFP (pdf/docx), preview
POST /api/batch/run             → answer every question (background job, live results)
POST /api/batch/cancel/{id}     → stop & keep partial answers
GET  /api/batch/status/{id}     → poll progress + logs + live/partial + result
GET  /api/batch/download/{id}   → CSV with answer columns (partial mid-run, full when done)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import get_settings
from app.jobs import job_manager
from app.routers.query import RETRIEVAL_RANGES, _clamp
from app.services.batch_qa import (
    OUTPUT_COLUMNS,
    answer_questions,
    parse_questions_csv,
    rows_to_csv,
)

router = APIRouter(prefix="/api/batch", tags=["batch"])

MAX_UPLOAD_BYTES = 2_000_000
MAX_QUESTIONS = 500

# Parsed uploads awaiting a run. In-memory: an upload is only meaningful
# between /upload and /run in the same session.
_uploads: dict[str, dict] = {}


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file.")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"CSV is too large (limit {MAX_UPLOAD_BYTES // 1000} KB).")

    try:
        parsed = parse_questions_csv(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Could not read the CSV: {exc}")

    if parsed["count"] > MAX_QUESTIONS:
        raise HTTPException(400, f"{parsed['count']} questions exceeds the limit of {MAX_QUESTIONS}.")

    return _store_and_preview(parsed, file.filename, source="csv")


# ── Upload RFP (pdf/docx → extracted questions) ───────────────────────────────

RFP_MAX_UPLOAD_BYTES = 25_000_000   # RFPs are larger than a CSV of questions


@router.post("/upload-rfp")
async def upload_rfp(file: UploadFile = File(...)):
    from app.services.rfp_questions import SUPPORTED_RFP_EXTS, extract_rfp_to_parsed

    name = (file.filename or "").lower()
    if not name.endswith(SUPPORTED_RFP_EXTS):
        raise HTTPException(400, "Please upload a PDF or DOCX RFP document.")
    raw = await file.read()
    if len(raw) > RFP_MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File is too large (limit {RFP_MAX_UPLOAD_BYTES // 1_000_000} MB).")

    settings = get_settings()
    if not settings.api_key_set:
        raise HTTPException(400, "No LLM API key configured — question extraction needs the model.")

    try:
        parsed = await extract_rfp_to_parsed(file.filename, raw, settings, max_questions=MAX_QUESTIONS)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Could not extract questions: {str(exc)[:200]}")

    if parsed["count"] > MAX_QUESTIONS:
        parsed["rows"] = parsed["rows"][:MAX_QUESTIONS]
        parsed["count"] = MAX_QUESTIONS

    return _store_and_preview(parsed, file.filename, source="rfp")


def _store_and_preview(parsed: dict, filename: str, source: str) -> dict:
    upload_id = uuid.uuid4().hex[:12]
    _uploads[upload_id] = parsed
    q_col = parsed["question_column"]
    return {
        "upload_id": upload_id,
        "filename": filename,
        "source": source,                       # "csv" | "rfp"
        "question_column": q_col,
        "columns": parsed["fieldnames"],
        "count": parsed["count"],
        "preview": [r.get(q_col, "") for r in parsed["rows"] if r.get(q_col)][:5],
        "llm_calls": parsed["count"] * 2,   # planner + synthesis per question
    }


# ── Run ───────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    upload_id: str
    query_type: str = "auto"
    platform: str | None = None            # dual-track scope for all questions
    top_chunks: int | None = None
    top_communities: int | None = None
    max_prompt_entities: int | None = None
    max_prompt_relationships: int | None = None


@router.post("/run")
async def start_run(req: RunRequest):
    parsed = _uploads.get(req.upload_id)
    if not parsed:
        raise HTTPException(404, "Upload not found — please upload the CSV again.")

    settings = get_settings()
    from app.services.graph_store import get_graph_store
    if get_graph_store().count_entities() == 0:
        raise HTTPException(400, "Graph not ready. Run Data Prep first.")

    overrides = {
        k: _clamp(k, getattr(req, k))
        for k in RETRIEVAL_RANGES
        if getattr(req, k) is not None
    }

    job = job_manager.create(kind="batch_qa")

    async def factory(emit, cancel_event):
        total = parsed["count"]
        emit(f"Answering {total} question(s) — {total * 2} LLM calls "
             f"({settings.active_model_label}) …", progress=0.02, stage="answering")

        q_col = parsed["question_column"]
        # Live view seeded from the input so pending rows are visible too.
        live_rows = [dict(r) for r in parsed["rows"]]

        def _publish():
            answered = sum(1 for r in live_rows if str(r.get("Status", "")).startswith("Answered"))
            done_n = sum(1 for r in live_rows if r.get("Status"))
            emit.set_partial({
                "fieldnames": parsed["fieldnames"],
                "question_column": q_col,
                "rows": live_rows,
                "total": total,
                "done": done_n,
                "answered": answered,
            })

        def on_progress(done, tot, idx, row):
            live_rows[idx] = row
            q = row.get(q_col, "")
            emit(f"[{done}/{tot}] {q[:70]}… — {row.get('Status', '')}",
                 progress=0.02 + 0.96 * (done / tot), stage="answering")
            _publish()   # stream the row into /status for the live table

        from app.routers.query import _clean_platform
        rows, was_cancelled = await answer_questions(
            parsed, settings,
            query_type=req.query_type,
            platform=_clean_platform(req.platform),
            retrieval_overrides=overrides,
            max_concurrency=max(1, settings.max_llm_concurrency // 2),
            cancel_event=cancel_event,
            on_progress=on_progress,
        )

        answered = sum(1 for r in rows if str(r.get("Status", "")).startswith("Answered"))
        no_content = sum(1 for r in rows
                         if str(r.get("Status", "")).startswith(("NO CONTENT", "GAP")))
        errors = sum(1 for r in rows if str(r.get("Status", "")).startswith("ERROR"))
        emit(f"Done{' (stopped early)' if was_cancelled else ''}. "
             f"{answered} answered · {no_content} need new content · {errors} errors.",
             progress=1.0, stage="done")

        return {
            "fieldnames": parsed["fieldnames"],
            "question_column": parsed["question_column"],
            "rows": rows,
            "total": parsed["count"],
            "answered": answered,
            "no_content": no_content,
            "errors": errors,
            "was_cancelled": was_cancelled,
        }

    job_manager.run(job, factory)
    return {"job_id": job.id, "status": job.status}


# ── Cancel / status ───────────────────────────────────────────────────────────

@router.post("/cancel/{job_id}")
async def cancel(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status not in ("running", "cancelling"):
        return {"status": job.status, "message": "Job is not running"}
    job.cancel()
    return {"status": "cancelling"}


@router.get("/status/{job_id}")
async def status(job_id: str):
    d = job_manager.status_dict(job_id)
    if not d:
        raise HTTPException(404, "job not found")
    return d


# ── Download ──────────────────────────────────────────────────────────────────

@router.get("/download/{job_id}")
async def download(job_id: str):
    d = job_manager.status_dict(job_id)
    if not d:
        raise HTTPException(404, "job not found")

    # Prefer the final result; fall back to the live partial so a run can be
    # downloaded mid-flight (partial CSV of the answers completed so far).
    src = d.get("result") or d.get("partial") or {}
    if not src.get("rows"):
        raise HTTPException(400, "No answers yet — the job has not produced results.")

    partial = not d.get("result")
    fname = f"answers_{'partial_' if partial else ''}{job_id}.csv"
    csv_text = rows_to_csv(src["fieldnames"], src["rows"])
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
