"""Query agent — retrieval + LLM synthesis in one async call."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.prompts import build_query_prompt
from app.llm.query_planner import plan_query
from app.services.retriever import retrieve


async def ask(
    question: str,
    settings: Settings,
    query_type: str = "auto",
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

    context = retrieve(clean_question, settings, query_type=query_type,
                       top_chunks=top_chunks, top_communities=top_communities,
                       hops=hops)

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

    # Build detail payloads for clickable pills.
    # In blob mode, attach a doc link so users can open the exact source RFP.
    from urllib.parse import quote
    blob_mode = settings.blob_mode
    chunk_details = []
    for c in context["top_chunks"]:
        fname = c.get("filename") or c.get("doc_id", "?")
        detail = {
            "filename": fname,
            "page": c.get("page_start", "?"),
            "section": c.get("section", ""),
            "text": (c.get("text") or "")[:1200],
        }
        if blob_mode and fname and fname != "?":
            detail["doc_url"] = f"/api/data-prep/doc-open?filename={quote(fname)}"
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
    }
