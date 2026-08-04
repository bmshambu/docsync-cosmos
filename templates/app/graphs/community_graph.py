"""LangGraph workflow for Step 2 — Community Summariser.

    validate_prereqs → summarise_communities

Prerequisites: community map, entities, relationships, and chunks must exist
in the GraphStore (i.e. Data Prep has run).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graphs.state import CommunitySummaryState
from app.llm.summarizer import summarise_corpus
from app.services.graph_store import get_graph_store


def _emit(state: CommunitySummaryState, message: str,
          progress: float | None = None, stage: str | None = None):
    fn = state.get("emit")
    if fn:
        fn(message, progress=progress, stage=stage)


# ── Node 1: validate prerequisites ───────────────────────────────────────────

def node_validate(state: CommunitySummaryState) -> dict:
    store = get_graph_store()
    community_map = store.get_community_map()
    if not community_map.get("communities") or store.count_entities() == 0:
        raise FileNotFoundError(
            "Data prep must complete before running the community summariser — "
            "no community map or entities found in the store."
        )
    if not store.has_chunks():
        raise FileNotFoundError("No chunks found in the store. Run Data Prep first.")

    comm_ids = list(community_map.get("communities", {}).keys())
    selected = state.get("selected_communities")
    max_c = state.get("max_communities")
    if selected:
        label = f"{len(selected)} selected"
    elif max_c:
        label = f"first {min(max_c, len(comm_ids))}"
    else:
        label = str(len(comm_ids))
    _emit(state,
          f"Prerequisites OK. Summarising {label} of {len(comm_ids)} communities…",
          progress=0.02, stage="validate")
    return {"community_ids": comm_ids}


# ── Node 2: summarise all communities (LLM) ──────────────────────────────────

async def node_summarise(state: CommunitySummaryState) -> dict:
    settings = get_settings()
    total = len(state.get("community_ids", []))

    def on_progress(done, total, comm_id, error):
        frac = 0.05 + 0.90 * (done / total) if total else 0.95
        note = f"ERROR: {error}" if error else "written"
        _emit(state,
              f"[{done}/{total}] Community {comm_id} — {note}",
              progress=frac, stage="summarise")

    _emit(state, f"Calling {settings.active_model_label} for each community…", progress=0.05, stage="summarise")

    results, was_cancelled = await summarise_corpus(
        store=get_graph_store(),
        model=settings.model_summary,
        max_concurrency=settings.max_llm_concurrency,
        max_communities=state.get("max_communities"),
        selected_communities=state.get("selected_communities"),
        cancel_event=state.get("cancel_event"),
        on_progress=on_progress,
        max_tokens=settings.max_summary_tokens,
    )

    errors = [{"comm_id": r["comm_id"], "error": r["error"]}
              for r in results if r.get("error")]

    ok_count = len(results) - len(errors)
    suffix = " (stopped early)" if was_cancelled else ""
    _emit(state,
          f"Done{suffix}. {ok_count} summaries written, {len(errors)} errors.",
          progress=1.0, stage="done")

    return {"results": results, "was_cancelled": was_cancelled, "errors": errors}


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_community_graph():
    g = StateGraph(CommunitySummaryState)
    g.add_node("validate", node_validate)
    g.add_node("summarise", node_summarise)
    g.add_edge(START, "validate")
    g.add_edge("validate", "summarise")
    g.add_edge("summarise", END)
    return g.compile()


COMMUNITY_GRAPH = build_community_graph()


async def run_community_summary(
    emit=None,
    cancel_event=None,
    max_communities: int | None = None,
    selected_communities: list[str] | None = None,
) -> dict:
    initial: CommunitySummaryState = {
        "emit": emit,
        "cancel_event": cancel_event,
        "max_communities": max_communities,
        "selected_communities": selected_communities,
    }
    return await COMMUNITY_GRAPH.ainvoke(initial)
