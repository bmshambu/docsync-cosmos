"""GraphStore — persistence interface for all graph artefacts.

Step 1 of the Cosmos migration (see migration_strategy.md): every read/write of
entities, relationships, chunks, community data, and graph stats goes through
this interface. `FileGraphStore` preserves today's file layout exactly, so
behaviour is unchanged. Step 2 adds `CosmosGraphStore` implementing the same
interface against the five Cosmos containers:

    entities(/type) · relationships(/source_doc) · chunks(/doc_id)
    · communities(/graph_id) · jobs(/id)

Select the backend with STORAGE_BACKEND in .env ("file" | "cosmos").

Design notes for the Cosmos implementation:
  - save_extraction() receives the FULL merged state; the file backend writes
    it whole, the Cosmos backend should diff/upsert per entity id and per
    relationship (synthetic id = hash of source|target|relation_type|source_doc).
  - The interactive graph HTML generator reads plain JSON files; Cosmos backend
    should implement export_snapshot() by writing a temp snapshot before
    generation (file backend just returns its live paths).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.config import Settings, get_settings


class GraphStore(ABC):
    """Persistence for the knowledge-graph artefacts. One instance per backend."""

    # ── entities / relationships ────────────────────────────────────────────
    @abstractmethod
    def get_entities(self) -> list[dict]: ...

    @abstractmethod
    def get_relationships(self) -> list[dict]: ...

    @abstractmethod
    def save_extraction(self, entities: list[dict], relationships: list[dict]) -> None:
        """Persist the full merged entity/relationship state."""

    @abstractmethod
    def count_entities(self) -> int: ...

    def get_extracted_doc_names(self) -> set[str]:
        """Filenames that already have extracted entities (skip logic)."""
        names: set[str] = set()
        for e in self.get_entities():
            for fname in (e.get("source_docs") or []):
                names.add(fname)
        return names

    # ── targeted queries (retrieval hot path) ───────────────────────────────
    # Base implementations filter full pulls — identical behaviour to today.
    # Backends with a query engine (Cosmos) override with WHERE clauses so a
    # question never loads the whole corpus.

    def search_entity_candidates(self, keywords: list[str]) -> list[dict]:
        """Entities whose name/aliases/type contain any keyword (unscored)."""
        if not keywords:
            return []
        out = []
        for e in self.get_entities():
            text = " ".join(
                [e.get("name", ""), e.get("type", "")] + list(e.get("aliases") or [])
            ).lower()
            if any(kw in text for kw in keywords):
                out.append(e)
        return out

    def get_entities_by_type(self, entity_type: str) -> list[dict]:
        return [e for e in self.get_entities() if e.get("type") == entity_type]

    def get_relationships_for(self, entity_ids: set[str]) -> list[dict]:
        """Relationships touching any of the given entity ids."""
        if not entity_ids:
            return []
        return [
            r for r in self.get_relationships()
            if r.get("source") in entity_ids or r.get("target") in entity_ids
        ]

    def search_chunk_candidates(
        self, keywords: list[str],
        filter_doc_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Chunks whose text contains any keyword (unscored, capped)."""
        if not keywords:
            return []
        out = []
        for c in self.iter_chunks():
            if filter_doc_ids and c.get("doc_id") not in filter_doc_ids:
                continue
            text = (c.get("text") or "").lower()
            if any(kw in text for kw in keywords):
                out.append(c)
                if len(out) >= limit:
                    break
        return out

    def get_all_summaries(self) -> dict[str, str]:
        """comm_id → full summary text, in ONE backend call (never loop point-reads)."""
        result: dict[str, str] = {}
        for cid in self.get_community_map().get("communities", {}):
            text = self.get_community_summary(cid)
            if text:
                result[str(cid)] = text
        return result

    # ── jobs (durable job state, migration step 5) ──────────────────────────
    @abstractmethod
    def save_job(self, job: dict) -> None:
        """Persist a job status snapshot (idempotent upsert by job['id'])."""

    @abstractmethod
    def get_job(self, job_id: str) -> dict | None:
        """Fetch a persisted job snapshot (used when not in this replica's memory)."""

    # ── chunks ──────────────────────────────────────────────────────────────
    @abstractmethod
    def iter_chunks(self) -> Iterable[dict]:
        """Yield every chunk dict in the corpus."""

    @abstractmethod
    def save_doc_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        """Persist one document's chunks (replaces any previous set)."""

    def has_chunks(self) -> bool:
        """Cheap existence check — backends should avoid full scans."""
        return next(iter(self.iter_chunks()), None) is not None

    def chunks_by_filename(self) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {}
        for c in self.iter_chunks():
            by.setdefault(c.get("filename", "?"), []).append(c)
        return by

    def chunks_by_doc_id(self) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {}
        for c in self.iter_chunks():
            by.setdefault(c.get("doc_id", "?"), []).append(c)
        return by

    # ── community map + summaries ───────────────────────────────────────────
    @abstractmethod
    def get_community_map(self) -> dict: ...

    @abstractmethod
    def save_community_map(self, community_map: dict) -> None: ...

    @abstractmethod
    def save_community_summary(self, comm_id: str, text: str) -> str:
        """Persist one community's markdown summary. Returns a reference string."""

    @abstractmethod
    def get_community_summary(self, comm_id: str) -> str | None: ...

    @abstractmethod
    def list_community_summaries(self) -> list[dict]:
        """[{'file','title','preview'}] for every stored summary, id order."""

    def summary_ok(self, comm_id: str) -> bool:
        """True if a community has a real (non-stub, non-empty) summary."""
        text = self.get_community_summary(comm_id)
        if not text:
            return False
        text = text.strip()
        return len(text) >= 100 and "Summary Unavailable" not in text[:80]

    def summary_ok_ids(self) -> set[str]:
        """Comm-ids with a valid summary, in ONE backend call — use this instead
        of calling summary_ok() in a loop (N point-reads is pathological on Cosmos)."""
        return {
            str(int(item["file"].replace("community_", "").replace(".md", "")))
            for item in self.list_community_summaries()
            if len(item.get("preview", "")) >= 100
            and "Summary Unavailable" not in item.get("preview", "")[:80]
        }

    # ── graph stats ─────────────────────────────────────────────────────────
    @abstractmethod
    def get_graph_stats(self) -> dict | None: ...

    @abstractmethod
    def save_graph_stats(self, stats: dict) -> None: ...

    # ── snapshot for the D3 HTML generator ──────────────────────────────────
    @abstractmethod
    def export_snapshot(self) -> dict:
        """Return JSON file paths for the HTML generator:
        {'entities_file', 'relationships_file', 'community_map_file', 'communities_dir'}
        """


# ══════════════════════════════════════════════════════════════════════════
# File backend — today's exact layout under DATA_DIR
# ══════════════════════════════════════════════════════════════════════════

class FileGraphStore(GraphStore):
    def __init__(self, settings: Settings):
        self.s = settings

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _read_json(path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_json(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _summary_file(self, comm_id: str) -> Path | None:
        try:
            return self.s.communities_dir / f"community_{int(comm_id):02d}.md"
        except (ValueError, TypeError):
            return None

    # ── entities / relationships ─────────────────────────────────────────
    def get_entities(self) -> list[dict]:
        return self._read_json(self.s.entities_file, [])

    def get_relationships(self) -> list[dict]:
        return self._read_json(self.s.relationships_file, [])

    def save_extraction(self, entities: list[dict], relationships: list[dict]) -> None:
        self._write_json(self.s.entities_file, entities)
        self._write_json(self.s.relationships_file, relationships)

    def count_entities(self) -> int:
        return len(self.get_entities())

    # ── chunks ───────────────────────────────────────────────────────────
    def iter_chunks(self) -> Iterable[dict]:
        if not self.s.chunks_dir.exists():
            return
        for f in sorted(self.s.chunks_dir.glob("*_chunks.json")):
            for c in self._read_json(f, []):
                yield c

    def save_doc_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        self._write_json(self.s.chunks_dir / f"{doc_id}_chunks.json", chunks)

    # ── community map + summaries ────────────────────────────────────────
    def get_community_map(self) -> dict:
        return self._read_json(self.s.community_map_file, {})

    def save_community_map(self, community_map: dict) -> None:
        self._write_json(self.s.community_map_file, community_map)

    def save_community_summary(self, comm_id: str, text: str) -> str:
        self.s.communities_dir.mkdir(parents=True, exist_ok=True)
        out = self._summary_file(comm_id)
        out.write_text(text, encoding="utf-8")
        return str(out)

    def get_community_summary(self, comm_id: str) -> str | None:
        f = self._summary_file(comm_id)
        if not f or not f.exists():
            return None
        try:
            return f.read_text(encoding="utf-8")
        except Exception:
            return None

    def list_community_summaries(self) -> list[dict]:
        if not self.s.communities_dir.exists():
            return []
        items = []
        for md_file in sorted(self.s.communities_dir.glob("community_*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            title = md_file.stem
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") and not stripped.startswith("##") and len(stripped) > 1:
                    title = stripped.lstrip("#").strip()
                    break
            items.append({"file": md_file.name, "title": title, "preview": text[:400].strip()})
        return items

    # ── graph stats ──────────────────────────────────────────────────────
    def get_graph_stats(self) -> dict | None:
        return self._read_json(self.s.graph_stats_file, None)

    def save_graph_stats(self, stats: dict) -> None:
        self._write_json(self.s.graph_stats_file, stats)

    # ── jobs ─────────────────────────────────────────────────────────────
    def save_job(self, job: dict) -> None:
        self._write_json(self.s.data_dir / "jobs" / f"{job['id']}.json", job)

    def get_job(self, job_id: str) -> dict | None:
        return self._read_json(self.s.data_dir / "jobs" / f"{_safe_filename(job_id)}.json", None)

    # ── snapshot ─────────────────────────────────────────────────────────
    def export_snapshot(self) -> dict:
        # Files ARE the live store — hand the generator the real paths.
        return {
            "entities_file": self.s.entities_file,
            "relationships_file": self.s.relationships_file,
            "community_map_file": self.s.community_map_file,
            "communities_dir": self.s.communities_dir,
        }


# ══════════════════════════════════════════════════════════════════════════
# Cosmos DB backend — five containers, shared 1000 RU/s
# ══════════════════════════════════════════════════════════════════════════

import hashlib
import re as _re


def _safe_id(s: str) -> str:
    """Cosmos item ids may not contain / \\ # ?"""
    return _re.sub(r"[/\\#?]", "_", s)


def _safe_filename(s: str) -> str:
    return _re.sub(r"[^A-Za-z0-9_-]", "_", s)


def _rel_id(r: dict) -> str:
    """Deterministic synthetic id for a relationship."""
    key = f"{r.get('source')}|{r.get('target')}|{r.get('relation_type')}|{r.get('source_doc')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def _clean(doc: dict) -> dict:
    """Strip Cosmos system properties (_rid, _etag, _ts, …)."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class CosmosGraphStore(GraphStore):
    """GraphStore against Azure Cosmos DB for NoSQL.

    Containers (partition keys): entities(/type) · relationships(/source_doc)
    · chunks(/doc_id) · communities(/graph_id). Community map, summaries, and
    graph stats all live in `communities`, discriminated by a `kind` field.

    save_extraction() diffs against an in-memory cache of the last-persisted
    state, so the per-document incremental saves during extraction upsert only
    what changed — no O(N²) rewrites.

    Uses the sync azure-cosmos SDK; calls are short (single-digit ms at PoC
    scale) so blocking the event loop briefly is acceptable for now.

    NOTE: the community map is stored as ONE document — fine at PoC scale,
    but Cosmos caps items at 2 MB; split node_to_community per community
    before the corpus reaches ~50k entities.
    """

    GRAPH_ID = "default"

    def __init__(self, settings: Settings):
        from azure.cosmos import CosmosClient

        if not settings.cosmos_endpoint or not settings.cosmos_key:
            raise RuntimeError(
                "STORAGE_BACKEND=cosmos requires COSMOS_ENDPOINT and COSMOS_KEY in .env"
            )
        self.s = settings
        client = CosmosClient(settings.cosmos_endpoint, credential=settings.cosmos_key)
        db = client.get_database_client(settings.cosmos_database)
        self._entities      = db.get_container_client("entities")
        self._relationships = db.get_container_client("relationships")
        self._chunks        = db.get_container_client("chunks")
        self._communities   = db.get_container_client("communities")
        self._jobs          = db.get_container_client("jobs")
        # last-persisted state (id → cleaned doc) for incremental diffing
        self._ent_cache: dict[str, dict] | None = None
        self._rel_cache: dict[str, dict] | None = None

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _query_all(container, query: str = "SELECT * FROM c", params=None) -> list[dict]:
        return list(container.query_items(
            query=query, parameters=params or [], enable_cross_partition_query=True
        ))

    def _load_caches(self) -> None:
        if self._ent_cache is None:
            self._ent_cache = {d["id"]: _clean(d) for d in self._query_all(self._entities)}
        if self._rel_cache is None:
            self._rel_cache = {d["id"]: _clean(d) for d in self._query_all(self._relationships)}

    # ── entities / relationships ─────────────────────────────────────────
    def get_entities(self) -> list[dict]:
        self._load_caches()
        return [dict(d) for d in self._ent_cache.values()]

    def get_relationships(self) -> list[dict]:
        self._load_caches()
        return [dict(d) for d in self._rel_cache.values()]

    def save_extraction(self, entities: list[dict], relationships: list[dict]) -> None:
        self._load_caches()

        # Entities — upsert new/changed, delete removed (purge support)
        new_ents: dict[str, dict] = {}
        for e in entities:
            eid = _safe_id(str(e.get("id") or ""))
            if not eid:
                continue
            doc = {**e, "id": eid, "type": e.get("type") or "unknown"}
            new_ents[eid] = doc
        for eid, doc in new_ents.items():
            if self._ent_cache.get(eid) != doc:
                self._entities.upsert_item(doc)
        for eid, old in list(self._ent_cache.items()):
            if eid not in new_ents:
                self._entities.delete_item(item=eid, partition_key=old.get("type", "unknown"))
        self._ent_cache = new_ents

        # Relationships — same pattern with synthetic ids
        new_rels: dict[str, dict] = {}
        for r in relationships:
            doc = {**r,
                   "source_doc": r.get("source_doc") or "unknown"}
            doc["id"] = _rel_id(doc)
            new_rels[doc["id"]] = doc
        for rid, doc in new_rels.items():
            if self._rel_cache.get(rid) != doc:
                self._relationships.upsert_item(doc)
        for rid, old in list(self._rel_cache.items()):
            if rid not in new_rels:
                self._relationships.delete_item(item=rid, partition_key=old.get("source_doc", "unknown"))
        self._rel_cache = new_rels

    def count_entities(self) -> int:
        rows = self._query_all(self._entities, "SELECT VALUE COUNT(1) FROM c")
        return rows[0] if rows else 0

    # ── targeted queries — WHERE clauses instead of full pulls ───────────
    def search_entity_candidates(self, keywords: list[str]) -> list[dict]:
        if not keywords:
            return []
        clauses, params = [], []
        for i, kw in enumerate(keywords[:10]):
            p = f"@kw{i}"
            clauses.append(
                f"(CONTAINS(LOWER(c.name), {p}) OR CONTAINS(LOWER(c.type), {p}) "
                f"OR EXISTS(SELECT VALUE a FROM a IN c.aliases WHERE CONTAINS(LOWER(a), {p})))"
            )
            params.append({"name": p, "value": kw.lower()})
        query = f"SELECT * FROM c WHERE {' OR '.join(clauses)}"
        return [_clean(d) for d in self._query_all(self._entities, query, params)]

    def get_entities_by_type(self, entity_type: str) -> list[dict]:
        # /type is the partition key — this is a cheap single-partition query
        return [_clean(d) for d in self._query_all(
            self._entities, "SELECT * FROM c WHERE c.type = @t",
            [{"name": "@t", "value": entity_type}],
        )]

    def get_relationships_for(self, entity_ids: set[str]) -> list[dict]:
        if not entity_ids:
            return []
        ids = list(entity_ids)
        return [_clean(d) for d in self._query_all(
            self._relationships,
            "SELECT * FROM c WHERE ARRAY_CONTAINS(@ids, c.source) OR ARRAY_CONTAINS(@ids, c.target)",
            [{"name": "@ids", "value": ids}],
        )]

    def search_chunk_candidates(
        self, keywords: list[str],
        filter_doc_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if not keywords:
            return []
        clauses, params = [], []
        for i, kw in enumerate(keywords[:10]):
            p = f"@kw{i}"
            clauses.append(f"CONTAINS(LOWER(c.text), {p})")
            params.append({"name": p, "value": kw.lower()})
        query = f"SELECT TOP {int(limit)} * FROM c WHERE ({' OR '.join(clauses)})"
        if filter_doc_ids:
            query += " AND ARRAY_CONTAINS(@docs, c.doc_id)"
            params.append({"name": "@docs", "value": list(filter_doc_ids)})
        return [_clean(d) for d in self._query_all(self._chunks, query, params)]

    def get_all_summaries(self) -> dict[str, str]:
        rows = self._query_all(
            self._communities,
            "SELECT c.comm_id, c.text FROM c WHERE c.kind = 'summary'",
        )
        return {str(d["comm_id"]): d.get("text") or "" for d in rows if d.get("comm_id") is not None}

    # ── jobs ─────────────────────────────────────────────────────────────
    def save_job(self, job: dict) -> None:
        doc = dict(job)
        doc["id"] = _safe_id(str(doc.get("id")))
        # Cap stored logs — the doc must stay well under Cosmos's 2 MB item limit
        if isinstance(doc.get("logs"), list) and len(doc["logs"]) > 500:
            doc["logs"] = doc["logs"][-500:]
        self._jobs.upsert_item(doc)

    def get_job(self, job_id: str) -> dict | None:
        from azure.cosmos import exceptions
        try:
            return _clean(self._jobs.read_item(
                item=_safe_id(str(job_id)), partition_key=_safe_id(str(job_id))
            ))
        except exceptions.CosmosResourceNotFoundError:
            return None

    # ── chunks ───────────────────────────────────────────────────────────
    def iter_chunks(self) -> Iterable[dict]:
        # Stream lazily — callers that only need the first item (existence
        # checks) must not pay for a full 500-doc pull.
        for d in self._chunks.query_items(
            query="SELECT * FROM c", enable_cross_partition_query=True
        ):
            yield _clean(d)

    def has_chunks(self) -> bool:
        rows = self._query_all(self._chunks, "SELECT VALUE COUNT(1) FROM c")
        return bool(rows and rows[0])

    def save_doc_chunks(self, doc_id: str, chunks: list[dict]) -> None:
        existing = {d["id"] for d in self._query_all(
            self._chunks, "SELECT c.id FROM c WHERE c.doc_id = @d",
            [{"name": "@d", "value": doc_id}],
        )}
        new_ids: set[str] = set()
        for i, c in enumerate(chunks):
            cid = f"{_safe_id(doc_id)}_{i:04d}"
            new_ids.add(cid)
            self._chunks.upsert_item({**c, "id": cid, "doc_id": c.get("doc_id") or doc_id})
        # Remove stale chunks from a previous (longer) chunking of this doc
        for cid in existing - new_ids:
            self._chunks.delete_item(item=cid, partition_key=doc_id)

    # ── communities container (map / summaries / stats via `kind`) ───────
    def _read_comm_item(self, item_id: str) -> dict | None:
        from azure.cosmos import exceptions
        try:
            return self._communities.read_item(item=item_id, partition_key=self.GRAPH_ID)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def get_community_map(self) -> dict:
        doc = self._read_comm_item("community_map")
        return (doc or {}).get("map", {})

    def save_community_map(self, community_map: dict) -> None:
        self._communities.upsert_item({
            "id": "community_map", "graph_id": self.GRAPH_ID,
            "kind": "map", "map": community_map,
        })

    @staticmethod
    def _summary_id(comm_id: str) -> str:
        return f"summary_{int(comm_id):02d}"

    def save_community_summary(self, comm_id: str, text: str) -> str:
        title = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith("##") and len(stripped) > 1:
                title = stripped.lstrip("#").strip()
                break
        self._communities.upsert_item({
            "id": self._summary_id(comm_id), "graph_id": self.GRAPH_ID,
            "kind": "summary", "comm_id": str(comm_id),
            "title": title, "text": text,
        })
        return f"cosmos://communities/{self._summary_id(comm_id)}"

    def get_community_summary(self, comm_id: str) -> str | None:
        try:
            doc = self._read_comm_item(self._summary_id(comm_id))
        except (ValueError, TypeError):
            return None
        return doc.get("text") if doc else None

    def list_community_summaries(self) -> list[dict]:
        # Project only what the UI needs — never pull full summary bodies here
        rows = self._query_all(
            self._communities,
            "SELECT c.id, c.comm_id, c.title, SUBSTRING(c.text, 0, 400) AS preview "
            "FROM c WHERE c.kind = 'summary' ORDER BY c.id",
        )
        items = []
        for d in rows:
            # Keep the community_NN.md naming — the UI derives the number from it
            fname = f"community_{int(d.get('comm_id', 0)):02d}.md"
            items.append({
                "file": fname,
                "title": d.get("title") or fname,
                "preview": (d.get("preview") or "").strip(),
            })
        return items

    # ── graph stats ──────────────────────────────────────────────────────
    def get_graph_stats(self) -> dict | None:
        doc = self._read_comm_item("graph_stats")
        return doc.get("stats") if doc else None

    def save_graph_stats(self, stats: dict) -> None:
        self._communities.upsert_item({
            "id": "graph_stats", "graph_id": self.GRAPH_ID,
            "kind": "stats", "stats": stats,
        })

    # ── snapshot for the D3 HTML generator ───────────────────────────────
    def export_snapshot(self) -> dict:
        """Dump a JSON/md snapshot to a scratch dir for the HTML generator."""
        snap = self.s.data_dir / "graph_snapshot"
        comm_dir = snap / "communities"
        comm_dir.mkdir(parents=True, exist_ok=True)

        def _dump(path: Path, data) -> Path:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            return path

        community_map = self.get_community_map()
        paths = {
            "entities_file": _dump(snap / "entities.json", self.get_entities()),
            "relationships_file": _dump(snap / "relationships.json", self.get_relationships()),
            "community_map_file": _dump(snap / "community_map.json", community_map),
            "communities_dir": comm_dir,
        }
        for cid in community_map.get("communities", {}):
            text = self.get_community_summary(cid)
            if text:
                (comm_dir / f"community_{int(cid):02d}.md").write_text(text, encoding="utf-8")
        return paths


# ══════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def get_graph_store() -> GraphStore:
    settings = get_settings()
    backend = (settings.storage_backend or "file").lower()
    if backend == "cosmos":
        return CosmosGraphStore(settings)
    return FileGraphStore(settings)
