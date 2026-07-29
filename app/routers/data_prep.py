"""Step 1 — Data Prep API.

GET  /api/data-prep/scan            → count + names of docs in a folder (no LLM)
POST /api/data-prep/run             → start a background data-prep job
POST /api/data-prep/cancel/{id}     → request stop-and-save for a running job
GET  /api/data-prep/status/{id}     → poll job progress + logs + result
GET  /api/data-prep/graph-stats     → latest graph_stats.json (if any)
GET  /api/data-prep/graph-html      → the generated interactive graph HTML
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.graphs.data_prep_graph import run_build_only, run_data_prep
from app.jobs import job_manager
from app.services.storage import SUPPORTED_EXTENSIONS, get_source

router = APIRouter(prefix="/api/data-prep", tags=["data-prep"])


# ── Scan ──────────────────────────────────────────────────────────────────────

@router.get("/containers")
async def blob_containers():
    """List blob containers in the storage account (blob mode only)."""
    settings = get_settings()
    if not settings.blob_mode:
        return {"blob_mode": False, "containers": [], "default": ""}
    from app.services.storage import list_blob_containers
    try:
        containers = list_blob_containers()
    except Exception as exc:
        raise HTTPException(400, f"Could not list containers: {exc}")
    default = settings.azure_storage_container_name
    if default not in containers:
        default = containers[0] if containers else ""
    return {"blob_mode": True, "containers": containers, "default": default}


@router.get("/folders")
async def blob_folders(container: str = Query(default="", description="Container to inspect; empty = .env default")):
    """List virtual folders in a blob container (blob mode only)."""
    settings = get_settings()
    if not settings.blob_mode:
        return {"blob_mode": False, "folders": []}
    from app.services.storage import list_blob_folders
    try:
        folders = list_blob_folders(container or None)
    except Exception as exc:
        raise HTTPException(400, f"Could not list blob folders: {exc}")
    return {"blob_mode": True, "folders": folders}


@router.get("/scan")
async def scan(
    folder_path: str = Query(default="", description="Absolute path to a folder of RFP docs (ignored in blob mode)"),
    folders: list[str] | None = Query(default=None, description="Blob folders (prefixes) to scan; omit for whole container"),
    container: str = Query(default="", description="Blob container; empty = .env default"),
):
    settings = get_settings()
    folder_path = folder_path.strip()
    if not settings.blob_mode and not folder_path:
        raise HTTPException(400, "folder_path is required (or configure Azure Blob Storage in .env)")
    try:
        source = get_source(folder_path, prefixes=folders, container=container or None)
        names = source.list_document_names()   # names only — no blob download
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        # Covers Azure SDK errors (auth, invalid container name, network)
        raise HTTPException(400, f"Document source error: {exc}")

    from pathlib import Path as _P
    by_type: dict[str, int] = {}
    for n in names:
        ext = _P(n).suffix.lower().lstrip(".")
        by_type[ext] = by_type.get(ext, 0) + 1

    # Which files already have entities extracted (for incremental runs)
    from app.services.graph_store import get_graph_store
    extracted = get_graph_store().get_extracted_doc_names()

    return {
        "count": len(names),
        "by_type": by_type,
        "files": names,
        "extracted": sorted(extracted),
        "blob_mode": settings.blob_mode,
    }


# ── Run ───────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    folder_path: str = ""
    resolution: float = 1.0
    skip_existing: bool = True
    max_docs: int | None = None              # None = process all
    selected_files: list[str] | None = None  # None = all; else only these filenames
    force_reextract: bool = False            # purge + re-extract the selected files
    folders: list[str] | None = None         # blob folders (prefixes); None = whole container
    container: str = ""                      # blob container; "" = .env default


@router.post("/run")
async def start_run(req: RunRequest):
    settings = get_settings()
    folder = req.folder_path.strip()
    if not settings.blob_mode and not folder:
        raise HTTPException(400, "folder_path is required (or configure Azure Blob Storage in .env)")

    job = job_manager.create(kind="data_prep")

    async def factory(emit, cancel_event):
        return await run_data_prep(
            folder_path=folder,
            emit=emit,
            cancel_event=cancel_event,
            resolution=req.resolution,
            skip_existing=req.skip_existing,
            max_docs=req.max_docs,
            selected_files=req.selected_files,
            force_reextract=req.force_reextract,
            blob_folders=req.folders,
            blob_container=req.container or None,
        )

    job_manager.run(job, factory)
    return {"job_id": job.id, "status": job.status}


# ── Cancel ────────────────────────────────────────────────────────────────────

@router.post("/cancel/{job_id}")
async def cancel(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status not in ("running", "cancelling"):
        return {"status": job.status, "message": "Job is not running"}
    job.cancel()
    return {"status": "cancelling"}


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status/{job_id}")
async def status(job_id: str):
    # Read-through: memory first, persisted snapshot (survives restarts) second
    d = job_manager.status_dict(job_id)
    if not d:
        raise HTTPException(404, "job not found")
    return d


# ── Outputs ───────────────────────────────────────────────────────────────────

@router.get("/graph-stats")
async def graph_stats():
    from app.services.graph_store import get_graph_store
    stats = get_graph_store().get_graph_stats()
    if not stats:
        return JSONResponse({"exists": False})
    stats["exists"] = True
    return stats


@router.get("/graph-scopes")
async def graph_scopes():
    """Scope values for the graph-viz picker (both fields), with doc counts.
    Empty when no metadata registry has been synced yet."""
    from app.services.graph_store import get_graph_store
    try:
        vals = get_graph_store().list_scopes()
    except Exception:
        vals = []
    return {"scopes": vals, "scoping_available": bool(vals)}


@router.get("/graph-html")
async def graph_html(
    scope: str = Query("", description="Platform / Service Function value; empty = whole corpus"),
    limit: int = Query(600, ge=50, le=2000, description="max nodes rendered (degree-capped)"),
):
    """Interactive graph HTML, generated ON DEMAND from the store for ONE scope.

    The whole-corpus graph (~22k entities) freezes the browser — which is why it
    rendered blank — so the viz is always scoped and degree-capped. Pass a
    Platform / Sub Service Line / Service Function value to focus it."""
    from app.services.graph_store import get_graph_store
    from app.services.graph_html import generate_scope_graph_html

    store = get_graph_store()
    if store.count_entities() == 0:
        raise HTTPException(404, "Graph not generated yet. Run Data Prep first.")

    scope_value = scope.strip() or None
    html = generate_scope_graph_html(store, scope_value, node_cap=limit)
    return HTMLResponse(html)


# ── Open source document (redirects to a short-lived blob SAS URL) ────────────

@router.get("/doc-open")
async def doc_open(filename: str = Query(..., description="Blob filename to view")):
    from fastapi.responses import RedirectResponse
    from app.services.storage import get_blob_view_url

    url = get_blob_view_url(filename)
    if not url:
        raise HTTPException(404, "Document links are only available in Azure Blob mode.")
    return RedirectResponse(url)


# ── Build graph only (no LLM re-extraction) ───────────────────────────────────

class BuildGraphRequest(BaseModel):
    resolution: float = 1.0


@router.post("/build-graph")
async def build_graph_only(req: BuildGraphRequest):
    """Rebuild the knowledge graph from existing entities without re-running LLM extraction."""
    job = job_manager.create(kind="build_graph")

    async def factory(emit, cancel_event):
        return await run_build_only(emit=emit, resolution=req.resolution)

    job_manager.run(job, factory)
    return {"job_id": job.id, "status": job.status}
