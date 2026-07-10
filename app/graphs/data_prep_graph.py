"""LangGraph workflow for Step 1 — Data Prep.

    extract_text → extract_entities (LLM) → build_graph → generate_html

Supports:
  max_docs   — process only the first N documents
  cancel_event — asyncio.Event set by the Stop button; partial results are kept
"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graphs.state import DataPrepState
from app.llm.extractor import extract_corpus
from app.services import extract as extract_svc
from app.services import graph_build, graph_html, storage
from app.services.graph_store import get_graph_store


def _emit(state: DataPrepState, message: str, progress: float | None = None, stage: str | None = None):
    fn = state.get("emit")
    if fn:
        fn(message, progress=progress, stage=stage)


# ── Node 1: text extraction ───────────────────────────────────────────────────

def node_extract_text(state: DataPrepState) -> dict:
    settings = get_settings()
    settings.ensure_dirs()
    folder = state["folder_path"]
    max_docs = state.get("max_docs")

    blob_folders = state.get("blob_folders")
    blob_container = state.get("blob_container")
    where = folder or " · ".join(filter(None, [
        f"container: {blob_container}" if blob_container else "Azure Blob container",
        f"folders: {', '.join(f or '(root)' for f in blob_folders)}" if blob_folders else "",
    ]))
    _emit(state, f"Listing documents in {where} …", progress=0.02, stage="extract_text")
    source = storage.get_source(folder, prefixes=blob_folders, container=blob_container)
    all_paths = source.list_documents()
    if not all_paths:
        raise ValueError(f"No supported documents (.pdf/.docx/.pptx) found in {folder or 'the blob container'}")

    selected = state.get("selected_files")
    if selected:
        sel = set(selected)
        doc_paths = [p for p in all_paths if p.name in sel]
        if not doc_paths:
            raise ValueError("None of the selected files were found in the source.")
        label = f"{len(doc_paths)} selected"
    else:
        doc_paths = all_paths[:max_docs] if max_docs else all_paths
        label = f"first {len(doc_paths)}" if max_docs and len(doc_paths) < len(all_paths) else str(len(doc_paths))
    _emit(state, f"Processing {label} of {len(all_paths)} document(s). Extracting text + chunks …",
          progress=0.05, stage="extract_text")

    def on_progress(done, total, result):
        frac = 0.05 + 0.20 * (done / total)
        name = result.get("filename", "?")
        note = "skipped" if result.get("skipped") else f"{result.get('chunks', 0)} chunks"
        if result.get("error"):
            note = f"ERROR: {result['error']}"
        _emit(state, f"[{done}/{total}] {name} — {note}", progress=frac, stage="extract_text")

    # Force re-extract overrides skip so updated blob content is re-chunked
    skip = state.get("skip_existing", True) and not state.get("force_reextract")
    results = extract_svc.extract_all(
        doc_paths,
        text_dir=settings.extracted_text_dir,
        chunks_dir=settings.chunks_dir,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        skip_existing=skip,
        on_progress=on_progress,
    )

    # Persist freshly-chunked docs through the GraphStore so non-file backends
    # (Cosmos) receive them. Chunk files remain the local pipeline scratch.
    store = get_graph_store()
    for r in results:
        if r.get("error") or r.get("skipped") or not r.get("doc_id"):
            continue
        chunk_file = settings.chunks_dir / f"{r['doc_id']}_chunks.json"
        if chunk_file.exists():
            store.save_doc_chunks(r["doc_id"], json.loads(chunk_file.read_text(encoding="utf-8")))

    return {"doc_paths": [str(p) for p in doc_paths], "extract_results": results}


# ── Node 2: entity/relationship extraction (LLM) ──────────────────────────────

async def node_extract_entities(state: DataPrepState) -> dict:
    settings = get_settings()
    force_files = state.get("selected_files") if state.get("force_reextract") else None
    if force_files:
        _emit(state, f"Force re-extract: purging old entities for {len(force_files)} doc(s) first …",
              progress=0.26, stage="extract_entities")
    _emit(state, f"Extracting entities + relationships with {settings.active_model_label} …",
          progress=0.27, stage="extract_entities")

    def on_progress(done, total, fname, error):
        if total == 0:
            _emit(state, f"{fname}", progress=0.83, stage="extract_entities")
            return
        frac = 0.27 + 0.55 * (done / total)
        note = f"ERROR: {error}" if error else "ok"
        _emit(state, f"[{done}/{total}] {fname} — {note}", progress=frac, stage="extract_entities")

    entities, relationships, per_doc, was_cancelled = await extract_corpus(
        store=get_graph_store(),
        model=settings.model_extract,
        max_concurrency=settings.max_llm_concurrency,
        max_docs=state.get("max_docs"),
        selected_files=state.get("selected_files"),
        force_files=force_files,
        cancel_event=state.get("cancel_event"),
        on_progress=on_progress,
        max_tokens=settings.max_extract_tokens,
    )

    errors = [{"filename": d["filename"], "error": d["error"]} for d in per_doc if d.get("error")]
    msg = (
        f"Extraction stopped early. {len(entities)} entities, {len(relationships)} relationships saved."
        if was_cancelled
        else f"Extracted {len(entities)} entities, {len(relationships)} relationships."
    )
    _emit(state, msg, progress=0.83, stage="extract_entities")
    return {
        "entities_count": len(entities),
        "relationships_count": len(relationships),
        "per_doc_errors": errors,
        "was_cancelled": was_cancelled,
    }


# ── Node 3: build graph + communities ─────────────────────────────────────────

def node_build_graph(state: DataPrepState) -> dict:
    store = get_graph_store()
    _emit(state, "Building knowledge graph + detecting communities …",
          progress=0.86, stage="build_graph")

    if store.count_entities() == 0:
        _emit(state, "No entities to graph — skipping.", progress=0.92, stage="build_graph")
        return {"stats": {"nodes": 0, "edges": 0, "communities": 0}}

    stats = graph_build.build_and_save(store, resolution=state.get("resolution", 1.0))
    _emit(state, f"Graph: {stats['nodes']} nodes, {stats['edges']} edges, "
                 f"{stats['communities']} communities.", progress=0.92, stage="build_graph")
    return {"stats": stats}


# ── Node 4: generate interactive HTML ─────────────────────────────────────────

def node_generate_html(state: DataPrepState) -> dict:
    settings = get_settings()
    stats = state.get("stats", {})
    if not stats.get("nodes"):
        _emit(state, "No graph to visualise — skipping HTML generation.", progress=1.0, stage="done")
        return {"html_path": ""}

    _emit(state, "Generating interactive graph visualisation …",
          progress=0.95, stage="generate_html")
    snapshot = get_graph_store().export_snapshot()
    out = graph_html.generate_graph_html(
        entities_file=snapshot["entities_file"],
        relationships_file=snapshot["relationships_file"],
        community_map_file=snapshot["community_map_file"],
        communities_dir=snapshot["communities_dir"],
        out_file=settings.graph_html_file,
    )
    suffix = " (partial — stopped early)" if state.get("was_cancelled") else ""
    _emit(state, f"Data prep complete{suffix}.", progress=1.0, stage="done")
    return {"html_path": str(out)}


# ── Graph assembly ────────────────────────────────────────────────────────────

def build_data_prep_graph():
    g = StateGraph(DataPrepState)
    g.add_node("extract_text", node_extract_text)
    g.add_node("extract_entities", node_extract_entities)
    g.add_node("build_graph", node_build_graph)
    g.add_node("generate_html", node_generate_html)
    g.add_edge(START, "extract_text")
    g.add_edge("extract_text", "extract_entities")
    g.add_edge("extract_entities", "build_graph")
    g.add_edge("build_graph", "generate_html")
    g.add_edge("generate_html", END)
    return g.compile()


DATA_PREP_GRAPH = build_data_prep_graph()


async def run_data_prep(
    folder_path: str,
    emit=None,
    cancel_event=None,
    resolution: float = 1.0,
    skip_existing: bool = True,
    max_docs: int | None = None,
    selected_files: list[str] | None = None,
    force_reextract: bool = False,
    blob_folders: list[str] | None = None,
    blob_container: str | None = None,
) -> dict:
    initial: DataPrepState = {
        "folder_path": folder_path,
        "emit": emit,
        "cancel_event": cancel_event,
        "resolution": resolution,
        "skip_existing": skip_existing,
        "max_docs": max_docs,
        "selected_files": selected_files,
        "force_reextract": force_reextract,
        "blob_folders": blob_folders,
        "blob_container": blob_container,
    }
    return await DATA_PREP_GRAPH.ainvoke(initial)


# ── Build-only (no LLM re-extraction) ────────────────────────────────────────

async def run_build_only(
    emit=None,
    resolution: float = 1.0,
) -> dict:
    """Rebuild graph + HTML from existing entities without re-running the LLM."""
    settings = get_settings()
    store = get_graph_store()

    def _e(msg, progress=None, stage=None):
        if emit:
            emit(msg, progress=progress, stage=stage)

    _e("Building knowledge graph + detecting communities …", progress=0.1, stage="build_graph")

    if store.count_entities() == 0:
        _e("No entities found — run full data prep first.", progress=1.0, stage="done")
        return {"stats": {}, "html_path": "", "was_cancelled": False,
                "entities_count": 0, "relationships_count": 0}

    stats = graph_build.build_and_save(store, resolution=resolution)
    _e(f"Graph: {stats['nodes']} nodes, {stats['edges']} edges, "
       f"{stats['communities']} communities.", progress=0.6, stage="build_graph")

    _e("Generating interactive graph visualisation …", progress=0.7, stage="generate_html")
    snapshot = store.export_snapshot()
    out = graph_html.generate_graph_html(
        entities_file=snapshot["entities_file"],
        relationships_file=snapshot["relationships_file"],
        community_map_file=snapshot["community_map_file"],
        communities_dir=snapshot["communities_dir"],
        out_file=settings.graph_html_file,
    )

    _e("Graph rebuild complete.", progress=1.0, stage="done")
    return {
        "stats": stats,
        "html_path": str(out),
        "was_cancelled": False,
        "entities_count": stats.get("entities", 0),
        "relationships_count": stats.get("relationships", 0),
    }
