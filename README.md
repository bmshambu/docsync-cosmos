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

## Feature set (client-test feedback round, 2026-07-29)

Five features shipped from live testing feedback, on top of the four tabs:

| # | Feature | What it does |
|---|---|---|
| 1 | **Shared-password login** | Optional PoC gate. Set `APP_PASSWORD` in `.env` → a correct password mints an HMAC-signed HttpOnly cookie (stdlib only, no extra dependency); a middleware guards every page/API. Empty `APP_PASSWORD` = open (local dev). A "Sign out" pill appears when enabled. Not per-user identity — for real auth, front with App Service Easy Auth (Entra ID). |
| 2 | **Scope spans both metadata fields** | The scope selector is now **"Platform / Sub Service Line / Service Function"** and lists values from **both** the `Platform` field *and* the `Services_Function` field. The two fields never share a value, so a selected value matches whichever field it belongs to (`scoped_doc_ids(any_value=)`). The dropdown groups the two under labelled optgroups. |
| 3 | **RFP upload → live answers** (Tab 4) | Besides a questions CSV, Tab 4 accepts a **client RFP (PDF/DOCX)**: the model extracts every bidder question, then answers each dual-track. Answers **stream into a live-filling table** as they complete (not at the end), the **partial CSV is downloadable mid-run**, and each row records **Model, Time (s), Retrieval Settings** alongside Answer/Status/Sources/citations. |
| 4 | **Per-scope community summaries** | A **separate community graph per scope value** — every Platform / Sub Service Line value *and* every Service Function value — with **no entity re-extraction** (filter existing entities to the scope's docs → Louvain → summarise). Built all-upfront from the Community tab with live progress; each scope's artefacts live in their own `graph_id` partition. |
| 5 | **Per-scope graph visualisation** | The knowledge-graph HTML is generated **on demand, per scope**, and **degree-capped** — the whole-corpus graph (~22.6k nodes) froze D3 and rendered blank. A "Graph scope" picker on the Data Prep tab opens one scope's sub-graph, coloured by that scope's own communities (falls back to global communities when a scope hasn't been built). |

## Configuration (.env)

- `APP_PASSWORD` — set to require a shared password before using the app (feature
  #1); `APP_SESSION_HOURS` sets the cookie lifetime (default 12). Empty = open.
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

## Batch Q&A (Tab 4) — questions in, answers out

Two input modes, one answer pipeline:

- **Questions CSV** — one question per row (column named `Question`, else the
  first column; delimiter/BOM sniffed). Limits: 2 MB, 500 questions.
- **RFP document (PDF/DOCX)** — upload the client RFP and the model **extracts
  every bidder question** first (paragraphs → ~6k-char windows → parallel
  extraction → dedupe), then answers each. The extracted questions are previewed
  before you run. Limit: 25 MB. **The RFP is never ingested into the answer
  corpus** — it is only read for its questions. (It's also poison as corpus
  content: because user questions come *from* it, its text out-matches every
  proposal document. Use `scripts/remove_doc.py` if one was ingested by mistake;
  the scan UI warns on filenames containing "RFP".)

Either way: every question runs the normal planner → retrieve → synthesise
pipeline, and the answers come back as a CSV.

- **Output columns** (appended to your originals): `Answer`, `Status`,
  `Sources` (citations), `Matching Documents`, `Content Tracks`, `Query Type`,
  `Interpreted As`, `Model`, `Time (s)`, `Retrieval Settings`.
- **Live incremental results**: answers **stream into the results table as they
  complete** (pending rows greyed, a pulsing "Live" badge) — you don't wait for
  the whole batch. The **partial CSV is downloadable mid-run**
  (`answers_partial_<id>.csv`); the full file (`answers_<id>.csv`) when done.
- **Scope** (Platform / Sub Service Line / Service Function): pick a value to run
  the whole batch dual-track (selected value + Clients and Markets); the
  `Content Tracks` column shows the per-answer split. This is the **benchmark
  harness** — same questions through this and the old tech, compare side by side.
  The selector needs a synced metadata registry; when absent, a note explains how
  to enable it.
- **Cost**: 2 LLM calls per question (planner + synthesis), plus ~1 call per
  ~6k characters of the RFP for question extraction. Shown before you run.
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

- **Scope** — the selector is **"Platform / Sub Service Line / Service Function"**
  and lists values from **both** the `Platform` field and the `Services_Function`
  field (grouped in the dropdown, plus a free-text box and a "N documents in
  scope" preview). The two fields never share a value, so a chosen value matches
  whichever field it belongs to — `scoped_doc_ids(any_value=)`. Scope is a
  *doc_id set* joined into every signal below (entities via `source_docs`, chunks
  via `filter_doc_ids`, title matches, communities, financial table). Nothing
  selected → whole corpus (original behaviour).
- **Dual-track** — selecting any value (that isn't "Clients and Markets" itself)
  triggers **two** scoped retrievals in parallel: **Track A** = the selected
  value, matched against **either field** (e.g. Oracle → Platform, or Advisory →
  Service Function — technical/approach content) and **Track B** = fixed
  `Services_Function = "Clients and Markets"` (client references, credentials,
  market proof). The two contexts are merged with per-chunk track labels,
  cross-track overlaps deduped as `Oracle + Clients and Markets`, and a
  **single** synthesis call attributes each part of the answer to its track.
  Still 2 LLM calls — the second track is retrieval-only. Citations prefer the
  governed `webUrl` (Templafy/SharePoint) over blob links.

The numbered steps below run **once** unscoped, or **once per track** when a
scope value is selected (then merged before the single synthesis).

1. **Planner** (LLM call 1) — rewrites the question (typo/grammar fixes, intent
   preserved) and, when the UI mode is Auto, picks `query_type`
   (local/global/hybrid) and `hops` (1, or 2 for chained relations). Any
   failure falls back silently to the raw question + heuristic classification.
   Runs once and is **shared by both tracks**.
2. **Keywords** — stop-words and ≤2-char tokens dropped; these drive every step below.
3. **Entity matching** — backend narrows to candidates (`CONTAINS` on
   name/aliases/type, ≤10 keywords), Python ranks them: exact name word +3,
   substring +2, type +1, attributes +1 → top `TOP_ENTITIES`. These are surfaced
   in the answer's **clickable "N entities" citation** → a drawer lists which
   entities were used (name + type) with an **"Open interactive graph ↗"** button
   that renders a focused graph of exactly those entities + their 1-hop
   neighbours, cited entities highlighted (`GET /api/query/entity-graph?ids=`).
4. **Classification** — heuristics, only if the planner didn't decide.
5. **Traversal** (local/hybrid) — seeds = top 5 matched; per-hop
   `get_relationships_for(frontier)` (ARRAY_CONTAINS on Cosmos); result
   relevance-ranked against the keywords.
6. **Communities** (global/hybrid) — map + ALL summaries in ONE call, scored by
   keyword frequency → top `TOP_COMMUNITIES` with full text. When a scope value
   is active **and its per-scope community graph has been built** (feature #4),
   this reads *that* graph (`graph_id = scope_graph_id(value)`) — already
   scope-only and summarised about the scope, so no post-filter. Otherwise it
   falls back to the whole-corpus `default` graph and keeps only communities
   touching in-scope docs (so nothing breaks before scopes are built). The result
   carries `community_graph_id` to show which graph the communities came from.
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

- **Benchmark the scoped/dual-track pipeline vs the old tech** — run the same
  question set (or a client RFP) through the Batch tab (scoped, dual-track) and
  compare side by side.
- **Vector search** — DiskANN on the chunks container as a fifth retrieval
  signal (quality upgrade, not a scaling prerequisite).
- **Extractor retry** — port the Batch tab's transient-error backoff into
  `extract_one` before a 2,000-call extraction run.

**Shipped since the original roadmap:**

- ~~M3 — per-scope community summaries~~ — **done 2026-07-29** (feature #4): a
  separate community graph per Platform / Sub Service Line and per Service
  Function value, `graph_id`-partitioned, no re-extraction. Retrieval reads the
  scope's own graph when built (else falls back to the global one). Built
  all-upfront from the Community tab; see `app/services/scope_communities.py`.
- ~~Blank knowledge-graph HTML~~ — **done 2026-07-29** (feature #5): the viz is
  generated on demand per scope and degree-capped; the whole-corpus embed of
  ~22.6k nodes was what froze D3.
- ~~Community map is one Cosmos doc (2 MB cap)~~ — **fixed 2026-07-22**: the map
  is now sharded across `map_shard` items (hit at 22.6k entities / 1,378 docs).
