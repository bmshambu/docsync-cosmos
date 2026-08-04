"""M1 — sync blob metadata into the document registry (no LLM, no re-extraction).

Reads the metadata key-values off every blob in the configured container and
writes a document registry (doc_id -> platform / service_functions / title /
webUrl / is_deleted / ...) into the active GraphStore. This is what makes
service-function scoping work on ALREADY-EXTRACTED docs — the paid-for
extraction is never touched; only the registry is (re)built.

Run this:
  - after the big extraction of the office corpus, OR
  - any time metadata is re-tagged in Templafy (cheap refresh)

Usage (from the cosmos-rag folder):
    ..\\.venv\\Scripts\\python.exe -m scripts.sync_metadata
    ..\\.venv\\Scripts\\python.exe -m scripts.sync_metadata --container gps-test-new
    ..\\.venv\\Scripts\\python.exe -m scripts.sync_metadata --verify-only
"""

from __future__ import annotations

import sys
import time
from collections import Counter

from app.config import get_settings
from app.services.graph_store import get_graph_store
from app.services.storage import blob_metadata_map


def _arg(name, default=None):
    a = sys.argv[1:]
    return (a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default) \
        if name in a else default


def main() -> int:
    args = set(sys.argv[1:])
    settings = get_settings()
    store = get_graph_store()

    print(f"backend  : {settings.storage_backend}")
    if settings.storage_backend == "cosmos":
        print(f"cosmos   : {settings.cosmos_endpoint} / {settings.cosmos_database}")

    if "--verify-only" in args:
        reg = store.get_doc_registry()
        deleted = sum(1 for r in reg.values() if r.get("is_deleted"))
        print(f"\nregistry records : {len(reg)}")
        print(f"  is_deleted     : {deleted}")
        print(f"  platforms      : {[(p['value'], p['count']) for p in store.list_platforms()]}")
        from app.services.metadata import CLIENTS_AND_MARKETS
        cm = sum(1 for r in reg.values()
                 if not r.get("is_deleted") and CLIENTS_AND_MARKETS in (r.get("service_functions") or []))
        print(f"  clients&markets: {cm}")
        return 0

    container = _arg("--container") or settings.azure_storage_container_name
    print(f"container: {container}\n")
    print("reading blob metadata (one listing pass) ...")
    t0 = time.time()
    meta = blob_metadata_map(container)
    if not meta:
        print("No blob metadata found. Check the container / connection string.")
        return 1

    records = list(meta.values())
    # Only keep registry records for docs that are actually in the graph
    # (extracted). Untracked blobs are ignored — they'd never be retrieved.
    known = {d["doc_id"] for d in store.list_doc_titles()}
    matched = [r for r in records if r["doc_id"] in known]
    unmatched = len(records) - len(matched)

    print(f"blobs with metadata : {len(records)}")
    print(f"matched to graph    : {len(matched)}")
    if unmatched:
        print(f"in blob but NOT extracted (ignored): {unmatched}")
    if not matched:
        print("\nNo overlap between blob docs and extracted docs — run Data Prep first, "
              "or check you are pointed at the right container.")
        return 1

    store.save_doc_registry(matched)

    plat = Counter()
    deleted = 0
    from app.services.metadata import CLIENTS_AND_MARKETS
    cm = 0
    for r in matched:
        if r.get("is_deleted"):
            deleted += 1
            continue
        for p in (r.get("platform") or []):
            plat[p] += 1
        if CLIENTS_AND_MARKETS in (r.get("service_functions") or []):
            cm += 1

    print(f"\nregistry written in {time.time() - t0:.1f}s")
    print(f"  platforms       : {dict(plat)}")
    print(f"  clients&markets : {cm}")
    print(f"  is_deleted      : {deleted} (excluded from scopes)")
    print("\nScoping is now live. The Query/Batch tabs' Platform dropdown reads from this registry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
