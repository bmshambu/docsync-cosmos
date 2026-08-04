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
{subquestions_block}
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
- Chunks are labeled by TRACK when two were searched: the selected platform track (e.g. **[ORACLE]**) carries technical approach / methodology / configuration; the **[CLIENTS AND MARKETS]** track carries client references, credentials, and market proof. Draw the "how we do it" from the platform track and the "proof / who we've done it for" from the Clients and Markets track, and make clear in the answer which track each part came from. A chunk labeled **[ORACLE + CLIENTS AND MARKETS]** belongs to both — cite it once.
{decompose_rule_block}- ONLY if there are no source chunks AND no matched entities at all: reply "The library has no content on [topic] — this question needs new source material." NEVER open with what is missing when evidence exists — answer from the available evidence first, and if something specific is absent, note the gap in ONE short sentence at the END
- Then, on its own line: **Also try:** "[follow-up 1]" · "[follow-up 2]"
- Finally, on the LAST line, rate how well the provided evidence (chunks, entities, communities) actually supports your answer, as: **Confidence:** <score> — <one short sentence of reasoning>. The score is a number from 0.00 to 1.00: 1.00 = the evidence directly and fully answers the question; ~0.5 = partial or indirect support; 0.00 = no real support (e.g. you had to say the library has no content). Judge the EVIDENCE's support for the answer, not how well-written the answer is.
"""


def build_query_prompt(
    question: str,
    context: dict,
    max_entities: int | None = None,        # None → MAX_PROMPT_ENTITIES from .env
    max_relationships: int | None = None,   # None → MAX_PROMPT_RELATIONSHIPS from .env
    sub_questions: list[str] | None = None, # M3: >1 items → question was decomposed
) -> tuple[str, str]:
    """Return (system, user) for query synthesis from retrieval context.

    ``sub_questions``: when the planner split a compound question into up to 3
    self-contained parts, pass that list so the model sees exactly what each
    part was and answers all of them. None/single-item → today's prompt,
    unchanged (the overwhelming majority of questions)."""
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

    # Chunks block — title-matched docs flagged as primary; track label (M2
    # dual-track) shows whether content came from the selected Platform track or
    # the Clients & Markets track so the answer can attribute each fact.
    chunk_lines = []
    for c in chunks:
        flags = ""
        if c.get("track_label"):
            flags += f"[{c['track_label'].upper()}] "
        if c.get("title_match"):
            flags += "[TITLE MATCH] "
        chunk_lines.append(
            f"**{flags}{c.get('filename','?')} | p.{c.get('page_start','?')} | {c.get('section','')}**\n"
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

    # M3 decomposition — only rendered when the question was genuinely split
    # into 2-3 parts; both blocks are "" otherwise, leaving the prompt
    # byte-identical to before decomposition existed.
    subquestions_block = ""
    decompose_rule_block = ""
    if sub_questions and len(sub_questions) > 1:
        numbered = "\n".join(f"{i + 1}. [Q{i + 1}] {q}" for i, q in enumerate(sub_questions))
        subquestions_block = (
            f"\nThis question was interpreted as {len(sub_questions)} separate parts:\n"
            f"{numbered}\n"
        )
        decompose_rule_block = (
            "- This question has multiple parts (listed above) — chunks/entities are "
            "labeled **[Q1]**, **[Q2]**, **[Q3]** for which part they support (relevant to "
            "more than one part → **[Q1 + Q2]**). Answer EVERY part, each clearly separated "
            "(its own short paragraph or bullet group, in the order given). If one part lacks "
            "evidence, say so for THAT part only — a gap in one part must not suppress the "
            "answer to the others.\n"
        )

    user = QUERY_USER_TEMPLATE.format(
        question=question,
        subquestions_block=subquestions_block,
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
        decompose_rule_block=decompose_rule_block,
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


# ── RFP question extraction (Tab 4 — RFP upload) ─────────────────────────────
# A three-stage pipeline (see rfp_questions.py): EXTRACT candidate questions →
# DECOMPOSE compound asks into standalone parts → FILTER out non-answerable
# (administrative / form / attachment / pointer-only) items. Ported from the
# proven office extractor; the "GPS" (their answer system) wording is swapped
# for our domain ("the response / proposal library").

# ── Stage 1: extract ─────────────────────────────────────────────────────────

RFP_EXTRACT_SYSTEM = (
    "You extract, from a client RFP (Request for Proposal), the questions a "
    "bidder must answer with a substantive, unique response in their proposal. "
    "You never invent, answer, or summarise; you preserve the source wording. "
    "You return STRICT JSON only — no prose, no markdown fences."
)

RFP_EXTRACT_USER_TEMPLATE = """Analyze the following content and extract specific questions or statements that explicitly require a detailed, unique response from the bidder in their RFP proposal. Focus exclusively on items that:
- Directly ask the bidder to provide specific information, plans, or solutions.
- Explicitly request the bidder to describe their approach, methodology, or qualifications.
- Clearly indicate that the bidder must demonstrate or explain something in their proposal.

Pay special attention to sentences or questions that begin with these words or phrases:
Who, What, When, Where, Why, How, Please, Describe, Explain, Clarify, Outline, Demonstrate, Define, Illustrate, Summarize, Indicate, List, Specify, Detail, Include, Present, Provide.

Also look for common RFP question phrases such as:
How many, In what way, Can you, Does your, Do you have, Does your firm, Provider should, Provider needs, Provider must, What are the steps, What is the process for, How will you ensure, Can you provide examples of, What steps are required to, What is your approach to, How do you handle, What are your qualifications in, What experience do you have with, What support will be provided, Can you outline the timeline for, What measures do you take to, what are the key deliverables, Can you demonstrate, what assurances can you provide.

Punctuation as indicators:
- A question mark ("?") typically indicates a direct question.
- A colon (":") may introduce a request for detailed information.
- Bullet points following an introductory question often indicate requests for specific information.
- When a PARENT question introduces sub-bullets, MERGE each sub-bullet into one full standalone question that includes the parent topic (do NOT output the parent and sub-bullets as separate incomplete lines).

Do NOT extract as questions:
- Submission, formatting, signature, attachment, or portal instructions.
- Section headings, column headers, table labels, or exhibit titles standing alone.
- Orphaned sub-bullets, single-word prompts, or fragments that depend on nearby text to make sense.
- Lines that only say to provide details, describe, or explain without stating what topic is in scope.
- Administrative or vendor-profile fields (identifiers, insurance forms, contact blocks, acknowledgments).
- Executive summary or why-the-firm-should-be-selected narrative requests (proposal boilerplate, not methodology).
- Proposed fieldwork, reporting, or engagement delivery timelines (scheduling milestones, not how work is done).
- Representative client lists, reference rosters, or comparable-engagement tables (org names, tenure, services history).

For each extracted item:
- It must be a complete, self-contained sentence that states the topic AND what the bidder must address; skip it if you cannot rewrite it that way from the source text.
- It must require a substantive, unique response that would be a distinct section or point in the proposal.

Format the output as a JSON object:
{{"questions": ["Question 1", "Question 2"]}}

If no questions requiring a specific response are found, return {{"questions": []}}.
Extract a maximum of 200 questions. Do NOT include any example questions in the output.

Content to analyze:
\"\"\"
{content}
\"\"\"
"""


def build_rfp_extract_prompt(content: str) -> tuple[str, str]:
    """(system, user) to extract biddable questions from one RFP text window."""
    return RFP_EXTRACT_SYSTEM, RFP_EXTRACT_USER_TEMPLATE.format(content=content)


# ── Stage 2: decompose compound questions ────────────────────────────────────
# NOTE: this splitting logic is MIRRORED in the query planner's task 4
# (PLANNER_USER_TEMPLATE / query_planner.py) so the Query tab and CSV batch
# split questions the same way. This one runs as a batched call over a numbered
# LIST of extracted RFP questions; the planner runs it inline over ONE typed
# question. If you tune the rules here, UPDATE THE PLANNER TOO (and vice versa).

RFP_DECOMPOSE_SYSTEM = (
    "You split compound RFP questions into standalone, separately-answerable "
    "parts, applying a strict standalone test. You return STRICT JSON only."
)

RFP_DECOMPOSE_USER_TEMPLATE = """Review each extracted RFP question below. Some combine multiple distinct asks in one sentence (compound questions). Retrieval embeds the whole string once, so a strong part and a weak part dilute the combined match score — split only when separate parts would each retrieve different response-library content.

For each item, decide whether to KEEP it as one question or SPLIT it into separate standalone questions.

SPLIT only when ALL of these are true:
- The item contains two or more DISTINCT substantive asks joined by "and", ";", "as well as", or parallel phrasing (e.g. "Describe X and explain Y" where X and Y are different topics) — not one topic rephrased.
- Each ask is about a different service area, methodology, role, module, integration, or deliverable.
- Every emitted part is a COMPLETE, self-contained question a reader could answer without seeing the other parts — no pronouns/pointers that depend on the other clause ("those expenses", "the above", "it", "them", "such services") without naming the topic.
- Each emitted part is substantive (methodology, approach, qualifications, service delivery, risk, etc.) — not administrative.
- Each emitted part would likely map to a different section of the response / proposal library.

Do NOT split when:
- Only one real substantive ask is present, even if the sentence is long or has supporting detail.
- A later clause is an orphan/fragment that loses meaning without the earlier clause (e.g. "…and a description of how your firm charges for those expenses" depends on the expenses topic from the first clause).
- Both clauses ask for the same answer topic / would match the same content.
- The line is mostly administrative (submission, formatting, attachments, signatures, W-9, portal, yes/no).
- Splitting would create incomplete stubs like "Provide details" or "Describe how you charge" with no topic.
- You are uncertain — default to KEEP the original compound question unchanged.
- If one clause is administrative, OMIT that clause; do not emit it as a searchable question.

When splitting, rewrite each part as a complete standalone sentence with explicit topic and ask.

Examples:
- SPLIT: "Describe your requirements for national office consultations and how you expect national office personnel will be involved in your audit."
  -> "Describe your requirements for national office consultations."
  -> "Describe how you expect national office personnel will be involved in your audit."
- KEEP: "A description of expenses that might be necessary to perform the external audit services and a description of how your firm charges for those expenses." (second clause not clearly standalone)
- SPLIT + drop admin: "Describe your audit methodology and attach a signed W-9 form." -> "Describe your audit methodology." (do NOT emit the W-9 attachment)
- KEEP: "Explain your approach to data conversion and how converted data will be validated before load." (one integrated data-conversion topic)

Return JSON only:
{{"items": [
  {{"index": 1, "action": "keep", "questions": ["original question unchanged"]}},
  {{"index": 2, "action": "split", "questions": ["standalone part 1", "standalone part 2"]}}
]}}

Use "keep" with a single-element array when unchanged. Use "split" when emitting two or more standalone substantive parts. If admin text is dropped and only one substantive part remains, use "keep" with that single part. Include exactly ONE entry per numbered item below (1-based index).

Items:
{numbered}
"""


def build_rfp_decompose_prompt(numbered: str) -> tuple[str, str]:
    """(system, user) to keep/split a numbered list of extracted questions."""
    return RFP_DECOMPOSE_SYSTEM, RFP_DECOMPOSE_USER_TEMPLATE.format(numbered=numbered)


# ── Stage 3: answerability filter (was "GPS routing") ────────────────────────

RFP_FILTER_SYSTEM = (
    "You route extracted RFP items: SEND those a proposal writer could answer "
    "from the response / proposal library, and SUPPRESS administrative, "
    "form-based, or non-answerable items. You return STRICT JSON only."
)

RFP_FILTER_USER_TEMPLATE = """Review each extracted RFP item below and decide whether it should be sent to the response-library answer matching.

Classify every item as exactly one of:
- "SEND" — a substantive, standalone question a proposal writer could answer from response-library content (methodology, approach, qualifications, service delivery, risk, staffing model, etc.).
- "SUPPRESS" — do not send.

When uncertain, choose "SUPPRESS".

Use "SUPPRESS" if the item is administrative, instructional, form-based, attachment-based, or not a standalone answerable question. Suppress items that:
- Give submission or layout directions rather than asking for technical or service content.
- Require document handling only (upload a blank form, sign an exhibit, attach a template with no firm facts to state).
- Seek blanket agreement with solicitation rules, legal terms, or compliance statements with no deliverable to describe.
- Are fragmented follow-up lines: section titles, lone labels, one- or two-word stubs, or sub-bullets split from a parent.
- Use unclear pointers (this, that, them, it, above, below, prior section, as noted) without naming the subject.
- Could only be understood by reading another row, table cell, or earlier bullet in the RFP.
- Ask only "provide details," "explain," or "describe" with no topic, or restate a heading as if it were a full question.
- Target buyer-owned facts, requisition metadata, or incumbent relationships the responder cannot answer from firm knowledge.
- Cover procurement mechanics (timelines for questions, protests, amendments, or how/where/when to deliver the proposal).
- Focus on commercial worksheets, bonds, or fee schedules where the ask is pricing structure — not how work is performed.
- Expect a simple confirmation (yes/no, initial, or check) with no request for narrative explanation.
- Duplicate or restate instructions already implied by the solicitation template without a new substantive ask.
- Ask only for organizational charts or bare resume uploads with no firm fact, credential, or reference requested.
- Refer to external documents, appendices, or prior RFP sections without stating what must be explained in the response.
- Request a proposal executive summary or why-the-firm-should-be-selected narrative (selection rationale, not methodology).
- Ask for engagement scheduling only (proposed fieldwork dates, reporting milestones, delivery timelines).
- Ask for representative client rosters, reference lists, or comparable-organization experience tables.

Do NOT suppress firm identifiers or insurance coverage requests.

Send ONLY when the item is a complete, self-contained question a reader who has not seen the RFP page could still understand, AND a knowledgeable proposal writer could draft a meaningful narrative answer from response-library content — not from bespoke client lists, engagement calendars, or buyer-owned facts.

Return JSON only:
{{"items": [
  {{"index": 1, "decision": "SEND"}},
  {{"index": 2, "decision": "SUPPRESS"}}
]}}

Include exactly ONE entry per numbered item below, using the same 1-based index numbers.

Items:
{numbered}
"""


def build_rfp_filter_prompt(numbered: str) -> tuple[str, str]:
    """(system, user) to classify a numbered list SEND/SUPPRESS for answering."""
    return RFP_FILTER_SYSTEM, RFP_FILTER_USER_TEMPLATE.format(numbered=numbered)
