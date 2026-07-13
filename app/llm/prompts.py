"""Prompts for the LLM nodes.

Schema v2 (see schema_v2_plan.md): the original 14 audit-RFP entity types plus
4 proposal-response types (methodology, capability, credential, role), and the
original 17 relationship types plus 4 (integrates_with, migrates_to,
partners_with, evidenced_by). Backward compatible — old corpora re-extract
cleanly under v2.
"""

ENTITY_TYPES = [
    # v1 — audit-RFP domain
    "client", "service_provider", "service", "investor", "standard", "regulator",
    "location", "concept", "lender", "financial_instrument", "acquisition_target",
    "technology", "exchange", "deliverable",
    # v2 — proposal-response / implementation domain
    "methodology", "capability", "credential", "role",
]

RELATION_TYPES = [
    # v1
    "requires", "issued_by", "owned_by", "governed_by", "located_in", "operates_in",
    "has_lender", "acquired", "uses", "requires_audit_focus", "mentions",
    "has_deliverable", "listed_on", "has_instrument", "similar_to", "part_of",
    "has_budget",
    # v2
    "integrates_with", "migrates_to", "partners_with", "evidenced_by",
]

EXTRACTION_SYSTEM = """You are an expert knowledge-graph extraction engine for RFP (Request for Proposal) and proposal-response documents.
You read one document at a time and extract structured entities and the typed relationships between them.
You return STRICT JSON only — no prose, no markdown fences, no commentary.
You never invent facts that are not supported by the document text."""

EXTRACTION_USER_TEMPLATE = """Extract entities and relationships from the document below.

# Entity types (use exactly these strings for "type")
{entity_types}

# Relationship types (use exactly these strings for "relation_type")
{relation_types}

# Output format — return ONLY this JSON object, nothing else:
{{
  "entities": [
    {{
      "id": "snake_case_unique_id",
      "name": "Human readable name",
      "type": "one of the entity types above",
      "aliases": ["alternative names"],
      "source_docs": ["{filename}"],
      "attributes": {{ "key": "value pairs relevant to the type" }}
    }}
  ],
  "relationships": [
    {{
      "source": "entity_id",
      "target": "entity_id",
      "relation_type": "one of the relationship types above",
      "source_doc": "{filename}",
      "page": 1,
      "description": "optional one-line context"
    }}
  ]
}}

# Extraction guidelines
- Extract every named entity that has a meaningful relationship to something else — do not extract isolated mentions.
- For standards and regulations (IFRS, ISA, SOX, ISQM, GDPR, etc.) always create a `standard` entity.
- For software products and platforms (Oracle PPM, SAP Field Glass, PowerBI, ServiceNow, cloud platforms, middleware) create `technology` entities; for named modules or components of a product (e.g. Oracle Time and Labor, Item Master, Infolets) create a `technology` entity linked to its parent product with `part_of`.
- For named delivery approaches, frameworks, and process phases (implementation methodologies, CRP / SIT / UAT testing cycles, cut-over plans, data conversion strategies, change management or training approaches, risk/communication plans) create `methodology` entities.
- For things the firm can do or offers (e.g. sourcing via a platform, integration development, managed services) create `capability` entities; link a capability to supporting proof with `evidenced_by`.
- For proof points and qualifications (project counts, go-live counts, awards, partnerships, client references, firm facts such as ownership structure or financial standing) create `credential` entities; capture numbers in `attributes`. For partnerships also add a `partners_with` relationship between the firms.
- For named job roles and security roles (Project Manager, Project Accountant, Resource Manager, system administrator roles) create `role` entities.
- When two systems exchange data or are connected, add an `integrates_with` relationship; when data or projects move from one system to another, add `migrates_to` (source system → target system).
- For locations, extract a country/city only if an entity operates in or is located there.
- For concepts, extract focus areas, complex topics, and reporting themes that fit no more specific type.
- Use snake_case for all entity IDs; replace spaces and special chars with `_`.
- Every `source` and `target` in relationships MUST refer to an `id` you defined in `entities`.
- For any fee budget / cost estimate, create a `financial_instrument` entity and link the relevant `service` to it with `has_budget`.
- Set `source_docs` / `source_doc` to "{filename}" for everything you extract here.
- Use the `page` numbers given in the [page=N] markers in the text.

# Document filename: {filename}

# Document text:
{document_text}
"""


def build_extraction_prompt(filename: str, document_text: str) -> tuple[str, str]:
    """Return (system, user) messages for entity extraction of one document."""
    user = EXTRACTION_USER_TEMPLATE.format(
        entity_types=", ".join(ENTITY_TYPES),
        relation_types=", ".join(RELATION_TYPES),
        filename=filename,
        document_text=document_text,
    )
    return EXTRACTION_SYSTEM, user


# ── Community summariser prompts ──────────────────────────────────────────────

SUMMARY_SYSTEM = """You are a knowledge-graph analyst writing structured community summaries for an RFP analysis system.
Each summary will be used by an AI query agent to answer cross-corpus questions without loading source documents.
Write in clear, professional prose. Be specific — name entities, reference standards by code, cite source documents.
Return ONLY the markdown — no preamble, no trailing commentary, no code fences."""


SUMMARY_USER_TEMPLATE = """Write a structured community summary for the graph cluster below.

## Community {comm_id} — Member entities ({entity_count} total)

{entity_table}

## Internal relationships ({internal_count})

{internal_rels}

## Cross-community connections (top {cross_count})

{cross_rels}

## Source document excerpts (top chunks by relevance)

{chunk_excerpts}

---

Write the summary using EXACTLY this markdown structure (aim for 300–600 words total):

# Community {comm_id} — [Descriptive Theme Title]

## Theme
[2-3 sentences: what is the connecting thread between these entities? Be specific — name the key entities and what binds them. Do NOT just list names.]

## Source Documents
[Bullet list: one line per document. Mark "primary" if the community's main entities come from it, "partial" if only mentioned.]

## Key Entities
[Group by type. Within each group, list the most important first. Use bold for entity names.]

## [1-3 domain-specific sections with descriptive titles]
[Choose titles relevant to the community theme — e.g. "Standards & Compliance", "Technology Stack", "Methodologies & Delivery Approach", "Credentials & Partnerships", "Geographic Footprint", "Financial Structure", "Deliverables".]
[For standards: use a markdown table — Standard | Scope | Why it matters.]
[For methodologies: bullet list of approaches/phases with what each covers.]
[For technology: bullet list of systems/modules with their role and integrations.]

## Cross-community Connections
[Name the connected communities by number (e.g. "Community 3") and their theme. One bullet per connection. Explain what links them.]

## Strategic Significance
[1-2 sentences on why this cluster matters for RFP analysis or proposal-response work — what capability, credential, risk area, or market segment it signals.]
"""


# ── Query agent prompts ───────────────────────────────────────────────────────

QUERY_SYSTEM = """You are an expert RFP knowledge-graph query agent.
You answer questions about RFP documents using ONLY the structured context provided below.
You never invent facts not present in the context.
You are concise and precise — no filler, no introductory preamble."""

QUERY_USER_TEMPLATE = """Answer the question using ONLY the context provided.

# Question
{question}

# Query type: {query_type}

## Matched entities ({entity_count})
{entities_block}

## Relevant relationships ({rel_count})
{rels_block}

## Community summaries ({comm_count})
{communities_block}

## Source chunks ({chunk_count})
{chunks_block}
{financial_block}
---

Answer rules:
- Start directly with the answer — no "Based on the graph…" preamble
- Cite every fact inline: *(filename.docx, p.N)* or *(Community N)*
- Use a markdown **table** when comparing 2+ items across 2+ attributes
- Use **bullet points** for 2-5 discrete facts; prose for a single-sentence answer
- Keep prose under ~150 words (table rows excluded)
- If a "Complete financial table" section is present, it lists EVERY financial instrument in the corpus — use it (not the entity/chunk samples) for any question that filters, compares, ranks, or totals amounts, and enumerate ALL matching rows
- Chunks marked **[TITLE MATCH]** come from documents whose title directly matches the question — treat them as the PRIMARY source for the answer
- ONLY if there are no source chunks AND no matched entities at all: reply "The library has no content on [topic] — this question needs new source material." NEVER open with what is missing when evidence exists — answer from the available evidence first, and if something specific is absent, note the gap in ONE short sentence at the END
- End with exactly: **Also try:** "[follow-up 1]" · "[follow-up 2]"
"""


def build_query_prompt(
    question: str,
    context: dict,
    max_entities: int | None = None,        # None → MAX_PROMPT_ENTITIES from .env
    max_relationships: int | None = None,   # None → MAX_PROMPT_RELATIONSHIPS from .env
) -> tuple[str, str]:
    """Return (system, user) for query synthesis from retrieval context."""
    from app.config import get_settings
    settings = get_settings()
    max_entities = max_entities or settings.max_prompt_entities
    max_relationships = max_relationships or settings.max_prompt_relationships

    entities  = context.get("matched_entities", [])
    rels      = context.get("traversal", {}).get("relationships", [])
    comms     = context.get("relevant_communities", [])   # (cid, meta, text)
    chunks    = context.get("top_chunks", [])

    # Entities block — attributes included (budgets, amounts, scopes live there)
    ent_lines = []
    for e in entities[:max_entities]:
        attrs = "; ".join(f"{k}={v}" for k, v in (e.get("attributes") or {}).items())
        line = (
            f"- **{e.get('name', e['id'])}** [{e.get('type','?')}] "
            f"(source: {', '.join(e.get('source_docs') or [])})"
        )
        if attrs:
            line += f" — {attrs}"
        ent_lines.append(line)
    entities_block = "\n".join(ent_lines) or "_(none matched)_"

    # Relationships block
    seen: set = set()
    rel_lines = []
    for r in rels[:max_relationships]:
        key = f"{r.get('source')}→{r.get('target')}"
        if key not in seen:
            seen.add(key)
            rel_lines.append(
                f"- {r.get('source')} **{r.get('relation_type','→')}** {r.get('target')}"
                f"  *(doc: {r.get('source_doc','?')}, p.{r.get('page','?')})*"
            )
    rels_block = "\n".join(rel_lines) or "_(none)_"

    # Communities block
    comm_lines = []
    for cid, meta, summary_text in comms:
        entities_preview = ", ".join(e["name"] for e in meta.get("entities", [])[:4])
        excerpt = summary_text[:800].strip() if summary_text else "(no summary yet)"
        comm_lines.append(
            f"### Community {cid}\n"
            f"Key entities: {entities_preview}\n\n"
            f"{excerpt}{'…' if len(summary_text) > 800 else ''}"
        )
    communities_block = "\n\n".join(comm_lines) or "_(none matched)_"

    # Chunks block — title-matched docs flagged so the LLM treats them as primary
    chunk_lines = []
    for c in chunks:
        flag = "[TITLE MATCH] " if c.get("title_match") else ""
        chunk_lines.append(
            f"**{flag}{c.get('filename','?')} | p.{c.get('page_start','?')} | {c.get('section','')}**\n"
            f"> {(c.get('text') or '')[:500]}…"
        )
    chunks_block = "\n\n".join(chunk_lines) or "_(none matched)_"

    # Complete financial table — present only for money/aggregation questions
    financial_block = ""
    fin_rows = context.get("financial_table") or []
    if fin_rows:
        lines = ["", f"## Complete financial table — ALL {len(fin_rows)} instruments in the corpus",
                 "", "| Instrument | Currency | Min | Max | Source RFP |", "|---|---|---|---|---|"]
        for r in fin_rows:
            docs = ", ".join(r.get("source_docs") or [])
            extra = "; ".join(f"{k}={v}" for k, v in (r.get("attributes") or {}).items())
            name = r.get("name", "?") + (f" ({extra})" if extra else "")
            lines.append(
                f"| {name} | {r.get('currency','')} | {r.get('min_amount','')} "
                f"| {r.get('max_amount','')} | {docs} |"
            )
        financial_block = "\n".join(lines) + "\n"

    user = QUERY_USER_TEMPLATE.format(
        question=question,
        query_type=context.get("query_type", "auto").upper(),
        entity_count=len(entities),
        entities_block=entities_block,
        rel_count=len(rels),
        rels_block=rels_block,
        comm_count=len(comms),
        communities_block=communities_block,
        chunk_count=len(chunks),
        chunks_block=chunks_block,
        financial_block=financial_block,
    )
    return QUERY_SYSTEM, user


def build_summary_prompt(
    comm_id: str,
    entities: list[dict],
    internal_rels: list[dict],
    cross_rels: list[dict],
    chunk_excerpts: list[dict],
) -> tuple[str, str]:
    """Build (system, user) messages for summarising one community."""

    # Entity table
    rows = []
    for e in entities:
        docs = ", ".join(e.get("source_docs") or [])
        attrs = "; ".join(f"{k}={v}" for k, v in (e.get("attributes") or {}).items())
        rows.append(f"- **{e.get('name', e['id'])}** [{e.get('type', '?')}] | {docs}" +
                    (f" | {attrs}" if attrs else ""))
    entity_table = "\n".join(rows) if rows else "_(no entities)_"

    # Internal relationships
    def _rel_line(r):
        return (f"- {r.get('source')} **{r.get('relation_type','→')}** {r.get('target')}"
                f"  *(doc: {r.get('source_doc','?')}, p.{r.get('page','?')})*")

    internal_block = "\n".join(_rel_line(r) for r in internal_rels[:20]) or "_(none)_"
    cross_block    = "\n".join(_rel_line(r) for r in cross_rels[:10]) or "_(none)_"

    # Chunk excerpts
    chunk_block_parts = []
    for c in chunk_excerpts:
        excerpt = (c.get("text") or "")[:600].strip()
        chunk_block_parts.append(
            f"**{c.get('filename','?')} | p.{c.get('page_start','?')} | {c.get('section','')}**\n> {excerpt}…"
        )
    chunk_block = "\n\n".join(chunk_block_parts) if chunk_block_parts else "_(no chunks found)_"

    user = SUMMARY_USER_TEMPLATE.format(
        comm_id=comm_id,
        entity_count=len(entities),
        entity_table=entity_table,
        internal_count=len(internal_rels),
        internal_rels=internal_block,
        cross_count=len(cross_rels),
        cross_rels=cross_block,
        chunk_excerpts=chunk_block,
    )
    return SUMMARY_SYSTEM, user
