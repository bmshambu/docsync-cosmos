# Service-Function Metadata Plan — Dual-Track Graph Retrieval

*Planning doc, 2026-07-21. Agreed design, not yet implemented.*

## Context

Office production reality (cannot change): **one blob container, no folders** —
all service-function documents together. Every blob carries **metadata
key-values**: `Services_Function_Capabilities` (list — includes the value
"Clients and Markets"), `Industry_Sector_Market` (list), `Keywords` (list),
`Platform` (list), `Title`, `Short_Description`, `webUrl`, `FileDownloadUrl`,
`CreatedTime`, `ModifiedTime`, `IsDeleted`, `FileId`, Templafy IDs.

Team's current (old-tech) process we are benchmarking against and improving
with the graph:
1. User preselects a service function in the UI (e.g. "Oracle") — there are
   **no cross-SF questions**; the SF is always given.
2. Per question, **two searches run in parallel**: (a) docs tagged with the
   selected SF, (b) docs tagged **"Clients and Markets"** (adds client
   references / market proof for general questions).
3. One summarized answer from both. The answer must **say which track the
   content came from** so users understand the source.

Infra: paid Azure OpenAI (GPT-5.5) + Cosmos **serverless** account in office.
(Note: serverless containers have no throughput setting — the earlier
provisioned/shared-RU instructions in migration_strategy.md don't apply there;
just create the five containers.)

## Key design decision — filter by JOIN, not by re-tagging entities

Entities are NOT re-extracted or re-tagged. A per-document **registry**
(doc → metadata) is the single source of truth for scoping:

- **Chunks**: stamped with the metadata arrays → server-side
  `ARRAY_CONTAINS(c.service_functions, @sf)` filters (a no-LLM patch of
  existing chunk documents — NOT a re-extraction).
- **Entities**: filtered at retrieval time by joining `source_docs` against the
  scoped doc set from the registry (in Python, after the candidate query).
- **Title matching / communities**: same join — scoped doc set intersected
  with doc titles and community `source_docs`.

Result: the paid-for v2 extraction is fully preserved; metadata changes later
(re-tagging in Templafy) need only a cheap re-sync, never LLM calls.

## Phases

### M0 — Metadata discovery — DONE 2026-07-21
`scripts/inspect_blob_metadata.py` (run on the office machine against the real
container: `python -m scripts.inspect_blob_metadata --json m0_report.json`):
- Lists all blobs with metadata; reports: coverage per key (tagged vs untagged),
  distinct `Services_Function_Capabilities` values + doc counts, multi-tag
  distribution, `IsDeleted` count, sample rows.
- **Detects how lists are serialized** in blob metadata (comma / semicolon /
  JSON — blob metadata values are plain strings; the parser depends on this).
- Output: console report + JSON dump. Everything downstream trusts this.
- Fallback rule decision comes from the numbers: docs with NO SF tag are either
  excluded from scoped search or included in every scope — decide when we see
  the coverage percentage.

### M1 — Metadata plumbing + scoped retrieval (~1.5–2 days)
1. **`AzureBlobSource`**: capture blob metadata during listing (one API call,
   `include metadata`); expose a doc→metadata map. Parse list fields per M0's
   detected format.
2. **Doc registry**: one item per document in the `communities` container
   (`kind='document'`, partition `graph_id`): filename, doc_id, SF list,
   industry list, platform, keywords, title, short_description, webUrl,
   modified time. Written during Data Prep; `scripts/sync_metadata.py` creates/
   refreshes it for already-extracted docs **without re-extraction** and
   patches existing chunk items with the tag arrays.
3. **Retrieval scoping**: `retrieve(..., service_function=...)` —
   chunks via `ARRAY_CONTAINS`, entities/titles/communities via registry join.
   "All" = no filter (today's behaviour).
4. **UI**: service-function dropdown on Query + Batch tabs, populated from
   `/api/query/service-functions` (distinct values + doc counts from registry).
5. **Better citations**: `webUrl` from the registry replaces blob-SAS links in
   chunk details and matched documents (fallback to SAS when absent) — answers
   link to the governed Templafy/SharePoint source.
6. **Hygiene**: `IsDeleted=true` docs are skipped at ingestion and evicted by
   the sync script (reuses `remove_doc` purge machinery). `ModifiedTime` drives
   stale-extraction detection alongside blob ETags.
7. `Title`/`Short_Description` feed title matching (curated titles beat
   filename parsing); `Keywords` added to entity-candidate matching text.

### M2 — Dual-track answering (~1 day, the team's process, graph-powered)
- `ask(question, service_function=SF)`:
  1. Planner call (one rewrite, shared by both tracks).
  2. **Track A**: scoped retrieval for SF. **Track B**: scoped retrieval for
     "Clients and Markets". (If SF *is* Clients and Markets, single track.)
  3. **One synthesis call** — contexts labeled `[ORACLE CONTENT]` /
     `[CLIENTS & MARKETS]` with the rule: technical approach from the SF
     track; credentials, client references, market proof from the C&M track.
     **Every part of the answer says which track it came from** (user
     requirement #5) — inline track citations plus a summary note when the
     answer draws on one track only ("found in Clients and Markets content").
- Response payload: per-track chunk/entity counts → UI shows
  "Oracle content (3 chunks) · Clients & Markets (2 chunks)" pills; the
  drawer groups chunks by track.
- **Batch tab inherits automatically** (same `ask()`); CSV gains a
  `Content Tracks` column.
- LLM cost per question stays **2 calls** (planner + synthesis) — the second
  track is retrieval-only, no extra LLM.

### M3 — Later (not in scope now)
Per-SF graph partitioning (`graph_id` = service function): clean per-SF
communities, scoped rebuilds/re-summaries (fixes the full-re-summarise cost at
2k docs). The dual-track then queries two logical graphs instead of two
filters. Requires the multi-tag decision (a doc tagged Oracle+Workday joins
both graphs).

## Findings from sample metadata (2026-07-21, two real blobs)

- **Lists are JSON arrays** (`["Advisory", "Consulting", …]`) → `json.loads`
  parsing. M0's serialization question is answered.
- **Multi-tagging is the norm**: one doc had 4+ SF values; another had
  `["Business Process Group", "Clients and Markets"]`. Dual-track consequence:
  a doc tagged with BOTH the selected SF and C&M is retrievable by both tracks
  → **dedup in the merge** so one deck is never presented as two independent
  corroborating sources.
- **⚠ Check the "Oracle" mapping**: observed SF values are org service lines
  (Advisory, Consulting, Technology, Business Process Group, Clients and
  Markets) — "Oracle" is NOT among them and may live in the `Platform` list
  instead. ASK THE TEAM which field their current tool filters on when the
  user selects "Oracle"; the UI dropdown + scoping filter key on that field
  (possibly Platform, possibly SF+Platform combined).
- Edge cases for the parser: `[""]` (empty string inside array — drop
  empties); `Title` has typos/glued extensions ("…companypptx") while `Name`
  is the clean filename → title matching scores against BOTH Title and Name;
  `Keywords` carry acronyms (CISSP, CISA, CPA) → feed entity-candidate text;
  soft-deleted blobs exist (portal "Undelete" seen) → IsDeleted path is real.

## Decisions (2026-07-21, user-confirmed)

- **"Oracle" lives in `Platform`**; for safety the scope filter matches
  **BOTH** `Platform` and `Services_Function_Capabilities`. Implementation:
  stamp one combined **`scope_tags`** array per chunk/registry record =
  lowercased union of both lists → single case-insensitive
  `ARRAY_CONTAINS(c.scope_tags, @value)` covers both fields; the C&M track is
  `scope_tags` contains "clients and markets" via the same mechanism.
- **UI scope selector = dropdown + free text.** Dropdown populated from the
  distinct values of both fields (with doc counts); free-text input as the
  escape hatch — trimmed, lowercased, ~50-char server-side cap, parameterized
  queries only. Before asking, the UI shows **"N documents in scope"** for the
  typed value so a typo ("oralce" → 0 docs) is caught before spending the
  2 LLM calls.

## Open items carried forward
- M0 still needed for: full distinct-value enumeration of
  `Services_Function_Capabilities` AND `Platform`, tag coverage %, IsDeleted
  count. (Serialization format is now known.)
- Untagged-doc fallback rule (decide on M0 numbers).
- Benchmark protocol vs the old tech: run the same question set (Batch tab
  CSV) through both and compare side by side — the Batch tab is the harness.
