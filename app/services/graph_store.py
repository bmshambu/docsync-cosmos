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
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.config import Settings, get_settings

# Global (whole-corpus) community graph id. Per-scope community builds use a
# scope_graph_id() derived from the scope value instead.
DEFAULT_GRAPH_ID = "default"


def scope_graph_id(scope_value: str | None) -> str:
    """Deterministic, storage-safe community graph_id for a scope value.

    'Clients and Markets' -> 'scope_clients_and_markets'. Empty / 'default'
    stays the global graph id. Single source of truth for the mapping — the
    build driver, retrieval, and endpoints all slug through here so a scope's
    per-scope communities always land in (and read from) the same partition."""
    s = (scope_value or "").strip().lower()
    if not s or s == DEFAULT_GRAPH_ID:
        return DEFAULT_GRAPH_ID
    slug = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return f"scope_{slug}" if slug else DEFAULT_GRAPH_ID


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

    def get_all_summaries(self, graph_id: str | None = None) -> dict[str, str]:
        """comm_id → full summary text, in ONE backend call (never loop point-reads)."""
        result: dict[str, str] = {}
        for cid in self.get_community_map(graph_id).get("communities", {}):
            text = self.get_community_summary(cid, graph_id)
            if text:
                result[str(cid)] = text
        return result

    def list_doc_titles(self) -> list[dict]:
        """Distinct documents in the corpus: [{'doc_id', 'filename'}].

        Powers filename-as-question matching. When a metadata registry exists,
        the curated `title` and `name` are included so title matching can use
        them (Templafy titles beat filename parsing).
        """
        reg = self.get_doc_registry()
        seen: dict[str, str] = {}
        for c in self.iter_chunks():
            did = c.get("doc_id")
            if did and did not in seen:
                seen[did] = c.get("filename", did)
        out = []
        for did, fname in seen.items():
            rec = reg.get(did) or {}
            out.append({
                "doc_id": did, "filename": fname,
                "title": rec.get("title") or "", "name": rec.get("name") or "",
            })
        return out

    # ── metadata registry + scoping (M1 service-function plan) ──────────────
    # save/get are backend-specific; the scoping helpers below are shared.

    def save_doc_registry(self, records: list[dict]) -> None:
        raise NotImplementedError

    def get_doc_registry(self) -> dict[str, dict]:
        """{doc_id: record}. Empty when no metadata has been synced yet."""
        raise NotImplementedError

    def list_platforms(self) -> list[dict]:
        """Distinct Platform values with live doc counts (excludes deleted).
        [{'value': 'Oracle', 'count': 64}] — kept for back-compat callers.
        The scope dropdown now uses list_scopes() (both fields)."""
        from collections import Counter
        counts: Counter = Counter()
        display: dict[str, str] = {}   # lowered -> first-seen original casing
        for rec in self.get_doc_registry().values():
            if rec.get("is_deleted"):
                continue
            for p in (rec.get("platform") or []):
                counts[p] += 1
                display.setdefault(p, p)
        # Prefer nicer display casing from the record's raw values if present
        return sorted(
            [{"value": display.get(k, k).title() if k.islower() else display.get(k, k),
              "count": n} for k, n in counts.items()],
            key=lambda d: -d["count"],
        )

    def list_scopes(self) -> list[dict]:
        """Distinct scope values across BOTH the Platform ('Platform / Sub
        Service Line') and Services_Function ('Service Function') fields, with
        live doc counts. Powers the scope dropdown.

        The two fields never share a value (verified in M0), so every value maps
        to exactly one field — recorded as 'field' so the UI can group them and
        so callers can scope by either without ambiguity.
        [{'value': 'Oracle', 'count': 64, 'field': 'platform'},
         {'value': 'Advisory', 'count': 699, 'field': 'service_function'}]"""
        from collections import Counter
        counts: Counter = Counter()
        field_of: dict[str, str] = {}
        for rec in self.get_doc_registry().values():
            if rec.get("is_deleted"):
                continue
            for p in (rec.get("platform") or []):
                counts[p] += 1
                field_of.setdefault(p, "platform")
            for s in (rec.get("service_functions") or []):
                counts[s] += 1
                field_of.setdefault(s, "service_function")

        _small = {"and", "of", "the", "for", "to", "in", "on", "a", "an"}

        def _disp(k: str) -> str:
            if not k.islower():
                return k                       # already has intentional casing
            words = k.split()
            return " ".join(
                w if (i and w in _small) else w.capitalize()
                for i, w in enumerate(words)
            )

        # Platform values first, then Service Function; each group by count desc.
        return sorted(
            [{"value": _disp(k), "count": n, "field": field_of[k]}
             for k, n in counts.items()],
            key=lambda d: (0 if d["field"] == "platform" else 1, -d["count"]),
        )

    def scoped_doc_ids(
        self, platform: str | None = None, service_function: str | None = None,
        any_value: str | None = None,
    ) -> set[str] | None:
        """doc_ids in scope. None = no scope (all docs, today's behaviour).

        any_value → matches EITHER field (Platform OR Services_Function). This
            is what a user-selected scope uses, since the dropdown now mixes
            values from both fields and they never collide.
        platform → Platform field only (exact lowered match).
        service_function → Services_Function field only (Track B / Clients and
            Markets uses this so it always targets the SF field).
        Deleted docs are always excluded. Docs with NO scope tags at all are
        included in every scope (fallback: never invisible; ~1 doc in practice).
        """
        if not platform and not service_function and not any_value:
            return None
        plat = (platform or "").strip().lower()
        sf = (service_function or "").strip().lower()
        anyv = (any_value or "").strip().lower()
        out: set[str] = set()
        for did, rec in self.get_doc_registry().items():
            if rec.get("is_deleted"):
                continue
            tags_plat = rec.get("platform") or []
            tags_sf = rec.get("service_functions") or []
            if not tags_plat and not tags_sf:
                out.add(did)                       # untagged fallback
                continue
            if anyv and (anyv in tags_plat or anyv in tags_sf):
                out.add(did)
            elif plat and plat in tags_plat:
                out.add(did)
            elif sf and sf in tags_sf:
                out.add(did)
        return out

    def doc_web_url(self, doc_id: str | None = None, filename: str | None = None) -> str | None:
        """Governed source URL (Templafy/SharePoint) for citations, if known."""
        reg = self.get_doc_registry()
        if doc_id and doc_id in reg:
            return reg[doc_id].get("web_url") or None
        if filename:
            for rec in reg.values():
                if rec.get("filename") == filename:
                    return rec.get("web_url") or None
        return None

    def get_chunks_for_docs(self, doc_ids: list[str]) -> list[dict]:
        """All chunks belonging to the given documents."""
        if not doc_ids:
            return []
        wanted = set(doc_ids)
        return [c for c in self.iter_chunks() if c.get("doc_id") in wanted]

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
    # graph_id selects which community graph to read/write: None/'default' is
    # the whole-corpus graph; a scope_graph_id(value) is a per-scope graph
    # (item #4). The doc registry is NEVER per-scope — it stays global.
    @abstractmethod
    def get_community_map(self, graph_id: str | None = None) -> dict: ...

    @abstractmethod
    def save_community_map(self, community_map: dict, graph_id: str | None = None) -> None: ...

    @abstractmethod
    def save_community_summary(self, comm_id: str, text: str, graph_id: str | None = None) -> str:
        """Persist one community's markdown summary. Returns a reference string."""

    @abstractmethod
    def get_community_summary(self, comm_id: str, graph_id: str | None = None) -> str | None: ...

    @abstractmethod
    def list_community_summaries(self, graph_id: str | None = None) -> list[dict]:
        """[{'file','title','preview'}] for every stored summary, id order."""

    def summary_ok(self, comm_id: str, graph_id: str | None = None) -> bool:
        """True if a community has a real (non-stub, non-empty) summary."""
        text = self.get_community_summary(comm_id, graph_id)
        if not text:
            return False
        text = text.strip()
        return len(text) >= 100 and "Summary Unavailable" not in text[:80]

    def summary_ok_ids(self, graph_id: str | None = None) -> set[str]:
        """Comm-ids with a valid summary, in ONE backend call — use this instead
        of calling summary_ok() in a loop (N point-reads is pathological on Cosmos)."""
        return {
            str(int(item["file"].replace("community_", "").replace(".md", "")))
            for item in self.list_community_summaries(graph_id)
            if len(item.get("preview", "")) >= 100
            and "Summary Unavailable" not in item.get("preview", "")[:80]
        }

    # ── graph stats ─────────────────────────────────────────────────────────
    @abstractmethod
    def get_graph_stats(self, graph_id: str | None = None) -> dict | None: ...

    @abstractmethod
    def save_graph_stats(self, stats: dict, graph_id: str | None = None) -> None: ...

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

    # ── per-scope path helpers ───────────────────────────────────────────
    # graph_id None/'default' → today's top-level layout (unchanged). A scope
    # graph id → an isolated subtree under graph/scopes/<graph_id>/.
    def _scope_dir(self, graph_id: str | None) -> Path:
        if not graph_id or graph_id == DEFAULT_GRAPH_ID:
            return self.s.graph_dir
        return self.s.graph_dir / "scopes" / graph_id

    def _map_file(self, graph_id: str | None) -> Path:
        return self._scope_dir(graph_id) / "community_map.json"

    def _comm_dir(self, graph_id: str | None) -> Path:
        return self._scope_dir(graph_id) / "communities"

    def _stats_file(self, graph_id: str | None) -> Path:
        return self._scope_dir(graph_id) / "graph_stats.json"

    def _summary_file(self, comm_id: str, graph_id: str | None = None) -> Path | None:
        try:
            return self._comm_dir(graph_id) / f"community_{int(comm_id):02d}.md"
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
    def get_community_map(self, graph_id: str | None = None) -> dict:
        return self._read_json(self._map_file(graph_id), {})

    def save_community_map(self, community_map: dict, graph_id: str | None = None) -> None:
        self._write_json(self._map_file(graph_id), community_map)

    def save_community_summary(self, comm_id: str, text: str, graph_id: str | None = None) -> str:
        self._comm_dir(graph_id).mkdir(parents=True, exist_ok=True)
        out = self._summary_file(comm_id, graph_id)
        out.write_text(text, encoding="utf-8")
        return str(out)

    def get_community_summary(self, comm_id: str, graph_id: str | None = None) -> str | None:
        f = self._summary_file(comm_id, graph_id)
        if not f or not f.exists():
            return None
        try:
            return f.read_text(encoding="utf-8")
        except Exception:
            return None

    def list_community_summaries(self, graph_id: str | None = None) -> list[dict]:
        comm_dir = self._comm_dir(graph_id)
        if not comm_dir.exists():
            return []
        items = []
        for md_file in sorted(comm_dir.glob("community_*.md")):
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
    def get_graph_stats(self, graph_id: str | None = None) -> dict | None:
        return self._read_json(self._stats_file(graph_id), None)

    def save_graph_stats(self, stats: dict, graph_id: str | None = None) -> None:
        self._write_json(self._stats_file(graph_id), stats)

    # ── doc registry (metadata scoping) ──────────────────────────────────
    def _registry_file(self):
        return self.s.graph_dir / "doc_registry.json"

    def save_doc_registry(self, records: list[dict]) -> None:
        existing = self._read_json(self._registry_file(), {})
        for rec in records:
            if rec.get("doc_id"):
                existing[rec["doc_id"]] = rec
        self._write_json(self._registry_file(), existing)

    def get_doc_registry(self) -> dict[str, dict]:
        return self._read_json(self._registry_file(), {})

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
        # community map cache, keyed by graph_id (default + per-scope graphs)
        self._map_cache: dict[str, dict] = {}

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

    def get_all_summaries(self, graph_id: str | None = None) -> dict[str, str]:
        rows = self._query_all(
            self._communities,
            "SELECT c.comm_id, c.text FROM c WHERE c.graph_id = @gid AND c.kind = 'summary'",
            [{"name": "@gid", "value": self._gid(graph_id)}],
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
        self._titles_cache = None   # doc list changed

    def list_doc_titles(self) -> list[dict]:
        # Cached — the doc list only changes on extraction runs
        cached = getattr(self, "_titles_cache", None)
        if cached is not None:
            return cached
        rows = self._query_all(self._chunks, "SELECT DISTINCT c.doc_id, c.filename FROM c")
        self._titles_cache = [
            {"doc_id": d["doc_id"], "filename": d.get("filename") or d["doc_id"]}
            for d in rows if d.get("doc_id")
        ]
        return self._titles_cache

    def get_chunks_for_docs(self, doc_ids: list[str]) -> list[dict]:
        if not doc_ids:
            return []
        return [_clean(d) for d in self._query_all(
            self._chunks,
            "SELECT * FROM c WHERE ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@ids", "value": list(doc_ids)}],
        )]

    # ── communities container (map / summaries / stats via `kind`) ───────
    # Every item is partitioned by graph_id: 'default' (whole corpus) or a
    # scope_graph_id (per-scope, item #4). EVERY query below filters graph_id so
    # one scope's build never reads or clobbers another's — a cross-partition
    # query without that filter would mix all scopes' shards/summaries together.
    @staticmethod
    def _gid(graph_id: str | None) -> str:
        return graph_id or DEFAULT_GRAPH_ID

    def _read_comm_item(self, item_id: str, graph_id: str | None = None) -> dict | None:
        from azure.cosmos import exceptions
        try:
            return self._communities.read_item(item=item_id, partition_key=self._gid(graph_id))
        except exceptions.CosmosResourceNotFoundError:
            return None

    # Cosmos caps items at 2 MB. At scale the community_map (esp. its
    # node_to_community with one entry per entity) exceeds that, so it is SHARDED
    # across multiple items and reassembled on read. Old single-item maps
    # ("community_map") are still read for backward compatibility.
    _MAP_SHARD_BYTES = 1_600_000   # < 2 MB item cap, leaves room for envelope

    def get_community_map(self, graph_id: str | None = None) -> dict:
        gid = self._gid(graph_id)
        if gid in self._map_cache:
            return self._map_cache[gid]
        shards = self._query_all(
            self._communities,
            "SELECT c.part, c.data FROM c WHERE c.graph_id = @gid AND c.kind = 'map_shard' "
            "ORDER BY c.part",
            [{"name": "@gid", "value": gid}],
        )
        if shards:
            blob = "".join(s["data"] for s in sorted(shards, key=lambda s: s["part"]))
            try:
                self._map_cache[gid] = json.loads(blob)
            except json.JSONDecodeError:
                self._map_cache[gid] = {}
            return self._map_cache[gid]
        # Legacy single-item map (default graph only, pre-sharding)
        doc = self._read_comm_item("community_map", gid)
        self._map_cache[gid] = (doc or {}).get("map", {})
        return self._map_cache[gid]

    def save_community_map(self, community_map: dict, graph_id: str | None = None) -> None:
        from azure.cosmos import exceptions
        gid = self._gid(graph_id)
        # ensure_ascii=True so 1 char == 1 byte — char-count splitting is then
        # byte-safe (Cosmos measures item size in bytes, not chars).
        blob = json.dumps(community_map, ensure_ascii=True)
        step = self._MAP_SHARD_BYTES
        parts = [blob[i:i + step] for i in range(0, len(blob), step)] or [""]

        # Remove any stale shards / legacy single doc first — SCOPED to this
        # graph_id so other scopes' maps are untouched.
        for old in self._query_all(
            self._communities,
            "SELECT c.id FROM c WHERE c.graph_id = @gid "
            "AND (c.kind = 'map_shard' OR c.id = 'community_map')",
            [{"name": "@gid", "value": gid}]):
            try:
                self._communities.delete_item(item=old["id"], partition_key=gid)
            except exceptions.CosmosResourceNotFoundError:
                pass

        for i, chunk in enumerate(parts):
            self._communities.upsert_item({
                "id": f"map_shard_{i:04d}", "graph_id": gid,
                "kind": "map_shard", "part": i, "total": len(parts), "data": chunk,
            })
        self._map_cache[gid] = community_map

    @staticmethod
    def _summary_id(comm_id: str) -> str:
        return f"summary_{int(comm_id):02d}"

    def save_community_summary(self, comm_id: str, text: str, graph_id: str | None = None) -> str:
        gid = self._gid(graph_id)
        title = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith("##") and len(stripped) > 1:
                title = stripped.lstrip("#").strip()
                break
        self._communities.upsert_item({
            "id": self._summary_id(comm_id), "graph_id": gid,
            "kind": "summary", "comm_id": str(comm_id),
            "title": title, "text": text,
        })
        return f"cosmos://communities/{gid}/{self._summary_id(comm_id)}"

    def get_community_summary(self, comm_id: str, graph_id: str | None = None) -> str | None:
        try:
            doc = self._read_comm_item(self._summary_id(comm_id), graph_id)
        except (ValueError, TypeError):
            return None
        return doc.get("text") if doc else None

    def list_community_summaries(self, graph_id: str | None = None) -> list[dict]:
        # Project only what the UI needs — never pull full summary bodies here
        rows = self._query_all(
            self._communities,
            "SELECT c.id, c.comm_id, c.title, SUBSTRING(c.text, 0, 400) AS preview "
            "FROM c WHERE c.graph_id = @gid AND c.kind = 'summary' ORDER BY c.id",
            [{"name": "@gid", "value": self._gid(graph_id)}],
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
    def get_graph_stats(self, graph_id: str | None = None) -> dict | None:
        doc = self._read_comm_item("graph_stats", graph_id)
        return doc.get("stats") if doc else None

    def save_graph_stats(self, stats: dict, graph_id: str | None = None) -> None:
        self._communities.upsert_item({
            "id": "graph_stats", "graph_id": self._gid(graph_id),
            "kind": "stats", "stats": stats,
        })

    # ── doc registry (metadata scoping) — kind='document' in communities ──
    def save_doc_registry(self, records: list[dict]) -> None:
        for rec in records:
            did = rec.get("doc_id")
            if not did:
                continue
            self._communities.upsert_item({
                **rec, "id": f"doc_{_safe_id(did)}",
                "graph_id": self.GRAPH_ID, "kind": "document",
            })
        self._registry_cache = None

    def get_doc_registry(self) -> dict[str, dict]:
        cached = getattr(self, "_registry_cache", None)
        if cached is not None:
            return cached
        rows = self._query_all(
            self._communities, "SELECT * FROM c WHERE c.kind = 'document'")
        self._registry_cache = {
            d["doc_id"]: _clean(d) for d in rows if d.get("doc_id")}
        return self._registry_cache

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
