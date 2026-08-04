"""Clear Cosmos DB containers — for a fresh backfill or a clean re-extraction.

PERMANENTLY deletes items from the graph containers in whatever account
COSMOS_ENDPOINT / COSMOS_KEY / COSMOS_DATABASE point at in .env. The blob
documents are untouched — everything in Cosmos is derived data and can be
rebuilt (backfill script or a fresh Data Prep run).

Usage (from the cosmos-rag folder):
    ..\\.venv\\Scripts\\python.exe -m scripts.clear_cosmos --counts-only
    ..\\.venv\\Scripts\\python.exe -m scripts.clear_cosmos                       # asks to type 'delete'
    ..\\.venv\\Scripts\\python.exe -m scripts.clear_cosmos --yes                 # no prompt
    ..\\.venv\\Scripts\\python.exe -m scripts.clear_cosmos --containers chunks,jobs
"""

from __future__ import annotations

import sys
import time

from azure.cosmos import CosmosClient

from app.config import get_settings

# container name → partition-key field on each item
CONTAINERS = {
    "entities":      "type",
    "relationships": "source_doc",
    "chunks":        "doc_id",
    "communities":   "graph_id",
    "jobs":          "id",
}


def main() -> int:
    args = sys.argv[1:]
    settings = get_settings()
    if not settings.cosmos_endpoint or not settings.cosmos_key:
        print("COSMOS_ENDPOINT / COSMOS_KEY not set in .env")
        return 1

    selected = list(CONTAINERS)
    for a in args:
        if a.startswith("--containers"):
            names = a.split("=", 1)[1] if "=" in a else args[args.index(a) + 1]
            selected = [n.strip() for n in names.split(",") if n.strip()]
            bad = [n for n in selected if n not in CONTAINERS]
            if bad:
                print(f"Unknown container(s): {bad}. Valid: {list(CONTAINERS)}")
                return 1

    client = CosmosClient(settings.cosmos_endpoint, credential=settings.cosmos_key)
    db = client.get_database_client(settings.cosmos_database)

    print(f"target  : {settings.cosmos_endpoint}")
    print(f"database: {settings.cosmos_database}\n")

    # Counts first — always shown, and the whole job if --counts-only
    counts: dict[str, int] = {}
    for name in selected:
        cc = db.get_container_client(name)
        rows = list(cc.query_items("SELECT VALUE COUNT(1) FROM c",
                                   enable_cross_partition_query=True))
        counts[name] = rows[0] if rows else 0
        print(f"  {name:15s} {counts[name]} items")

    total = sum(counts.values())
    if "--counts-only" in args:
        return 0
    if total == 0:
        print("\nNothing to delete.")
        return 0

    if "--yes" not in args:
        answer = input(
            f"\nPERMANENTLY delete {total} items from {len(selected)} container(s) "
            f"in {settings.cosmos_endpoint} ?\nType 'delete' to proceed: "
        ).strip().lower()
        if answer != "delete":
            print("aborted — nothing deleted")
            return 1

    t0 = time.time()
    for name in selected:
        if counts[name] == 0:
            continue
        cc = db.get_container_client(name)
        pk_field = CONTAINERS[name]
        print(f"clearing {name} … ", end="", flush=True)
        deleted = 0
        # re-query in pages until empty (deleting while iterating a single
        # query result can skip items)
        while True:
            batch = list(cc.query_items(
                f"SELECT TOP 100 c.id, c.{pk_field} AS pk FROM c",
                enable_cross_partition_query=True,
            ))
            if not batch:
                break
            for item in batch:
                cc.delete_item(item=item["id"], partition_key=item.get("pk"))
                deleted += 1
        print(f"{deleted} deleted")

    print(f"\ndone in {time.time() - t0:.1f}s — verify:")
    for name in selected:
        cc = db.get_container_client(name)
        rows = list(cc.query_items("SELECT VALUE COUNT(1) FROM c",
                                   enable_cross_partition_query=True))
        print(f"  {name:15s} {rows[0] if rows else 0} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
