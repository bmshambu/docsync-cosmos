"""Community summariser — cloud replacement for the rfp-community-summarizer skill.

For each community in the community map:
  1. Gather full entity details, internal + cross-community relationships, top source chunks
  2. Call the LLM to write a structured markdown summary (300-600 words)
  3. Persist the summary via the GraphStore
  4. Update the community map with a summary_file pointer (incremental)

All persistence goes through the GraphStore (file today, Cosmos in step 2).
Supports cancel_event and selected/max_communities for Stop & Save / batch control.
"""

from __future__ import annotations

import asyncio
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm.client import get_chat
from app.llm.prompts import build_summary_prompt


# ── Chunk search (keyword-based, same logic as query_graph.py) ────────────────

_STOP_WORDS = {
    "the", "and", "for", "are", "was", "with", "that", "this", "have",
    "from", "they", "will", "been", "what", "which", "how", "not", "but",
}


def _search_chunks(keywords: list[str], all_chunks: list[dict], top_n: int = 3) -> list[dict]:
    kws = [k.lower() for k in keywords if k.lower() not in _STOP_WORDS and len(k) > 2]
    if not kws:
        return []
    scored: list[tuple[int, dict]] = []
    for c in all_chunks:
        text = (c.get("text") or "").lower()
        score = sum(text.count(k) for k in kws)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_n]]


# ── Per-community context builder ─────────────────────────────────────────────

def _build_community_context(
    comm_id: str,
    community: dict,
    entity_lookup: dict[str, dict],
    all_relationships: list[dict],
    all_chunks: list[dict],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return (full_entities, internal_rels, cross_rels, top_chunks)."""
    member_ids = {e["id"] for e in community.get("entities", []) if e.get("id")}

    # Full entity details
    full_entities = [
        entity_lookup.get(eid, {"id": eid, "name": eid, "type": "unknown",
                                 "source_docs": [], "attributes": {}})
        for eid in member_ids
    ]

    # Split relationships into internal / cross-community
    internal_rels, cross_rels = [], []
    for r in all_relationships:
        src, tgt = r.get("source"), r.get("target")
        if not src or not tgt:
            continue
        src_in = src in member_ids
        tgt_in = tgt in member_ids
        if src_in and tgt_in:
            internal_rels.append(r)
        elif src_in or tgt_in:
            cross_rels.append(r)

    # Keyword search chunks using entity names
    keywords = [e.get("name", "") for e in full_entities if e.get("name")]
    top_chunks = _search_chunks(keywords, all_chunks, top_n=3)

    return full_entities, internal_rels, cross_rels[:10], top_chunks


# ── LLM call ─────────────────────────────────────────────────────────────────

async def _summarise_one(
    comm_id: str,
    community: dict,
    entity_lookup: dict,
    all_relationships: list[dict],
    all_chunks: list[dict],
    store,
    model: str,
    semaphore: asyncio.Semaphore,
    max_tokens: int = 8192,
    graph_id: str | None = None,
) -> dict:
    """Summarise one community, persist it via the store, return result dict."""
    full_entities, internal_rels, cross_rels, top_chunks = _build_community_context(
        comm_id, community, entity_lookup, all_relationships, all_chunks
    )

    system, user = build_summary_prompt(
        comm_id=comm_id,
        entities=full_entities,
        internal_rels=internal_rels,
        cross_rels=cross_rels,
        chunk_excerpts=top_chunks,
    )

    chat = get_chat(model, temperature=0.2, max_tokens=max_tokens, json_mode=False)

    # Thinking models (Gemini 2.5, GPT reasoning) can exhaust the token budget
    # on internal reasoning and return an EMPTY visible answer with no error.
    # Validate + retry, and never silently write an empty summary.
    summary_text = ""
    for attempt in range(2):
        async with semaphore:
            resp = await chat.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        summary_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        # Strip any accidental code fences the model may add
        summary_text = re.sub(r"^```[a-zA-Z]*\n?", "", summary_text.strip())
        summary_text = re.sub(r"\n?```$", "", summary_text).strip()
        if len(summary_text) >= 50:
            break

    if len(summary_text) < 50:
        raise ValueError(
            "LLM returned an empty/near-empty summary twice — likely the token budget "
            "was consumed by reasoning. Increase MAX_SUMMARY_TOKENS in .env."
        )

    # Guarantee a title heading so the UI never falls back to the filename
    if not summary_text.lstrip().startswith("#"):
        summary_text = f"# Community {comm_id} — Summary\n\n{summary_text}"

    ref = store.save_community_summary(comm_id, summary_text, graph_id)

    return {
        "comm_id": comm_id,
        "file": ref,
        "entities": len(full_entities),
        "summary_preview": summary_text[:200],
    }


def _update_community_map(store, comm_id: str, summary_file: str, graph_id: str | None = None):
    """Add summary_file pointer to a community entry (incremental update)."""
    try:
        data = store.get_community_map(graph_id)
        if comm_id in data.get("communities", {}):
            data["communities"][comm_id]["summary_file"] = summary_file
            store.save_community_map(data, graph_id)
    except Exception:
        pass  # non-fatal — the stored summary is the real output


# ── Public API ────────────────────────────────────────────────────────────────

async def summarise_corpus(
    store,
    model: str,
    max_concurrency: int,
    max_communities: int | None = None,
    selected_communities: list[str] | None = None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    max_tokens: int = 8192,
    graph_id: str | None = None,
) -> tuple[list[dict], bool]:
    """Summarise all (or selected / up to max_communities) communities.

    graph_id selects which community graph to summarise: None/'default' is the
    whole-corpus graph; a scope_graph_id is a per-scope graph (item #4). The map
    is read from — and every summary written to — that graph_id.

    Returns (results_list, was_cancelled).
    """
    community_map = store.get_community_map(graph_id)
    entities      = store.get_entities()
    relationships = store.get_relationships()
    all_chunks    = list(store.iter_chunks())

    entity_lookup  = {e["id"]: e for e in entities}
    communities    = community_map.get("communities", {})

    # Sort by community id (numeric), apply selection or max_communities slice
    comm_ids = sorted(communities.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    if selected_communities:
        sel = set(selected_communities)
        comm_ids = [c for c in comm_ids if c in sel]
    elif max_communities:
        comm_ids = comm_ids[:max_communities]

    total = len(comm_ids)
    semaphore = asyncio.Semaphore(max_concurrency)
    results: list[dict] = []
    done = 0
    was_cancelled = False

    async def _run(cid: str) -> dict:
        try:
            return await _summarise_one(
                comm_id=cid,
                community=communities[cid],
                entity_lookup=entity_lookup,
                all_relationships=relationships,
                all_chunks=all_chunks,
                store=store,
                model=model,
                semaphore=semaphore,
                max_tokens=max_tokens,
                graph_id=graph_id,
            )
        except Exception as exc:
            # Persist a stub so the community is still listed in the UI
            ref = store.save_community_summary(
                cid,
                f"# Community {cid} — Summary Unavailable\n\n"
                f"_(Error generating summary: {exc})_\n",
                graph_id,
            )
            return {"comm_id": cid, "error": str(exc), "file": ref}

    tasks = [asyncio.create_task(_run(cid)) for cid in comm_ids]

    for coro in asyncio.as_completed(tasks):
        if cancel_event and cancel_event.is_set():
            was_cancelled = True
            for t in tasks:
                t.cancel()
            break

        result = await coro
        results.append(result)
        done += 1

        # Incremental map update
        if not result.get("error"):
            _update_community_map(store, result["comm_id"], result["file"], graph_id)

        if on_progress:
            on_progress(done, total, result.get("comm_id"), result.get("error"))

    # If every queued community finished, this was NOT a partial run — the
    # cancel event may have fired after the last task already completed.
    if done >= total:
        was_cancelled = False

    return results, was_cancelled
