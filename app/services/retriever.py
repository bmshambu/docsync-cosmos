"""GraphRAG retrieval — deterministic context assembly for the query agent.

All graph artefacts are read through the GraphStore (file today, Cosmos in
step 2 of the migration). No LLM calls here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings
from app.services.graph_store import get_graph_store

_STOP_WORDS = {
    "the", "and", "for", "are", "was", "with", "that", "this", "have",
    "from", "they", "will", "been", "what", "which", "how", "not", "but",
}


# ── Keyword extraction ────────────────────────────────────────────────────────

def query_keywords(query: str) -> list[str]:
    return [
        k for k in re.findall(r"\w+", query.lower())
        if k not in _STOP_WORDS and len(k) > 2
    ]


# ── Entity search ─────────────────────────────────────────────────────────────

def search_entities(query: str, entities: list[dict], top_n: int = 10) -> list[dict]:
    """Score candidate entities against the query. Candidates come from
    store.search_entity_candidates() — this only ranks, it never scans the corpus."""
    keywords = query_keywords(query)
    if not keywords:
        return []

    scored = []
    for e in entities:
        name_alias = " ".join(
            [e.get("name", "")] + list(e.get("aliases") or [])
        ).lower()
        name_words = set(re.findall(r"\w+", name_alias))
        type_text  = (e.get("type") or "").lower()
        attr_text  = json.dumps(e.get("attributes") or {}).lower()

        score = 0
        for kw in keywords:
            if kw in name_words:            # exact word in name/alias — strongest signal
                score += 3
            elif kw in name_alias:          # substring of name/alias
                score += 2
            if kw in type_text:
                score += 1
            if kw in attr_text:
                score += 1
        if score > 0:
            scored.append((score, e))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top_n]]


# ── Graph traversal ───────────────────────────────────────────────────────────

def get_neighbours(store, entity_ids: set, hops: int = 1) -> dict:
    """Per-hop traversal via targeted store queries — never loads all edges."""
    visited   = set(entity_ids)
    frontier  = set(entity_ids)
    result_rels: list[dict] = []
    seen_rels: set = set()

    for _ in range(hops):
        if not frontier:
            break
        next_frontier: set = set()
        for r in store.get_relationships_for(frontier):
            key = (r.get("source"), r.get("target"), r.get("relation_type"), r.get("source_doc"))
            if key not in seen_rels:
                seen_rels.add(key)
                result_rels.append(r)
            next_frontier.add(r.get("source"))
            next_frontier.add(r.get("target"))
        frontier  = next_frontier - visited
        visited  |= next_frontier

    return {"entity_ids": list(visited), "relationships": result_rels}


# ── Chunk search ──────────────────────────────────────────────────────────────

def rank_chunks(keywords: list[str], candidates: list[dict], top_n: int = 5) -> list[dict]:
    """Rank chunk candidates by keyword frequency. Candidates come from
    store.search_chunk_candidates() — this only ranks."""
    scored = []
    for chunk in candidates:
        text  = (chunk.get("text") or "").lower()
        score = sum(text.count(kw) for kw in keywords)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_n]]


# ── Community search ──────────────────────────────────────────────────────────

def search_communities(
    query: str,
    community_map: dict,
    summaries: dict[str, str],
    top_n: int = 3,
) -> list[tuple[str, dict, str]]:
    """Returns list of (comm_id, comm_meta, summary_text) sorted by relevance.

    ``summaries`` comes from store.get_all_summaries() — ONE backend call,
    never per-community point reads (pathologically slow on Cosmos).
    """
    keywords = re.findall(r"\w+", query.lower())
    communities = community_map.get("communities", {})
    scored = []

    for comm_id, comm in communities.items():
        entity_text  = " ".join(
            f"{e['name']} {e['type']}" for e in comm.get("entities", [])
        ).lower()
        summary_text = comm.get("summary", "").lower()

        full_summary = summaries.get(str(comm_id), "")
        if full_summary:
            summary_text += " " + full_summary.lower()

        full_text = entity_text + " " + summary_text
        score = sum(full_text.count(kw) for kw in keywords)
        if score > 0:
            scored.append((score, comm_id, comm, full_summary))

    scored.sort(key=lambda x: -x[0])
    return [(cid, c, s) for _, cid, c, s in scored[:top_n]]


# ── Financial aggregation ─────────────────────────────────────────────────────

_MONEY_SIGNALS = [
    "budget", "cost", "fee", "fees", "price", "pricing", "amount", "spend",
    "million", "thousand", "usd", "sgd", "eur", "gbp", "expensive", "cheapest",
    "indicative",
]


def is_money_query(query: str) -> bool:
    q = query.lower()
    return any(s in q for s in _MONEY_SIGNALS)


def collect_financial_table(entities: list[dict]) -> list[dict]:
    """Return ALL financial_instrument entities as structured rows.

    Aggregation questions ("which RFPs have budget over 2M?") need the complete
    set — keyword top-N retrieval can only ever surface a sample, and text
    scoring cannot evaluate numeric comparisons. The LLM filters over this
    table instead.
    """
    rows = []
    for e in entities:
        if e.get("type") != "financial_instrument":
            continue
        attrs = e.get("attributes") or {}
        rows.append({
            "name": e.get("name", e.get("id", "?")),
            "currency": attrs.get("currency", ""),
            "min_amount": attrs.get("minimum_amount"),
            "max_amount": attrs.get("maximum_amount"),
            "attributes": {k: v for k, v in attrs.items()
                           if k not in ("currency", "minimum_amount", "maximum_amount")},
            "source_docs": e.get("source_docs") or [],
        })
    # Sort by max (then min) amount descending so biggest budgets lead
    def _amt(r):
        for v in (r["max_amount"], r["min_amount"]):
            if isinstance(v, (int, float)):
                return v
            if isinstance(v, str):
                digits = re.sub(r"[^\d.]", "", v)
                if digits:
                    try:
                        return float(digits)
                    except ValueError:
                        pass
        return -1
    rows.sort(key=_amt, reverse=True)
    return rows


# ── Query classifier ──────────────────────────────────────────────────────────

def classify_query(query: str, matched_entities: list[dict]) -> str:
    q = query.lower()
    global_signals = ["all", "across", "compare", "which rfp", "common", "trend",
                      "every", "both", "overall", "summary", "list all", "how many"]
    local_signals  = ["in the", "for halcyon", "for meridian", "rfp_", "in rfp",
                      "what is", "what are", "specific", "detail"]
    has_global = any(s in q for s in global_signals)
    has_local  = any(s in q for s in local_signals) or len(matched_entities) <= 2
    if has_global and has_local:
        return "hybrid"
    if has_global:
        return "global"
    return "local"


# ── Full retrieval pipeline ───────────────────────────────────────────────────

def retrieve(
    question: str,
    settings: Settings,
    query_type: str = "auto",
    top_chunks: int | None = None,       # None → settings.top_chunks
    top_communities: int | None = None,  # None → settings.top_communities
    hops: int = 1,
) -> dict:
    """Assemble all retrieval context for a question, using targeted store
    queries throughout — nothing here loads the whole corpus.

    All caps are tunable via .env: TOP_ENTITIES, TOP_COMMUNITIES, TOP_CHUNKS,
    CHUNK_CANDIDATE_LIMIT (plus MAX_PROMPT_* in the prompt builder)."""
    store = get_graph_store()
    kws = query_keywords(question)

    # Entity match: backend narrows to candidates, Python ranks them
    matched_entities = search_entities(
        question, store.search_entity_candidates(kws), top_n=settings.top_entities
    )

    if query_type == "auto":
        query_type = classify_query(question, matched_entities)

    # Graph traversal for local / hybrid — per-hop targeted edge queries
    traversal: dict = {"entity_ids": [], "relationships": []}
    if query_type in ("local", "hybrid") and matched_entities:
        seed_ids = {e["id"] for e in matched_entities[:5]}
        traversal = get_neighbours(store, seed_ids, hops=hops)

        # Rank edges by query relevance — the prompt keeps only ~15, and a
        # 2-hop expansion can return hundreds; without ranking the cutoff is
        # arbitrary and direct facts get displaced by second-order noise.
        if kws and traversal["relationships"]:
            def _rel_score(r: dict) -> int:
                text = " ".join([
                    str(r.get("source", "")), str(r.get("target", "")),
                    str(r.get("relation_type", "")), str(r.get("description", "")),
                ]).lower()
                return sum(1 for k in kws if k in text)
            traversal["relationships"].sort(key=_rel_score, reverse=True)

    # Community search for global / hybrid — summaries fetched in ONE call
    relevant_communities: list = []
    if query_type in ("global", "hybrid"):
        relevant_communities = search_communities(
            question, store.get_community_map(), store.get_all_summaries(),
            top_n=top_communities or settings.top_communities,
        )

    # Chunk search — filtered to matched-entity docs when local
    filter_docs: list[str] | None = None
    if query_type == "local" and matched_entities:
        filter_docs = list({
            Path(doc).stem.replace(" ", "_")
            for e in matched_entities[:5]
            for doc in e.get("source_docs", [])
        })

    top_chunk_list = rank_chunks(
        kws,
        store.search_chunk_candidates(
            kws, filter_doc_ids=filter_docs, limit=settings.chunk_candidate_limit
        ),
        top_n=top_chunks or settings.top_chunks,
    )

    # Money/aggregation questions get the COMPLETE financial table — a cheap
    # single-partition query on Cosmos (/type is the partition key)
    financial_table: list[dict] = []
    if is_money_query(question):
        financial_table = collect_financial_table(
            store.get_entities_by_type("financial_instrument")
        )

    return {
        "query_type": query_type,
        "matched_entities": matched_entities,
        "traversal": traversal,
        "relevant_communities": relevant_communities,  # list of (cid, meta, summary_text)
        "top_chunks": top_chunk_list,
        "financial_table": financial_table,
    }
