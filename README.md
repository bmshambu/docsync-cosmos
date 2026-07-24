# cosmos-rag — Cosmos DB migration workspace

Clean copy of the RFP GraphRAG app for the Cosmos DB storage migration
(see `../migration_strategy.md`). The parent folder stays untouched as the
working reference version; all migration work happens here.

## Migration progress

| Step | Status | What |
|---|---|---|
| 1. GraphStore interface | ✅ done | All entity/relationship/chunk/community/stats reads and writes go through `app/services/graph_store.py`. `FileGraphStore` preserves today's file layout — behaviour unchanged. |
| 2. CosmosGraphStore | ✅ done | Same interface against the five containers. Diff-based `save_extraction` (no O(N²) rewrites), per-doc chunk upserts, map/summaries/stats in `communities` via `kind` field, snapshot export for the D3 generator. `STORAGE_BACKEND=cosmos` is now the active backend. |
| 3. Pipeline write path | ✅ done | Extraction + summariser write per-doc/per-community through the store; chunks pushed via `save_doc_chunks` after text extraction. Local files remain as disposable scratch only. |
| 4. Retriever read path | ✅ done | Every retrieval step is a targeted query: entity candidates via CONTAINS on name/aliases/type, per-hop edge queries (ARRAY_CONTAINS), chunk candidates via CONTAINS on text (TOP-capped), summaries in one projection query, financial table as a single-partition /type query. Query cost scales with matches, not corpus size. |
| 5. Jobs container | ✅ done | Write-through job persistence (throttled to 2 s, forced on status transitions, logs capped at 500, non-JSON LangGraph state stripped) + read-through `status_dict()` fallback — jobs survive restarts and other replicas can answer polls. |
| 6. Backfill + verify | ✅ done | `scripts/backfill_to_cosmos.py` migrated the file artefacts (48 entities, 500 chunks/100 docs, map, summaries, stats); all three tab endpoints verified against Cosmos. |

**Perf note**: individual Cosmos calls take 1–5 s from a local dev machine (cross-region
round trips to East US 2 + client warmup). From the Azure-hosted web app in the same
region these drop to tens of ms. Never call `summary_ok()` in a loop — use
`summary_ok_ids()` (one query); that mistake cost 37 s on 48 communities.

## Configuration (.env)

- `STORAGE_BACKEND=file` — JSON/md files under `DATA_DIR` (local dev / rollback)
- `STORAGE_BACKEND=cosmos` — Azure Cosmos DB (the active production backend)
- `COSMOS_ENDPOINT` / `COSMOS_KEY` / `COSMOS_DATABASE` — PoC account
  `graph-rag-docsync-cosmos` (personal subscription). **Moving to the office
  account later = change only these three values.**

Cosmos containers (already provisioned, shared 1000 RU/s free tier):
`entities(/type)` · `relationships(/source_doc)` · `chunks(/doc_id)`
· `communities(/graph_id)` · `jobs(/id)`

## Backfill (moving data into a Cosmos account)

`scripts/backfill_to_cosmos.py` copies whatever is in the local `data/` folder
(entities, relationships, chunks, community map + summaries, stats) into the
Cosmos account configured in `.env`. It shows the target endpoint and asks for
confirmation before writing, and is idempotent — re-running upserts.

```powershell
cd cosmos-rag
..\.venv\Scripts\python.exe -m scripts.backfill_to_cosmos                # backfill + verify
..\.venv\Scripts\python.exe -m scripts.backfill_to_cosmos --verify-only  # counts only, no writes
..\.venv\Scripts\python.exe -m scripts.backfill_to_cosmos --yes          # skip confirm prompt
```

### Clearing Cosmos (fresh backfill / clean re-extraction)

`scripts/clear_cosmos.py` PERMANENTLY deletes items from the graph containers in
whatever account `.env` points at. Blob documents are untouched — everything in
Cosmos is derived and rebuildable. Shows counts + target endpoint and requires
typing `delete` before touching anything (`--yes` skips for scripted use).

```powershell
..\.venv\Scripts\python.exe -m scripts.clear_cosmos --counts-only        # read-only
..\.venv\Scripts\python.exe -m scripts.clear_cosmos                      # asks confirmation
..\.venv\Scripts\python.exe -m scripts.clear_cosmos --yes                # no prompt
..\.venv\Scripts\python.exe -m scripts.clear_cosmos --containers chunks,jobs
```

Typical fresh start: `clear_cosmos` → run Data Prep (extracts straight into
Cosmos) → Community Summariser. Or `clear_cosmos` → `backfill_to_cosmos` if the
data already exists as local files.

### Removing a single document (no re-extraction)

`scripts/remove_doc.py` evicts document(s) from the graph — entities (shared
entities keep their other sources), relationships, chunks, local scratch — then
rebuilds the graph. Use it when something was ingested that doesn't belong
(e.g. a **client RFP** uploaded into the responses folder: its text matches the
very questions users ask, so it dominates retrieval). Shows an impact preview
and asks for confirmation.

```powershell
..\.venv\Scripts\python.exe -m scripts.remove_doc "clientname_oracle_rfp.docx"
```

Afterwards: delete the file from blob too (or the next full Data Prep run
re-ingests it), and re-run the Community Summariser (Select all — clustering
changed). The scan UI also warns when file names containing "RFP" are about to
be ingested.

**Office-account move checklist:**
1. Create the same five containers in the office Cosmos account
   (shared DB throughput; partition keys as listed above)
2. Point `COSMOS_ENDPOINT` / `COSMOS_KEY` / `COSMOS_DATABASE` in `.env` at it
3. If you have newly extracted file data: run the backfill
4. `--verify-only` to confirm counts, then start the app — done

## Run locally

```powershell
cd cosmos-rag
..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8002 --reload
```

(Reuses the parent folder's venv; run from inside `cosmos-rag/` so `.env` and
`data/` resolve to this folder.)

## Batch Q&A (Tab 4) — CSV in, answers out

Users receive RFP questions as a list. **The client RFP is never ingested into
the answer corpus** — it is the question source, and because user questions come
*from* it, its text out-matches every proposal document and answers degrade into
requirement restatements. (Use `scripts/remove_doc.py` if one was ingested by
mistake; the scan UI warns on filenames containing "RFP".)

Flow: upload questions CSV → every question runs the normal
planner → retrieve → synthesise pipeline → download the same CSV with answers
appended.

- **Input**: one question per row. The column named `Question` is used, else the
  first column. Delimiter/BOM sniffed. Limits: 2 MB, 500 questions.
- **Output**: your original columns, plus `Answer`, `Status`, `Sources`,
  `Matching Documents`, `Content Tracks`, `Query Type`, `Interpreted As`.
- **Platform scope**: pick a Platform on the Batch tab to run the whole batch
  dual-track (selected Platform + Clients and Markets); the `Content Tracks`
  column shows the per-answer split. This is the **benchmark harness** — same
  CSV through this and the old tech, compare side by side.
- **Cost**: 2 LLM calls per question (planner + synthesis), shown before you run.
- **Retrieval settings**: reuses the Query tab's ⚙ session settings.
- **Stop & Save**: cancels mid-run; answers so far are kept and downloadable.
- **Transient LLM failures** (503/429/timeouts) retry 3× with exponential
  backoff, backing off outside the semaphore so other questions keep moving.
  Permanent errors (bad API key) fail fast without wasting retries.

### Status column — the triage output

| Status | Meaning |
|---|---|
| `Answered (matching document found)` | A response-library doc whose **title matches the question** was the primary source — the strongest case |
| `Answered (synthesised from corpus)` | Assembled from graph + chunks across documents |
| `GAP — retrieved content does not answer this` | Evidence was retrieved but the model says it doesn't answer the question |
| `NO CONTENT — needs new source material` | Nothing relevant in the library at all |
| `ERROR — …` | Failed after retries; re-run just those rows |

`GAP` + `NO CONTENT` rows are **the content team's to-write list** — that is a
deliverable, not a failure. Never tune the system into manufacturing
plausible-sounding answers for them.

## How a user question is handled (query pipeline reference)

Every question costs exactly **2 LLM calls** (planner + synthesis); everything
between them is deterministic and scales with matches, not corpus size.

### Metadata scoping + dual-track (M1 / M2)

Documents carry blob metadata (`Platform`, `Services_Function_Capabilities`, …)
synced into a per-doc **registry** (`scripts/sync_metadata.py`; new extractions
build it automatically). This drives two things:

- **Scope** — when the user picks a **Platform** (dropdown of Platform values +
  a "N documents in scope" preview), retrieval is restricted to that Platform's
  docs. Scope is a *doc_id set* joined into every signal below (entities via
  `source_docs`, chunks via `filter_doc_ids`, title matches, communities,
  financial table). No Platform selected → whole corpus (original behaviour).
- **Dual-track** — a selected Platform triggers **two** scoped retrievals in
  parallel: **Track A** = the selected Platform (e.g. Oracle, technical
  approach) and **Track B** = fixed `Services_Function = "Clients and Markets"`
  (client references, credentials, market proof). The two contexts are merged
  with per-chunk track labels, cross-track overlaps deduped as
  `Oracle + Clients and Markets`, and a **single** synthesis call attributes
  each part of the answer to its track. Still 2 LLM calls — the second track is
  retrieval-only. Citations prefer the governed `webUrl` (Templafy/SharePoint)
  over blob links.

The numbered steps below run **once** unscoped, or **once per track** when a
Platform is selected (then merged before the single synthesis).

1. **Planner** (LLM call 1) — rewrites the question (typo/grammar fixes, intent
   preserved) and, when the UI mode is Auto, picks `query_type`
   (local/global/hybrid) and `hops` (1, or 2 for chained relations). Any
   failure falls back silently to the raw question + heuristic classification.
   Runs once and is **shared by both tracks**.
2. **Keywords** — stop-words and ≤2-char tokens dropped; these drive every step below.
3. **Entity matching** — backend narrows to candidates (`CONTAINS` on
   name/aliases/type, ≤10 keywords), Python ranks them: exact name word +3,
   substring +2, type +1, attributes +1 → top `TOP_ENTITIES`.
4. **Classification** — heuristics, only if the planner didn't decide.
5. **Traversal** (local/hybrid) — seeds = top 5 matched; per-hop
   `get_relationships_for(frontier)` (ARRAY_CONTAINS on Cosmos); result
   relevance-ranked against the keywords.
6. **Communities** (global/hybrid) — map + ALL summaries in ONE call, scored by
   keyword frequency → top `TOP_COMMUNITIES` with full text.
7. **Title matching (filename-as-question)** — response-library documents are
   typically *named as the question they answer*
   ("Describe your firm's partnership with Oracle.pptx"). Every doc filename
   (cached `DISTINCT` list) is scored against the question by content-word
   overlap — question-verbs ("describe", "explain", "advisory"…) stripped from
   both sides, ≥2 overlapping words required, score normalised by title length,
   threshold `TITLE_MATCH_THRESHOLD` → top `TITLE_MATCH_DOCS` matched documents.
   Their best 2 chunks each (by keyword frequency, else the opening chunk) are
   **guaranteed into the context** — a title-matched deck whose body uses
   different wording than the question would never survive `CONTAINS`
   candidates alone. No title match → identical behaviour to before.
8. **Chunks** — `CONTAINS` candidates (≤ `CHUNK_CANDIDATE_LIMIT`), restricted to
   matched-entity docs when local; ranked by keyword frequency. **Merge**: title-
   matched chunks lead, keyword results fill the remaining slots (≥1 slot always
   reserved for keyword results when any exist) → top `TOP_CHUNKS` total.
9. **Financial table** (money questions) — ALL `financial_instrument` entities,
   deliberately uncapped: numeric filters ("over 2M") need the complete set.
   Single-partition query on Cosmos (/type is the partition key).
10. **Prompt assembly** (LLM call 2) — what actually reaches the model:

| Retrieved | Into the prompt | .env knob |
|---|---|---|
| `TOP_ENTITIES` (10) matched entities | first `MAX_PROMPT_ENTITIES` (8), with attributes | `MAX_PROMPT_ENTITIES` |
| all traversed edges, ranked | first `MAX_PROMPT_RELATIONSHIPS` (15), deduped | `MAX_PROMPT_RELATIONSHIPS` |
| `TOP_COMMUNITIES` (3) communities | 3 × 800-char summary excerpt | `TOP_COMMUNITIES` |
| `TOP_CHUNKS` (4) chunks, flagged `[TITLE MATCH]` and, when dual-track, `[ORACLE]` / `[CLIENTS AND MARKETS]` / `[ORACLE + CLIENTS AND MARKETS]` | 4 × 500-char excerpt (per track when scoped); flagged chunks are the LLM's PRIMARY source | `TOP_CHUNKS` |
| title-matched documents | listed in the answer's "📄 Matching documents" strip with governed doc links | `TITLE_MATCH_DOCS`, `TITLE_MATCH_THRESHOLD` |
| financial table | ALL rows (scope-filtered when a Platform is selected) | (uncapped by design) |

**Answer-tone rule** (fixed alongside title matching): the LLM may open with
"The library has no content on [topic]…" **only when there are no chunks AND no
entities at all**. With partial evidence it answers from what exists and notes
any gap in one sentence at the END — never a negative opener above real evidence.

**Dual-track attribution rule** (M2): technical approach / methodology /
configuration is drawn from the **platform track** (`[ORACLE]`); client
references, credentials, and market proof from the **`[CLIENTS AND MARKETS]`**
track; the answer states which track each part came from, and a chunk in both
tracks is cited once. If only one track has content, the answer notes which.

Two kinds of limits, different philosophies: the **precision caps** exist
because answer quality *degrades* with irrelevant context (they are ranked
cutoffs — what's dropped is least relevant); the financial table's **absence
of a cap** is the opposite call — for aggregation questions a sample produces
a *wrong* answer, so completeness beats economy.

**Session-tunable in the UI**: the Query tab's "⚙ Retrieval settings" panel lets
users adjust chunks (1–8), communities (1–5), prompt entities (4–15), and prompt
relationships (5–30) for their browser session (sessionStorage — dies with the
tab). `.env` stays the default; a "custom" badge shows when overridden; Reset
restores defaults. The server clamps every value to the same ranges regardless
of what the client sends — the UI ranges are UX, the clamp is the guardrail.

## Roadmap / deferred

- **Benchmark M1/M2 vs the old tech** — run the same question set through the
  Batch tab (Platform-scoped, dual-track) and compare side by side.
- **M3 — per-scope community summaries** (deferred): today the graph is single
  and communities are mixed-theme (correctly scope-*filtered*, but their summary
  text describes the mixed cluster). M3 would partition the graph for clean
  per-scope summaries + cheap scoped re-summaries. Held off until the benchmark
  shows the community layer actually hurts scoped answers — see
  `../service_function_plan.md`. A lighter fix (skip communities under a scope,
  or summarise in-scope entities at query time) is likely enough.
- **Vector search** — DiskANN on the chunks container as a fifth retrieval
  signal (quality upgrade, not a scaling prerequisite).
- ~~Community map is one Cosmos doc (2 MB cap)~~ — **fixed 2026-07-22**: the map
  is now sharded across `map_shard` items (hit at 22.6k entities / 1,378 docs).
- **Extractor retry** — port the Batch tab's transient-error backoff into
  `extract_one` before a 2,000-call extraction run.
