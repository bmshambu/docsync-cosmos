"""GraphRAG retrieval — deterministic context assembly for the query agent.

All graph artefacts are read through the GraphStore (file today, Cosmos in
step 2 of the migration). No LLM calls here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings
from app.services.graph_store import DEFAULT_GRAPH_ID, get_graph_store, scope_graph_id

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


# ── Filename-as-question matching ─────────────────────────────────────────────
# Response-library documents are typically NAMED as the question they answer
# ("Describe your firm's partnership with Oracle.pptx"). A title match is a far
# stronger signal than chunk keyword frequency — matched docs' chunks are
# GUARANTEED into the context (their body may use different wording than the
# question, so CONTAINS candidates alone would miss them).

# Words that appear in nearly every question AND every title — matching on
# them would match everything ("Describe…", "Advisory…").
_QUESTION_VERBS = {
    "describe", "explain", "outline", "detail", "provide", "discuss",
    "list", "give", "state", "summarize", "summarise", "advisory",
    "please", "your", "you", "our", "firm", "firms", "company",
}


def _title_words(text: str) -> set[str]:
    return {w for w in query_keywords(text)} - _QUESTION_VERBS


def match_doc_titles(
    question: str,
    titles: list[dict],
    top_n: int = 3,
    threshold: float = 0.4,
) -> list[dict]:
    """Score doc filenames against the question by content-word overlap.

    Returns [{'doc_id','filename','score'}] sorted by score. Requires ≥2
    overlapping content words so a single shared word never triggers a match.
    """
    q_words = _title_words(question)
    if not q_words:
        return []

    scored = []
    for t in titles:
        stem = Path(t.get("filename", "")).stem.replace("_", " ").replace("-", " ")
        t_words = _title_words(stem)
        if not t_words:
            continue
        overlap = q_words & t_words
        score = len(overlap) / len(t_words)
        if len(overlap) >= 2 and score >= threshold:
            scored.append((score, {"doc_id": t["doc_id"], "filename": t["filename"],
                                   "score": round(score, 3)}))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:top_n]]


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

def _to_doc_id(filename: str) -> str:
    """Filename -> doc_id, matching the pipeline (stem, spaces->_)."""
    return Path(filename).stem.replace(" ", "_")


def _in_scope(source_docs, scope: set[str] | None) -> bool:
    """True if any of an entity/community's source_docs is in the scope set.
    scope=None means no scope (everything passes)."""
    if scope is None:
        return True
    return any(_to_doc_id(d) in scope for d in (source_docs or []))


def retrieve(
    question: str,
    settings: Settings,
    query_type: str = "auto",
    top_chunks: int | None = None,       # None → settings.top_chunks
    top_communities: int | None = None,  # None → settings.top_communities
    hops: int = 1,
    platform: str | None = None,          # Platform field only
    service_function: str | None = None,  # Track B scope (Services_Function field)
    scope_value: str | None = None,       # Track A: match EITHER field
) -> dict:
    """Assemble all retrieval context for a question, using targeted store
    queries throughout — nothing here loads the whole corpus.

    `platform` / `service_function` restrict the whole retrieval to documents
    carrying that metadata tag (M1 service-function scoping). Scope is a set of
    doc_ids that intersects every signal — entities, title matches, chunks,
    communities, financial table.

    All caps are tunable via .env: TOP_ENTITIES, TOP_COMMUNITIES, TOP_CHUNKS,
    CHUNK_CANDIDATE_LIMIT (plus MAX_PROMPT_* in the prompt builder)."""
    store = get_graph_store()
    kws = query_keywords(question)

    # Metadata scope — None = whole corpus (today's behaviour). scope_value
    # matches either field (user-selected Platform OR Service Function value).
    scope = store.scoped_doc_ids(
        platform=platform, service_function=service_function, any_value=scope_value,
    )

    # Entity match: backend narrows to candidates, Python ranks, scope filters
    entity_cands = store.search_entity_candidates(kws)
    if scope is not None:
        entity_cands = [e for e in entity_cands if _in_scope(e.get("source_docs"), scope)]
    matched_entities = search_entities(
        question, entity_cands, top_n=settings.top_entities
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

    # Community search for global / hybrid — summaries fetched in ONE call.
    # When a scope is active AND it has a per-scope community graph (item #4),
    # read THAT graph — its communities are already scope-only and summarised
    # about the scope, so no post-filter is needed. Otherwise fall back to the
    # whole-corpus 'default' graph and keep only communities touching in-scope
    # docs (pre-#4 behaviour), so nothing breaks before scopes are built.
    relevant_communities: list = []
    community_graph_id = DEFAULT_GRAPH_ID
    if query_type in ("global", "hybrid"):
        active_scope = scope_value or service_function or platform
        scope_gid = scope_graph_id(active_scope) if active_scope else DEFAULT_GRAPH_ID
        scope_map = (
            store.get_community_map(scope_gid)
            if scope_gid != DEFAULT_GRAPH_ID else {}
        )
        if scope_map.get("communities"):
            community_graph_id = scope_gid
            relevant_communities = search_communities(
                question, scope_map, store.get_all_summaries(scope_gid),
                top_n=top_communities or settings.top_communities,
            )
        else:
            relevant_communities = search_communities(
                question, store.get_community_map(), store.get_all_summaries(),
                top_n=top_communities or settings.top_communities,
            )
            if scope is not None:
                relevant_communities = [
                    (cid, meta, txt) for (cid, meta, txt) in relevant_communities
                    if _in_scope(meta.get("source_docs"), scope)
                ]

    # Filename-as-question: docs whose TITLE matches the question get their
    # best chunks GUARANTEED into the context (their body may use different
    # wording, so keyword candidates alone would miss them). Scope-restricted.
    titles = store.list_doc_titles()
    if scope is not None:
        titles = [t for t in titles if t.get("doc_id") in scope]
    title_matches = match_doc_titles(
        question, titles,
        top_n=settings.title_match_docs,
        threshold=settings.title_match_threshold,
    )
    boosted_chunks: list[dict] = []
    if title_matches:
        by_doc: dict[str, list[dict]] = {}
        for c in store.get_chunks_for_docs([m["doc_id"] for m in title_matches]):
            by_doc.setdefault(c.get("doc_id"), []).append(c)
        for m in title_matches:                       # strongest title match first
            doc_chunks = sorted(by_doc.get(m["doc_id"], []),
                                key=lambda c: c.get("page_start") or 0)
            # best 2 chunks by keyword frequency; if the body shares no query
            # words at all, keep the opening chunk (it usually carries the answer)
            best = rank_chunks(kws, doc_chunks, top_n=2) or doc_chunks[:1]
            boosted_chunks.extend({**c, "title_match": True} for c in best[:2])

    # Chunk search — filtered to matched-entity docs when local, and always
    # intersected with the metadata scope when one is active.
    filter_docs: list[str] | None = None
    if query_type == "local" and matched_entities:
        filter_docs = list({
            _to_doc_id(doc)
            for e in matched_entities[:5]
            for doc in e.get("source_docs", [])
        })
    if scope is not None:
        filter_docs = list(scope if filter_docs is None
                           else (set(filter_docs) & scope))

    n_chunks = top_chunks or settings.top_chunks
    keyword_ranked = rank_chunks(
        kws,
        store.search_chunk_candidates(
            kws, filter_doc_ids=filter_docs, limit=settings.chunk_candidate_limit
        ),
        top_n=n_chunks,
    )

    # Merge: title-matched chunks lead, keyword results fill remaining slots.
    # When keyword results exist, reserve at least one slot for them so the
    # context keeps some diversity beyond the title-matched docs.
    boost_limit = n_chunks if not keyword_ranked else max(1, n_chunks - 1)
    seen_chunks: set = set()
    top_chunk_list: list[dict] = []
    for c in boosted_chunks[:boost_limit] + keyword_ranked:
        key = (c.get("doc_id"), (c.get("text") or "")[:100])
        if key in seen_chunks:
            continue
        seen_chunks.add(key)
        top_chunk_list.append(c)
        if len(top_chunk_list) >= n_chunks:
            break

    # Money/aggregation questions get the COMPLETE financial table — a cheap
    # single-partition query on Cosmos (/type is the partition key). Scoped too.
    financial_table: list[dict] = []
    if is_money_query(question):
        fin_entities = store.get_entities_by_type("financial_instrument")
        if scope is not None:
            fin_entities = [e for e in fin_entities if _in_scope(e.get("source_docs"), scope)]
        financial_table = collect_financial_table(fin_entities)

    return {
        "query_type": query_type,
        "matched_entities": matched_entities,
        "traversal": traversal,
        "relevant_communities": relevant_communities,  # list of (cid, meta, summary_text)
        "top_chunks": top_chunk_list,
        "financial_table": financial_table,
        "title_matched_docs": title_matches,           # [{'doc_id','filename','score'}]
        "scope_doc_count": (len(scope) if scope is not None else None),
        "community_graph_id": community_graph_id,       # which graph communities came from
    }
