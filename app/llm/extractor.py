"""LLM-driven entity/relationship extraction.

Fixes vs original:
  - Gemini JSON mode (response_mime_type=application/json) prevents malformed output.
  - json-repair as a fallback for any remaining parse errors.
  - max_docs: slice corpus to process only the first N documents.
  - cancel_event: asyncio.Event — set by the Stop button; extraction stops between
    docs, partial results are written to disk, and the graph still builds.
  - Incremental write: entities/relationships flushed to disk after every document
    so a cancelled run always leaves a valid (partial) graph on disk.
  - Skip already-extracted docs: if entities_file already contains entries from a
    document (matched via source_docs), that document is not re-sent to the LLM.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import json_repair
from langchain_core.messages import HumanMessage, SystemMessage

from app.llm.client import get_chat
from app.llm.prompts import build_extraction_prompt

_MAX_WORDS = 24_000


def _page_marked_text(chunks: list[dict]) -> str:
    parts: list[str] = []
    last_page = None
    for c in chunks:
        page = c.get("page_start")
        if page != last_page:
            parts.append(f"\n[page={page}]")
            last_page = page
        parts.append(c.get("text", ""))
    text = " ".join(parts)
    words = text.split()
    return " ".join(words[:_MAX_WORDS]) if len(words) > _MAX_WORDS else text


def _parse_json_object(raw: str) -> dict:
    """Robust JSON parse with three fallback layers."""
    raw = raw.strip()
    # Strip markdown fences (Gemini sometimes adds them despite JSON mode)
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()

    # Layer 1: strict parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Layer 2: json-repair (handles missing/trailing commas, unquoted keys, etc.)
    try:
        repaired = json_repair.repair_json(raw, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
    except Exception:
        pass

    # Layer 3: extract outermost {...} span and strict-parse that
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse JSON from model output (first 300 chars): {raw[:300]}")


async def extract_one(
    filename: str,
    chunks: list[dict],
    model: str,
    semaphore: asyncio.Semaphore,
    max_tokens: int = 16000,
) -> dict:
    system, user = build_extraction_prompt(filename, _page_marked_text(chunks))
    chat = get_chat(model, temperature=0.0, max_tokens=max_tokens, json_mode=True)

    async with semaphore:
        resp = await chat.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )

    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = _parse_json_object(content)
    return {
        "filename": filename,
        "entities": data.get("entities") or [],
        "relationships": data.get("relationships") or [],
    }


def merge_entities(per_doc: list[dict]) -> tuple[list[dict], list[dict]]:
    entities_by_id: dict[str, dict] = {}
    relationships: list[dict] = []
    for doc_result in per_doc:
        for e in doc_result.get("entities") or []:
            eid = e.get("id")
            if not eid:
                continue
            if eid not in entities_by_id:
                entities_by_id[eid] = {
                    "id": eid,
                    "name": e.get("name", eid),
                    "type": e.get("type", "unknown"),
                    "aliases": list(e.get("aliases") or []),
                    "source_docs": list(e.get("source_docs") or []),
                    "attributes": dict(e.get("attributes") or {}),
                }
            else:
                ex = entities_by_id[eid]
                ex["aliases"] = sorted(set(ex["aliases"]) | set(e.get("aliases") or []))
                ex["source_docs"] = sorted(set(ex["source_docs"]) | set(e.get("source_docs") or []))
                ex["attributes"].update(e.get("attributes") or {})
        relationships.extend(doc_result.get("relationships") or [])
    return list(entities_by_id.values()), relationships


def _save_incremental(
    per_doc: list[dict],
    store,
    extra_entities: list[dict] | None = None,
    extra_relationships: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Merge per_doc results with any pre-existing (already-extracted) data and persist."""
    new_entities, new_relationships = merge_entities(per_doc)

    if extra_entities:
        # Merge new entities into the existing set
        entities_by_id: dict[str, dict] = {e["id"]: dict(e) for e in extra_entities}
        for e in new_entities:
            eid = e.get("id")
            if not eid:
                continue
            if eid in entities_by_id:
                ex = entities_by_id[eid]
                ex["aliases"] = sorted(set(ex.get("aliases") or []) | set(e.get("aliases") or []))
                ex["source_docs"] = sorted(set(ex.get("source_docs") or []) | set(e.get("source_docs") or []))
                ex["attributes"] = {**(ex.get("attributes") or {}), **(e.get("attributes") or {})}
            else:
                entities_by_id[eid] = e
        entities = list(entities_by_id.values())

        # Deduplicate relationships by (source, target, relation_type, source_doc)
        seen_rels: set = set()
        all_rels: list[dict] = []
        for r in (extra_relationships or []) + new_relationships:
            key = (r.get("source"), r.get("target"), r.get("relation_type"), r.get("source_doc"))
            if key not in seen_rels:
                seen_rels.add(key)
                all_rels.append(r)
        relationships = all_rels
    else:
        entities, relationships = new_entities, new_relationships

    store.save_extraction(entities, relationships)
    return entities, relationships


def purge_doc_data(
    entities: list[dict],
    relationships: list[dict],
    filenames: set[str],
) -> tuple[list[dict], list[dict]]:
    """Remove all traces of the given documents from entities/relationships.

    - Filenames are removed from each entity's source_docs.
    - Entities whose ONLY sources were the purged docs are dropped.
    - Relationships extracted from the purged docs are dropped, as are
      relationships that now point at a dropped entity.
    """
    kept_entities: list[dict] = []
    for e in entities:
        srcs = [s for s in (e.get("source_docs") or []) if s not in filenames]
        if srcs:
            kept_entities.append({**e, "source_docs": srcs})

    kept_ids = {e["id"] for e in kept_entities}
    kept_rels = [
        r for r in relationships
        if r.get("source_doc") not in filenames
        and r.get("source") in kept_ids
        and r.get("target") in kept_ids
    ]
    return kept_entities, kept_rels


async def extract_corpus(
    store,
    model: str,
    max_concurrency: int,
    max_docs: int | None = None,
    selected_files: list[str] | None = None,
    force_files: list[str] | None = None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    max_tokens: int = 16000,
) -> tuple[list[dict], list[dict], list[dict], bool]:
    """Extract entities/relationships for every (or selected/up to max_docs) documents.

    All persistence goes through the GraphStore. Skips documents whose entities
    already exist (matched via source_docs); saves incrementally after each doc.
    Returns (entities, relationships, per_doc_results, was_cancelled).
    """
    chunks_by_doc = store.chunks_by_filename()
    filenames = list(chunks_by_doc.keys())
    if selected_files:
        sel = set(selected_files)
        filenames = [f for f in filenames if f in sel]
    elif max_docs:
        filenames = filenames[:max_docs]

    # Existing state (for skip logic)
    existing_entities: list[dict] = store.get_entities()
    existing_relationships: list[dict] = store.get_relationships()

    # Force re-extract: purge the old data for these docs so the skip logic
    # treats them as new. Persisted immediately so the state is consistent.
    if force_files:
        existing_entities, existing_relationships = purge_doc_data(
            existing_entities, existing_relationships, set(force_files)
        )
        store.save_extraction(existing_entities, existing_relationships)

    already_extracted: set[str] = set()
    for e in existing_entities:
        for fname in (e.get("source_docs") or []):
            already_extracted.add(fname)

    # Only process docs not already extracted
    new_filenames = [f for f in filenames if f not in already_extracted]
    skipped_count = len(filenames) - len(new_filenames)

    if not new_filenames:
        if on_progress:
            on_progress(0, 0, f"(all {skipped_count} doc(s) already extracted — skipped)", None)
        return existing_entities, existing_relationships, [], False

    total = len(new_filenames)
    semaphore = asyncio.Semaphore(max_concurrency)
    per_doc: list[dict] = []
    done = 0
    was_cancelled = False
    entities: list[dict] = list(existing_entities)
    relationships: list[dict] = list(existing_relationships)

    async def _run(fname: str) -> dict:
        try:
            return await extract_one(fname, chunks_by_doc[fname], model, semaphore, max_tokens=max_tokens)
        except Exception as exc:
            return {"filename": fname, "entities": [], "relationships": [], "error": str(exc)}

    tasks = {asyncio.create_task(_run(f)): f for f in new_filenames}

    for coro in asyncio.as_completed(list(tasks)):
        # Check for cancellation before accepting each result
        if cancel_event and cancel_event.is_set():
            was_cancelled = True
            for t in tasks:
                t.cancel()
            break

        result = await coro
        per_doc.append(result)
        done += 1

        # Merge with pre-existing entities and persist incrementally
        entities, relationships = _save_incremental(
            per_doc, store,
            extra_entities=existing_entities,
            extra_relationships=existing_relationships,
        )

        if on_progress:
            on_progress(done, total, result.get("filename"), result.get("error"))

    # If we finished all queued docs (cancel fired after last one completed), it's not partial
    if done >= total:
        was_cancelled = False

    return entities, relationships, per_doc, was_cancelled
