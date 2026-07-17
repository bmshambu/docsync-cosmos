# Schema v2 Plan — PoC → Real Data (Proposal-Response Library)

*Planning doc, 2026-07-13. No implementation yet.*

## Context — what changed

The deployed solution now runs against **real data**, and two things shifted at once:

1. **Corpus type**: from *incoming audit RFPs* (clients, lenders, IFRS, acquisitions)
   to **KPMG's proposal-response library** — answer decks like
   "Describe your firm's partnership with Oracle.pptx",
   "Cloud conversion framework overview.pptx".
   **Each document is literally named as the question it answers** — a huge
   retrieval signal the current system ignores.
2. **Question type**: users now ask technology-implementation RFP questions —
   methodology (CRP / SIT / UAT / cut-over / change management), Oracle module
   configuration (Time & Labor, HCM, Project Analytics, Infolets), integrations
   (SAP Field Glass, GCP, middleware), data conversion (MPO → Oracle PPM,
   ~300 projects), security roles, SOX / segregation of duties, firm facts
   ("stand-alone or subsidiary?"), credentials, PowerBI / accelerators.

**Observed failure** (production screenshot): answer led with
"Graph doesn't have enough on Oracle Item Master, blanket purchase agreements,
or Source-to-Pay" then salvaged bullets from **chunk** search alone — the
entity/graph layer contributed nothing. Retrieval worked; the entity layer missed.

## Gap analysis — current 14 entity types vs new questions

| Question theme | Fit | Gap |
|---|---|---|
| Oracle modules, PowerBI, GCP, Ivalua, Field Glass | `technology` + `part_of` ✅ | extraction prompt (audit-flavored) missed them, not the schema |
| Methodology/approach (CRP, SIT, UAT, cut-over, Powered) | `concept` catch-all ❌ | **biggest gap** — most questions are "describe your approach to X" |
| Integrations (PPM ↔ Field Glass ↔ GCP) | — ❌ | needs `integrates_with` |
| Migration (MPO → Oracle PPM) | — ❌ | needs `migrates_to` |
| Security roles (Project Manager, Project Accountant…) | — ❌ | needs `role` |
| Credentials ("170+ Oracle go-lives", Oracle partnership) | — ❌ | needs `credential` + `partners_with` |
| Capabilities/offerings (sourcing via Ivalua) | `service` 🟨 | broaden guidance or add `capability` |
| SOX, segregation of duties | `standard`/`regulator` ✅ | fine |
| Deliverables (design docs, dashboards) | `deliverable` ✅ | fine |

**Separate axis — corpus coverage**: several questions are client-project-specific
(their 300 MPO projects, their role audit). If the library has no content on a
topic, no schema fixes that. Expectation-setting matters.

## Plan

### Phase 0 — Measure before modifying (~½ day, near-zero LLM cost)
- Run all extracted user questions through `retrieve()` against the real graph;
  record per question: entities matched, chunks found, which layer produced signal.
- Dump the entity-type histogram of the real extraction.
- Output: a per-question gap matrix separating *schema gaps* from
  *extraction-prompt gaps* from *corpus-coverage gaps*.

### Phase 3 — Filename-as-question retrieval — ✅ DONE 2026-07-13
- `match_doc_titles()` in retriever.py: content-word overlap on filenames with
  question-verb stripping (describe/explain/advisory/… would match everything),
  ≥2-word overlap requirement, length-normalised score, threshold 0.4.
- Matched docs' chunks are **guaranteed** into the context (their body may use
  different wording than the question — CONTAINS candidates alone would miss
  them); at least one slot stays reserved for keyword-ranked results.
- Store methods: `list_doc_titles()` (cached; Cosmos DISTINCT query) and
  `get_chunks_for_docs()` (partition-scoped on /doc_id).
- Prompt: `[TITLE MATCH]` flag on boosted chunks + rule to treat them as the
  primary source. "Graph doesn't have enough…" replaced: negative opener only
  when there are NO chunks AND NO entities; otherwise answer from evidence and
  note gaps in one sentence at the END.
- Response + UI: `matched_documents` payload; "📄 Matching documents" strip
  above the answer with blob doc-open links.
- Env knobs: `TITLE_MATCH_DOCS=3`, `TITLE_MATCH_THRESHOLD=0.4`.
- Verified: matcher unit tests (incl. verb-trap and single-word-overlap false
  positives), file + Cosmos backends, live browser query showing the matched-
  documents strip with working links.

### Phase 1 — Schema v2: extend, don't replace — ✅ DONE 2026-07-13
- Implemented in `cosmos-rag/app/llm/prompts.py` (single file — port this one
  file to the deployed repo):
  - Entity types +4: `methodology`, `capability`, `credential`, `role`
    (modules stay `technology` + `part_of`).
  - Relationship types +4: `integrates_with`, `migrates_to`, `partners_with`,
    `evidenced_by`.
  - Extraction guidelines rewritten for both domains (one generalized prompt,
    not a per-corpus variant): modules→technology+part_of, methodologies
    (CRP/SIT/UAT/cut-over), roles, credentials-with-numbers, integrations,
    partnerships. Audit guidance retained — backward compatible.
  - Summary prompt: "Source RFPs"→"Source Documents", section suggestions now
    include "Methodologies & Delivery Approach" and "Credentials & Partnerships".
- **Verified with a real Gemini call** on synthetic response-library text:
  extracted 5 methodologies, 3 roles, 3 credentials, 1 capability, 6
  technologies, and used all 4 new relationship types.

**Decision (user, 2026-07-13):** full clean run — re-extract ALL files with
schema v2 + regenerate ALL community summaries (select-all in both tabs;
"Re-extract selected (overwrite)" checkbox purges old entities first).

### Phase 2 — Re-extract the real corpus (1 LLM call per doc)
- Force re-extract (purge machinery exists), rebuild graph, re-summarise
  communities, regenerate theme data.

### Phase 4 — Roadmap flag (not scoped): batch question answering
- Users arrive with a *list* of questions (24 extracted). End-state feature:
  upload the list → answer all with citations → export a draft response doc.
  Influences Phase 3 design; ties into theme partitioning + theme contexts
  (see migration_strategy.md).

**Recommended order: 0 → 3 → 1 → 2.** Measure, grab the cheap filename win,
then change the schema, then pay for re-extraction once.

---

## Reviewer feedback round 2 (2026-07-14) — corpus-role separation

Reviewer clarified after deployment: **client RFPs are INPUT (question docs),
never a retrieval source**. Answers must come from proposal-response content
(past answers, methodology write-ups, boilerplate, case studies). Root cause of
"answers pull from the client RFP": the RFP was uploaded into the responses
folder — since the questions came FROM that document, it won every keyword
match, and answers restated requirements instead of explaining approach.

**Stage A — DONE 2026-07-14** (versioned per `version_history.md`):
- `scripts/remove_doc.py` — evict a document (entities/relationships/chunks +
  graph rebuild) without re-paying for extraction. Use it to remove the RFP,
  then delete the file from blob and re-run the Community Summariser.
- Scan-UI warning when "RFP"-named files are about to be ingested.
- Bug fix found during testing: `purge_doc_data` deleted entities that had no
  recorded source_docs (collateral damage on every purge / force re-extract).
- No blob restructuring — decision: client RFPs simply are not ingested.

**Stage B — later**: service-line filtering (tag chunks/entities with
`service_line` from blob folder; Query-tab selector; Cosmos WHERE filter).
Reviewer: "Oracle RFPs search Oracle content only."

**Stage C — later (REDEFINED by user)**: CSV question-answering tab — upload a
CSV of questions (already extracted; no RFP parsing needed), configure
retrieval settings, answer each through the existing planner/retrieve/
synthesise pipeline, return the CSV with answers + citations + "no content"
flags. Output feeds proposal/PPT creation. Reviewer's ~0.8 similarity
threshold maps to labelling answers "from answer library (strong title/keyword
match)" vs "synthesised from corpus"; a true embedding threshold arrives with
DiskANN vector search later.

**Also from feedback (open)**: "richer proposal sections — process steps,
tools, deliverables, not just scope lines" → neighbour-chunk expansion (pull
the chunk following a strong hit); the v2 methodology/deliverable entities from
the user's re-extraction should also lift this.
