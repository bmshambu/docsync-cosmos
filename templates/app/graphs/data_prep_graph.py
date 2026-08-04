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
    all_names = source.list_document_names()   # names only — no download
    if not all_names:
        raise ValueError(f"No supported documents (.pdf/.docx/.pptx) found in {folder or 'the blob container'}")

    # Blob metadata (M1 scoping): fetch the doc->metadata map once, drop
    # IsDeleted docs from ingestion, and keep the map to build the registry.
    meta_map: dict[str, dict] = {}
    settings2 = get_settings()
    if settings2.blob_mode:
        try:
            meta_map = storage.blob_metadata_map(blob_container)
        except Exception as exc:
            _emit(state, f"(metadata unavailable: {exc})", stage="extract_text")
        if meta_map:
            before = len(all_names)
            all_names = [
                n for n in all_names
                if not (meta_map.get(storage._doc_id_for(n)) or {}).get("is_deleted")
            ]
            dropped = before - len(all_names)
            if dropped:
                _emit(state, f"Skipping {dropped} IsDeleted document(s).", stage="extract_text")

    selected = state.get("selected_files")
    if selected:
        sel = set(selected)
        doc_names = [n for n in all_names if n in sel]
        if not doc_names:
            raise ValueError("None of the selected files were found in the source.")
        label = f"{len(doc_names)} selected"
    else:
        doc_names = all_names[:max_docs] if max_docs else all_names
        label = f"first {len(doc_names)}" if max_docs and len(doc_names) < len(all_names) else str(len(doc_names))
    _emit(state, f"Processing {label} of {len(all_names)} document(s). Streaming text + chunks …",
          progress=0.05, stage="extract_text")

    def on_progress(done, total, result):
        frac = 0.05 + 0.20 * (done / total)
        name = result.get("filename", "?")
        note = "skipped" if result.get("skipped") else f"{result.get('chunks', 0)} chunks"
        if result.get("error"):
            note = f"ERROR: {result['error']}"
        _emit(state, f"[{done}/{total}] {name} — {note}", progress=frac, stage="extract_text")

    # Force re-extract overrides skip so updated blob content is re-chunked.
    # Documents are STREAMED from the source into memory — no blob cache on disk.
    skip = state.get("skip_existing", True) and not state.get("force_reextract")
    results = extract_svc.extract_all_streamed(
        doc_names,
        reader=source.read_document_bytes,
        text_dir=settings.extracted_text_dir,
        chunks_dir=settings.chunks_dir,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
        skip_existing=skip,
        on_progress=on_progress,
    )

    # Persist freshly-chunked docs through the GraphStore. Chunks come straight
    # from the result (no file read-back).
    store = get_graph_store()
    for r in results:
        if r.get("error") or r.get("skipped") or not r.get("doc_id"):
            continue
        chunks = r.get("chunks_data")
        if chunks is None:
            cf = settings.chunks_dir / f"{r['doc_id']}_chunks.json"
            chunks = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else []
        store.save_doc_chunks(r["doc_id"], chunks)

    # Write the metadata registry for every processed doc (M1 scoping).
    if meta_map:
        recs = [meta_map[storage._doc_id_for(n)]
                for n in doc_names if storage._doc_id_for(n) in meta_map]
        if recs:
            store.save_doc_registry(recs)
            _emit(state, f"Metadata registry written for {len(recs)} doc(s).",
                  stage="extract_text")

    return {"doc_paths": list(doc_names), "extract_results": results}


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
    # The interactive graph is now generated ON DEMAND, per scope, by
    # /api/data-prep/graph-html. The old whole-corpus static file was ~22k nodes
    # and froze the browser (rendered blank), so nothing is pre-generated here.
    suffix = " (partial — stopped early)" if state.get("was_cancelled") else ""
    _emit(state, f"Data prep complete{suffix}.", progress=1.0, stage="done")
    return {"html_path": ""}


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

    # Interactive graph is generated on demand per scope by the graph-html
    # endpoint (whole-corpus static generation was the blank-graph culprit).
    _e("Graph rebuild complete.", progress=1.0, stage="done")
    return {
        "stats": stats,
        "html_path": "",
        "was_cancelled": False,
        "entities_count": stats.get("entities", 0),
        "relationships_count": stats.get("relationships", 0),
    }
