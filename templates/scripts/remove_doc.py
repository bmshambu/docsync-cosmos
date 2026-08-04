"""Remove document(s) from the knowledge graph — without a full re-extraction.

Use case: a document was ingested that doesn't belong in the answer corpus
(e.g. a client RFP uploaded into the responses folder). This purges its
entities, relationships, and chunks from the active GraphStore backend,
then rebuilds the graph. Your paid-for extraction of every OTHER document
is untouched.

What it does per document:
  1. Entities: filename removed from source_docs; entities whose ONLY source
     was this doc are deleted; relationships from this doc (or pointing at a
     deleted entity) are dropped.
  2. Chunks: the document's chunk partition is emptied.
  3. Local scratch (extracted_text/, chunks/) cleaned so a future Data Prep
     run re-processes the file if it is ever legitimately re-added.
  4. Graph + stats + HTML rebuilt (no LLM calls).

AFTERWARDS (manual):
  - Delete the file from the blob folder too, or the next full Data Prep run
    will re-ingest it.
  - Re-run the Community Summariser (Select all) — clustering changed.

Usage (from the cosmos-rag folder):
    ..\\.venv\\Scripts\\python.exe -m scripts.remove_doc "clientname_oracle_rfp.docx"
    ..\\.venv\\Scripts\\python.exe -m scripts.remove_doc file1.docx file2.pptx --yes
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.llm.extractor import purge_doc_data
from app.services.graph_store import get_graph_store


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print('usage: python -m scripts.remove_doc "<filename.docx>" [more files] [--yes]')
        return 1

    settings = get_settings()
    store = get_graph_store()

    print(f"backend : {settings.storage_backend}")
    if settings.storage_backend == "cosmos":
        print(f"target  : {settings.cosmos_endpoint} / {settings.cosmos_database}")

    # Resolve requested filenames against the corpus (case-insensitive)
    titles = store.list_doc_titles()
    by_name = {t["filename"].lower(): t for t in titles}
    targets: list[dict] = []
    for name in args:
        t = by_name.get(name.lower())
        if not t:
            close = [x["filename"] for x in titles if name.lower()[:12] in x["filename"].lower()]
            print(f"NOT FOUND in corpus: {name}")
            if close:
                print(f"  did you mean: {close[:5]}")
            return 1
        targets.append(t)

    filenames = {t["filename"] for t in targets}

    # Preview the purge impact before touching anything
    entities = store.get_entities()
    relationships = store.get_relationships()
    kept_e, kept_r = purge_doc_data(entities, relationships, filenames)
    n_chunks = sum(1 for c in store.iter_chunks() if c.get("filename") in filenames)

    print(f"\nremoving {len(targets)} document(s):")
    for t in targets:
        print(f"  - {t['filename']}  (doc_id: {t['doc_id']})")
    print(f"\nimpact: entities {len(entities)} -> {len(kept_e)} "
          f"| relationships {len(relationships)} -> {len(kept_r)} "
          f"| chunks removed: {n_chunks}")

    if "--yes" not in flags:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted — nothing changed")
            return 1

    # 1. entities + relationships
    store.save_extraction(kept_e, kept_r)
    print("entities/relationships purged")

    # 2. chunks (empty list deletes the partition's items on Cosmos)
    for t in targets:
        store.save_doc_chunks(t["doc_id"], [])
    print("chunks removed")

    # 3. local scratch so skip-logic can't resurrect stale artefacts
    for t in targets:
        for p in (settings.chunks_dir / f"{t['doc_id']}_chunks.json",
                  settings.extracted_text_dir / f"{t['doc_id']}.txt"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    print("local scratch cleaned")

    # 4. rebuild graph + stats + HTML (no LLM)
    from app.graphs.data_prep_graph import run_build_only
    res = asyncio.run(run_build_only())
    stats = res.get("stats", {})
    print(f"graph rebuilt: {stats.get('nodes', 0)} nodes, "
          f"{stats.get('communities', 0)} communities")

    print("\nREMEMBER:")
    print("  1. Delete the file from the blob folder too, or the next full "
          "Data Prep run will re-ingest it.")
    print("  2. Re-run the Community Summariser (Select all) — clustering changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
