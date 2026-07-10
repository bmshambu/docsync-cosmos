"""Step 3 — Query Agent API.

GET  /api/query/prerequisites   → check if graph + communities are ready
POST /api/query/ask             → retrieve context + synthesise answer (direct, no polling)
GET  /api/query/suggestions     → return example questions from the graph
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.llm.query_agent import ask as agent_ask

router = APIRouter(prefix="/api/query", tags=["query"])


# ── Prerequisites ─────────────────────────────────────────────────────────────

@router.get("/prerequisites")
async def prerequisites():
    from app.services.graph_store import get_graph_store
    store = get_graph_store()

    entity_count  = store.count_entities()
    community_map = store.get_community_map()
    communities   = community_map.get("communities", {})

    if not (entity_count and communities and store.has_chunks()):
        return {"ready": False, "entities": 0, "communities": 0, "summaries": 0}

    # Count real summaries in one backend call — pointers in the map are
    # wiped on rebuild, and per-community point reads are slow on Cosmos
    ok_ids = store.summary_ok_ids()
    summaries = sum(1 for cid in communities if str(cid) in ok_ids)

    settings = get_settings()
    return {
        "ready": True,
        "entities": entity_count,
        "communities": len(communities),
        "summaries": summaries,
        "summaries_warning": summaries == 0,
        # Session-tunable retrieval knobs: env defaults + allowed ranges
        "retrieval": {
            "defaults": {
                "top_chunks": settings.top_chunks,
                "top_communities": settings.top_communities,
                "max_prompt_entities": settings.max_prompt_entities,
                "max_prompt_relationships": settings.max_prompt_relationships,
            },
            "ranges": RETRIEVAL_RANGES,
        },
    }


# ── Ask ───────────────────────────────────────────────────────────────────────

# Session-tunable retrieval knobs: the UI offers these within safe ranges;
# the server clamps regardless (the API is callable directly) so a stray
# value can never blow the context window. None → .env default.
RETRIEVAL_RANGES = {
    "top_chunks":               (1, 8),
    "top_communities":          (1, 5),
    "max_prompt_entities":      (4, 15),
    "max_prompt_relationships": (5, 30),
}


def _clamp(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    lo, hi = RETRIEVAL_RANGES[name]
    return max(lo, min(hi, int(value)))


class AskRequest(BaseModel):
    question: str
    query_type: str = "auto"                    # auto | local | global | hybrid
    top_chunks: int | None = None               # None → TOP_CHUNKS from .env
    top_communities: int | None = None          # None → TOP_COMMUNITIES from .env
    max_prompt_entities: int | None = None      # None → MAX_PROMPT_ENTITIES from .env
    max_prompt_relationships: int | None = None # None → MAX_PROMPT_RELATIONSHIPS from .env
    hops: int = 1


@router.post("/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(400, "question is required")

    settings = get_settings()
    from app.services.graph_store import get_graph_store
    if get_graph_store().count_entities() == 0:
        raise HTTPException(400, "Graph not ready. Run Data Prep first.")

    try:
        result = await agent_ask(
            question=req.question.strip(),
            settings=settings,
            query_type=req.query_type,
            top_chunks=_clamp("top_chunks", req.top_chunks),
            top_communities=_clamp("top_communities", req.top_communities),
            max_prompt_entities=_clamp("max_prompt_entities", req.max_prompt_entities),
            max_prompt_relationships=_clamp("max_prompt_relationships", req.max_prompt_relationships),
            hops=req.hops,
        )
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        msg = str(exc)
        if "API key not valid" in msg or "API_KEY_INVALID" in msg:
            raise HTTPException(503, "Invalid Google API key — check GOOGLE_API_KEY in .env")
        if "AuthenticationError" in type(exc).__name__ or ("401" in msg and "azure" in msg.lower()):
            raise HTTPException(503, "Azure OpenAI auth failed — check AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT in .env")
        if "quota" in msg.lower() or "429" in msg:
            raise HTTPException(503, "LLM quota exceeded. Try again later.")
        raise HTTPException(500, f"LLM error: {msg[:200]}")

    return result


# ── Example questions ─────────────────────────────────────────────────────────

@router.get("/suggestions")
async def suggestions():
    from app.services.graph_store import get_graph_store
    entities = get_graph_store().get_entities()
    if not entities:
        return {"suggestions": []}

    # Pull a few real entity names to make suggestions concrete
    clients   = [e["name"] for e in entities if e.get("type") == "client"][:2]
    standards = [e["name"] for e in entities if e.get("type") == "standard"][:2]
    techs     = [e["name"] for e in entities if e.get("type") == "technology"][:1]

    base = [
        "Compare requirements across all RFPs",
        "Which RFPs mention security standards?",
        "List all deliverables required",
        "What technologies are mentioned across RFPs?",
        "Summarise the key themes in this corpus",
    ]
    specific = []
    if clients:
        specific.append(f"What does {clients[0]} require?")
    if standards:
        specific.append(f"Which RFPs reference {standards[0]}?")
    if techs:
        specific.append(f"Which vendors use {techs[0]}?")
    if len(clients) > 1:
        specific.append(f"Compare {clients[0]} and {clients[1]}")

    return {"suggestions": (specific + base)[:6]}
