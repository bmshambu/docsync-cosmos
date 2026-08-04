"""Per-scope community builder (feedback item #4).

Builds a SEPARATE community graph for each scope value — every Platform / Sub
Service Line value AND every Service Function value (from ``store.list_scopes()``)
— instead of one corpus-wide graph whose ~22k entities render blank and whose
communities are too broad to be useful within a single scope.

No entity re-extraction: entities/relationships are extracted once and reused.
Per scope we only (1) filter the existing entities to that scope's documents,
(2) run Louvain on the sub-graph (free), and (3) summarise its communities
(LLM calls — the only cost). Each scope's artefacts are stored under its own
``scope_graph_id`` so scopes never collide (see graph_store.py).

The whole-corpus 'default' graph is left untouched — retrieval falls back to it
for any scope that has not been built yet.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.llm.summarizer import summarise_corpus
from app.services.graph_build import (
    build_community_map,
    build_graph,
    detect_communities,
)
from app.services.graph_store import scope_graph_id


def _to_doc_id(filename: str) -> str:
    """Filename → doc_id, matching the pipeline + retriever._in_scope."""
    return Path(filename).stem.replace(" ", "_")


def _entity_in_scope(entity: dict, scope_ids: set[str]) -> bool:
    return any(_to_doc_id(d) in scope_ids for d in (entity.get("source_docs") or []))


def _scoped_entities(entities: list[dict], scope_ids: set[str]) -> list[dict]:
    return [e for e in entities if _entity_in_scope(e, scope_ids)]


# ── Planning (cheap — no Louvain, no LLM) ─────────────────────────────────────

def plan_scopes(store, scopes: list[str] | None = None) -> list[dict]:
    """Per-scope size preview so the UI can show cost before a run.

    [{'scope','field','graph_id','doc_count','entity_count'}] — entity_count is
    the number of extracted entities that fall in the scope (a proxy for how
    many communities/summaries it will produce). Cheap: loads the corpus once
    and filters in memory."""
    entities = store.get_entities()
    scope_defs = store.list_scopes()
    if scopes is not None:
        wanted = {s.strip().lower() for s in scopes}
        scope_defs = [s for s in scope_defs if s["value"].strip().lower() in wanted]
    out = []
    for s in scope_defs:
        val = s["value"]
        scope_ids = store.scoped_doc_ids(any_value=val) or set()
        ents = _scoped_entities(entities, scope_ids)
        out.append({
            "scope": val,
            "field": s.get("field"),
            "graph_id": scope_graph_id(val),
            "doc_count": len(scope_ids),
            "entity_count": len(ents),
        })
    return out


# ── Build one scope's community graph (deterministic, no LLM) ─────────────────

def build_scope_graph(
    store,
    scope_value: str,
    resolution: float = 1.0,
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> dict:
    """Filter → sub-graph → Louvain → persist map + stats under the scope's
    graph_id. Returns the stats dict. No summaries here (that's the LLM step)."""
    gid = scope_graph_id(scope_value)
    scope_ids = store.scoped_doc_ids(any_value=scope_value) or set()

    ents = entities if entities is not None else store.get_entities()
    rels = relationships if relationships is not None else store.get_relationships()

    kept = _scoped_entities(ents, scope_ids)
    # build_graph drops edges whose endpoints aren't nodes, so passing all
    # relationships and only the kept entities yields exactly the sub-graph.
    G = build_graph(kept, rels)
    partition = detect_communities(G, resolution)
    community_map = build_community_map(kept, partition, G)
    store.save_community_map(community_map, graph_id=gid)

    stats = {
        "scope": scope_value,
        "graph_id": gid,
        "doc_count": len(scope_ids),
        "entities": len(kept),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "communities": len(community_map["communities"]),
        "resolution": resolution,
    }
    store.save_graph_stats(stats, graph_id=gid)
    return stats


# ── Build + summarise every scope (LLM — the costed step) ─────────────────────

async def build_all_scope_communities(
    store,
    settings: Settings,
    *,
    scopes: list[str] | None = None,
    resolution: float = 1.0,
    max_communities_per_scope: int | None = None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
) -> tuple[list[dict], bool]:
    """For every scope value: build its sub-graph, then summarise its communities.

    ``on_progress(done_scopes, total_scopes, scope_value, stage, detail)`` is
    called as work advances so the caller (a durable job) can stream live status:
      - stage='graph'   after the sub-graph is built (detail = stats dict)
      - stage='summary' as each community summary completes (detail = k/n)
      - stage='scope'   when a scope is fully done (detail = result dict)

    ``max_communities_per_scope`` caps summaries per scope so one very large
    scope can't dominate the run. Returns (per_scope_results, was_cancelled).
    """
    all_entities = store.get_entities()
    all_relationships = store.get_relationships()

    scope_list = scopes if scopes is not None else [s["value"] for s in store.list_scopes()]
    total = len(scope_list)
    results: list[dict] = []
    was_cancelled = False

    for i, scope_value in enumerate(scope_list):
        if cancel_event and cancel_event.is_set():
            was_cancelled = True
            break

        gid = scope_graph_id(scope_value)
        stats = build_scope_graph(
            store, scope_value, resolution, all_entities, all_relationships,
        )
        if on_progress:
            on_progress(i, total, scope_value, "graph", stats)

        # Summarise this scope's communities into its own graph_id.
        def _sub_progress(done, sub_total, comm_id, error, _sv=scope_value, _i=i):
            if on_progress:
                on_progress(_i, total, _sv, "summary",
                            {"done": done, "total": sub_total, "comm_id": comm_id, "error": error})

        summ_results, sub_cancelled = await summarise_corpus(
            store,
            model=settings.model_summary,
            max_concurrency=settings.max_llm_concurrency,
            max_communities=max_communities_per_scope,
            cancel_event=cancel_event,
            on_progress=_sub_progress,
            max_tokens=settings.max_summary_tokens,
            graph_id=gid,
        )

        result = {
            "scope": scope_value,
            "graph_id": gid,
            "doc_count": stats["doc_count"],
            "communities": stats["communities"],
            "summarised": sum(1 for r in summ_results if not r.get("error")),
            "errors": sum(1 for r in summ_results if r.get("error")),
        }
        results.append(result)
        if on_progress:
            on_progress(i + 1, total, scope_value, "scope", result)

        if sub_cancelled:
            was_cancelled = True
            break

    return results, was_cancelled


# ── Durable-job runner (emit + cancel, for the Community tab) ─────────────────

async def run_scope_community_build(
    *,
    emit=None,
    cancel_event: asyncio.Event | None = None,
    scopes: list[str] | None = None,
    max_communities_per_scope: int | None = None,
    resolution: float = 1.0,
) -> dict:
    """Job entry point: validate prereqs, then build + summarise every scope,
    streaming live progress through ``emit(message, progress=, stage=)``."""
    from app.config import get_settings
    from app.services.graph_store import get_graph_store

    store = get_graph_store()
    settings = get_settings()

    if store.count_entities() == 0:
        raise FileNotFoundError(
            "Data prep must complete before building per-scope communities — "
            "no entities in the store."
        )
    scope_defs = store.list_scopes()
    if not scope_defs:
        raise FileNotFoundError(
            "No metadata registry found — per-scope communities need Platform / "
            "Service Function tags. Run Data Prep against the tagged blob container first."
        )

    scope_list = scopes if scopes is not None else [s["value"] for s in scope_defs]
    total = len(scope_list)
    if emit:
        emit(f"Building per-scope communities for {total} scope(s)…",
             progress=0.01, stage="validate")

    def on_progress(done, total_scopes, scope_value, stage, detail):
        if not emit:
            return
        base = (done / total_scopes) if total_scopes else 0.0
        if stage == "graph":
            emit(
                f"[{done + 1}/{total_scopes}] {scope_value}: built "
                f"{detail['communities']} communities "
                f"({detail['entities']} entities, {detail['doc_count']} docs)",
                progress=min(0.99, base + 0.02 / max(total_scopes, 1)), stage="graph",
            )
        elif stage == "summary":
            d, t = detail["done"], detail["total"] or 1
            note = f"error: {detail['error']}" if detail.get("error") else "written"
            emit(
                f"[{done + 1}/{total_scopes}] {scope_value}: summary {d}/{t} {note}",
                progress=min(0.99, base + (d / t) / max(total_scopes, 1)), stage="summary",
            )
        elif stage == "scope":
            emit(
                f"[{done}/{total_scopes}] {scope_value}: done — "
                f"{detail['summarised']} summaries, {detail['errors']} errors",
                progress=min(0.99, done / max(total_scopes, 1)), stage="scope",
            )

    results, was_cancelled = await build_all_scope_communities(
        store, settings,
        scopes=scope_list,
        resolution=resolution,
        max_communities_per_scope=max_communities_per_scope,
        cancel_event=cancel_event,
        on_progress=on_progress,
    )

    ok = sum(r["summarised"] for r in results)
    errs = sum(r["errors"] for r in results)
    suffix = " (stopped early)" if was_cancelled else ""
    if emit:
        emit(f"Done{suffix}. {len(results)} scope(s), {ok} summaries written, {errs} errors.",
             progress=1.0, stage="done")
    return {"results": results, "was_cancelled": was_cancelled, "scopes": len(results)}


# ── Which scopes already have a built community graph ─────────────────────────

def built_scope_status(store) -> list[dict]:
    """For each scope: whether it has a per-scope community graph yet, plus its
    community/summary counts. Powers the build UI (what's done, what's pending)."""
    out = []
    for s in store.list_scopes():
        gid = scope_graph_id(s["value"])
        cmap = store.get_community_map(gid)
        n_comm = len(cmap.get("communities", {}))
        n_summ = len(store.summary_ok_ids(gid)) if n_comm else 0
        out.append({
            "scope": s["value"],
            "field": s.get("field"),
            "graph_id": gid,
            "doc_count": s.get("count"),
            "communities": n_comm,
            "summaries": n_summ,
            "built": n_comm > 0,
        })
    return out
