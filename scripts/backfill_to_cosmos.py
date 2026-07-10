"""Backfill: copy the file-based graph artefacts (data/ folder) into Cosmos DB.

Reads via FileGraphStore, writes via CosmosGraphStore — both constructed
directly, so STORAGE_BACKEND in .env does not matter while running this.
The TARGET account is whatever COSMOS_ENDPOINT / COSMOS_KEY / COSMOS_DATABASE
point at in .env — when moving to the office account, change those three
values and re-run.

Usage (from the cosmos-rag folder):
    ..\\.venv\\Scripts\\python.exe -m scripts.backfill_to_cosmos                # backfill + verify
    ..\\.venv\\Scripts\\python.exe -m scripts.backfill_to_cosmos --verify-only  # counts only, no writes
    ..\\.venv\\Scripts\\python.exe -m scripts.backfill_to_cosmos --yes          # skip the confirm prompt
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict

from app.config import get_settings
from app.services.graph_store import CosmosGraphStore, FileGraphStore


def verify(dst: CosmosGraphStore) -> None:
    print("\nverify from Cosmos:")
    print(f"  entities:      {dst.count_entities()}")
    print(f"  relationships: {len(dst.get_relationships())}")
    print(f"  chunks:        {sum(1 for _ in dst.iter_chunks())}")
    print(f"  communities:   {len(dst.get_community_map().get('communities', {}))}")
    print(f"  summaries:     {len(dst.list_community_summaries())}")
    stats = dst.get_graph_stats()
    print(f"  graph stats:   {stats if stats else '(none)'}")


def main() -> int:
    args = set(sys.argv[1:])
    settings = get_settings()

    print(f"target : {settings.cosmos_endpoint}")
    print(f"database: {settings.cosmos_database}")

    dst = CosmosGraphStore(settings)

    if "--verify-only" in args:
        verify(dst)
        return 0

    src = FileGraphStore(settings)
    entities = src.get_entities()
    relationships = src.get_relationships()
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for c in src.iter_chunks():
        by_doc[c.get("doc_id", "unknown")].append(c)
    community_map = src.get_community_map()

    print(f"\nsource (data/ folder): {len(entities)} entities · {len(relationships)} relationships "
          f"· {sum(len(v) for v in by_doc.values())} chunks in {len(by_doc)} docs "
          f"· {len(community_map.get('communities', {}))} communities")

    if not entities and not by_doc:
        print("\nNothing to backfill — the local data/ folder has no extracted artefacts. "
              "Run Data Prep first (with STORAGE_BACKEND=file) or check DATA_DIR in .env.")
        return 1

    if "--yes" not in args:
        answer = input(f"\nBackfill this into {settings.cosmos_endpoint} ? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted — nothing written")
            return 1

    t0 = time.time()

    print(f"entities + relationships … ", end="", flush=True)
    dst.save_extraction(entities, relationships)
    print("done")

    print(f"chunks ({len(by_doc)} docs) … ", end="", flush=True)
    for doc_id, chunks in by_doc.items():
        dst.save_doc_chunks(doc_id, chunks)
    print("done")

    n_summaries = 0
    if community_map:
        dst.save_community_map(community_map)
        for cid in community_map.get("communities", {}):
            text = src.get_community_summary(cid)
            if text:
                dst.save_community_summary(cid, text)
                n_summaries += 1
    stats = src.get_graph_stats()
    if stats:
        dst.save_graph_stats(stats)
    print(f"communities: map={'yes' if community_map else 'no'} | summaries: {n_summaries} "
          f"| stats: {'yes' if stats else 'no'}")

    verify(dst)
    print(f"\nbackfill complete in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
