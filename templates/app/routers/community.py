"""Step 2 — Community Summariser API.

GET  /api/community/prerequisites   → check if data prep ran (counts communities)
POST /api/community/run             → start summarisation job
POST /api/community/cancel/{id}     → stop & save
GET  /api/community/status/{id}     → poll progress
GET  /api/community/summaries       → list written community .md files with previews
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.graphs.community_graph import run_community_summary
from app.jobs import job_manager

router = APIRouter(prefix="/api/community", tags=["community"])


# ── Prerequisites check ───────────────────────────────────────────────────────

@router.get("/prerequisites")
async def prerequisites():
    from app.services.graph_store import get_graph_store
    store = get_graph_store()

    community_map = store.get_community_map()
    communities = community_map.get("communities", {})

    if not (communities and store.count_entities() and store.has_chunks()):
        return {"ready": False, "community_count": 0, "summaries_done": 0}

    ok_ids = store.summary_ok_ids()
    comm_list = [
        {
            "id": cid,
            "entity_count": len(c.get("entities", [])),
            "source_docs": c.get("source_docs", []),
            "has_summary": str(cid) in ok_ids,
        }
        for cid, c in sorted(
            communities.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0
        )
    ]
    return {
        "ready": True,
        "community_count": len(communities),
        "summaries_done": sum(1 for c in comm_list if c["has_summary"]),
        "communities": comm_list,
    }


# ── Run ───────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    max_communities: int | None = None
    selected_communities: list[str] | None = None  # None = all; else only these ids


@router.post("/run")
async def start_run(req: RunRequest):
    job = job_manager.create(kind="community_summary")

    async def factory(emit, cancel_event):
        return await run_community_summary(
            emit=emit,
            cancel_event=cancel_event,
            max_communities=req.max_communities,
            selected_communities=req.selected_communities,
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


# ── Summaries list ────────────────────────────────────────────────────────────

@router.get("/summaries")
async def summaries():
    from app.services.graph_store import get_graph_store
    return {"summaries": get_graph_store().list_community_summaries()}


# ── Per-scope communities (feedback item #4) ──────────────────────────────────

@router.get("/scope-plan")
async def scope_plan():
    """Size + build-status preview per scope so the UI can show cost before a
    run. scoping_available is False when no metadata registry exists yet."""
    from app.services.graph_store import get_graph_store
    from app.services import scope_communities as sc
    store = get_graph_store()
    try:
        status = sc.built_scope_status(store)
    except Exception:
        status = []
    if not status:
        return {"scoping_available": False, "scopes": []}
    plan = {p["scope"]: p for p in sc.plan_scopes(store)}
    for row in status:
        est = plan.get(row["scope"], {})
        row["entity_count"] = est.get("entity_count", 0)
    total_entities = sum(r.get("entity_count", 0) for r in status)
    return {
        "scoping_available": True,
        "scopes": status,
        "total_scopes": len(status),
        "total_entity_instances": total_entities,
    }


class ScopeBuildRequest(BaseModel):
    scopes: list[str] | None = None            # None = every scope value
    max_communities_per_scope: int | None = None
    resolution: float = 1.0


@router.post("/build-scopes")
async def build_scopes(req: ScopeBuildRequest):
    from app.services.scope_communities import run_scope_community_build
    job = job_manager.create(kind="scope_community_build")

    async def factory(emit, cancel_event):
        return await run_scope_community_build(
            emit=emit,
            cancel_event=cancel_event,
            scopes=req.scopes,
            max_communities_per_scope=req.max_communities_per_scope,
            resolution=req.resolution,
        )

    job_manager.run(job, factory)
    return {"job_id": job.id, "status": job.status}


@router.get("/scope-summaries")
async def scope_summaries(graph_id: str = ""):
    """Community summaries for ONE scope's graph (its scope_graph_id)."""
    from app.services.graph_store import get_graph_store
    if not graph_id:
        raise HTTPException(400, "graph_id is required")
    return {"summaries": get_graph_store().list_community_summaries(graph_id)}
