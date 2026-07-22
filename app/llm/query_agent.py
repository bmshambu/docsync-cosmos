"""Query agent — retrieval + LLM synthesis in one async call.

M2 dual-track: when a Platform scope is selected, retrieval runs TWICE in
parallel — Track A (the selected Platform, e.g. Oracle) and Track B (fixed
Services_Function = "Clients and Markets") — then ONE synthesis call merges the
two track-labeled contexts. Technical/approach content comes from Track A;
client references, credentials, and market proof from Track B. A document
tagged in both is deduped and labeled "Oracle + Clients and Markets". Still
2 LLM calls per question (the second track is retrieval-only).
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.prompts import build_query_prompt
from app.llm.query_planner import plan_query
from app.services.metadata import CLIENTS_AND_MARKETS
from app.services.retriever import retrieve

CM_LABEL = "Clients and Markets"


def _ckey(c: dict) -> tuple:
    return (c.get("doc_id"), (c.get("text") or "")[:100])


def _merge_dual_tracks(ctx_a: dict, ctx_b: dict, label_a: str, label_b: str) -> tuple[dict, dict]:
    """Merge two scoped retrieval contexts into one, tagging each chunk/entity
    with its track and deduping cross-track overlaps as "A + B"."""
    a_keys = {_ckey(c) for c in ctx_a["top_chunks"]}
    b_keys = {_ckey(c) for c in ctx_b["top_chunks"]}
    both = a_keys & b_keys

    def track_of(k):
        return f"{label_a} + {label_b}" if k in both else (label_a if k in a_keys else label_b)

    merged_chunks, seen = [], set()
    for c in ctx_a["top_chunks"] + ctx_b["top_chunks"]:
        k = _ckey(c)
        if k in seen:
            continue
        seen.add(k)
        merged_chunks.append({**c, "track_label": track_of(k)})

    # Entities — dedup by id, mark cross-track as both
    ent: dict = {}
    for e in ctx_a["matched_entities"]:
        ent[e["id"]] = {**e, "track_label": label_a}
    for e in ctx_b["matched_entities"]:
        if e["id"] in ent:
            ent[e["id"]]["track_label"] = f"{label_a} + {label_b}"
        else:
            ent[e["id"]] = {**e, "track_label": label_b}

    def dedup_first(items, key):
        out = {}
        for it in items:
            out.setdefault(key(it), it)
        return list(out.values())

    merged = {
        "query_type": ctx_a["query_type"],
        "matched_entities": list(ent.values()),
        "traversal": {"entity_ids": [],
                      "relationships": ctx_a["traversal"]["relationships"]
                      + ctx_b["traversal"]["relationships"]},
        "relevant_communities": dedup_first(
            ctx_a["relevant_communities"] + ctx_b["relevant_communities"], lambda t: t[0]),
        "top_chunks": merged_chunks,
        "financial_table": dedup_first(
            ctx_a["financial_table"] + ctx_b["financial_table"], lambda r: r["name"]),
        "title_matched_docs": dedup_first(
            ctx_a["title_matched_docs"] + ctx_b["title_matched_docs"], lambda m: m["doc_id"]),
        "scope_doc_count": ctx_a.get("scope_doc_count"),
    }
    stats = {
        "dual": True,
        "track_a": {"label": label_a, "chunks": len(ctx_a["top_chunks"]),
                    "scope": ctx_a.get("scope_doc_count")},
        "track_b": {"label": label_b, "chunks": len(ctx_b["top_chunks"]),
                    "scope": ctx_b.get("scope_doc_count")},
        "both_chunks": len(both),
    }
    return merged, stats


async def ask(
    question: str,
    settings: Settings,
    query_type: str = "auto",
    platform: str | None = None,                 # Track-A scope; triggers dual-track
    top_chunks: int | None = None,               # None → settings defaults
    top_communities: int | None = None,
    max_prompt_entities: int | None = None,
    max_prompt_relationships: int | None = None,
    hops: int = 1,
) -> dict:
    # Planner pre-pass: clean typos, make the question precise, and (when the
    # user left mode on auto) pick query_type + hops. Falls back silently.
    plan = await plan_query(question, settings)
    clean_question = plan["question"]
    if query_type == "auto" and plan["planned"]:
        query_type = plan["query_type"]
        hops = plan["hops"]

    def _retrieve(**scope):
        return retrieve(clean_question, settings, query_type=query_type,
                        top_chunks=top_chunks, top_communities=top_communities,
                        hops=hops, **scope)

    # Dual-track when a Platform is selected (and it isn't C&M itself).
    dual = bool(platform) and platform.strip().lower() != CLIENTS_AND_MARKETS
    track_stats: dict | None = None
    if dual:
        ctx_a = _retrieve(platform=platform)
        ctx_b = _retrieve(service_function=CM_LABEL)
        context, track_stats = _merge_dual_tracks(ctx_a, ctx_b, platform, CM_LABEL)
    else:
        # No scope, or the user typed "Clients and Markets" → single track.
        sf = CM_LABEL if (platform and platform.strip().lower() == CLIENTS_AND_MARKETS) else None
        context = _retrieve(platform=(None if sf else platform), service_function=sf)

    system, user = build_query_prompt(
        clean_question, context,
        max_entities=max_prompt_entities,
        max_relationships=max_prompt_relationships,
    )
    chat = get_chat(settings.model_query, temperature=0.1,
                    max_tokens=settings.max_query_tokens, json_mode=False)
    resp = await chat.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    answer = resp.content if isinstance(resp.content, str) else str(resp.content)

    # Pull "Also try:" line out so the UI can render it as chips
    also_try: list[str] = []
    lines = answer.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("**Also try:**"):
            raw = line.replace("**Also try:**", "").strip()
            also_try = [s.strip().strip('"').strip("'") for s in raw.split("·") if s.strip()]
            lines.pop(i)
            break
    answer_clean = "\n".join(lines).strip()

    # Build detail payloads for clickable pills. Prefer the governed source URL
    # (Templafy/SharePoint webUrl from the registry); fall back to the blob SAS
    # open link. This keeps citations pointing at the system-of-record.
    from urllib.parse import quote
    from app.services.graph_store import get_graph_store
    store = get_graph_store()
    blob_mode = settings.blob_mode

    def _doc_link(doc_id: str | None, fname: str | None) -> str | None:
        url = store.doc_web_url(doc_id=doc_id, filename=fname)
        if url:
            return url
        if blob_mode and fname and fname != "?":
            return f"/api/data-prep/doc-open?filename={quote(fname)}"
        return None

    chunk_details = []
    for c in context["top_chunks"]:
        fname = c.get("filename") or c.get("doc_id", "?")
        detail = {
            "filename": fname,
            "page": c.get("page_start", "?"),
            "section": c.get("section", ""),
            "text": (c.get("text") or "")[:1200],
            "track": c.get("track_label"),   # dual-track attribution (None if single)
        }
        link = _doc_link(c.get("doc_id"), fname)
        if link:
            detail["doc_url"] = link
        chunk_details.append(detail)

    community_details = [
        {
            "id": cid,
            "entities": [e.get("name", "") for e in meta.get("entities", [])[:6]],
            "summary": (summary_text[:2000] if summary_text
                        else "(no summary yet — run the Community Summariser tab first)"),
        }
        for cid, meta, summary_text in context["relevant_communities"]
    ]

    # Title-matched documents — "this deck answers your question"
    matched_documents = []
    for m in context.get("title_matched_docs", []):
        doc = {"filename": m["filename"], "score": m["score"]}
        link = _doc_link(m.get("doc_id"), m["filename"])
        if link:
            doc["doc_url"] = link
        matched_documents.append(doc)

    return {
        "answer": answer_clean,
        "also_try": also_try,
        "query_type": context["query_type"],
        "entities_found": len(context["matched_entities"]),
        "communities_used": len(context["relevant_communities"]),
        "chunks_cited": len(context["top_chunks"]),
        "chunk_details": chunk_details,
        "community_details": community_details,
        "rewritten_question": clean_question if plan["planned"] else None,
        "hops_used": hops,
        "matched_documents": matched_documents,
        "platform": platform,
        "scope_doc_count": context.get("scope_doc_count"),
        "tracks": track_stats,   # dual-track per-track chunk counts (None if single)
    }
