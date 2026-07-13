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

- `STORAGE_BACKEND=file` — today's JSON/md files under `DATA_DIR`
- `STORAGE_BACKEND=cosmos` — Cosmos DB (raises NotImplementedError until step 2)
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

## How retrieve() works (query pipeline reference)

Every question costs exactly **2 LLM calls** (planner + synthesis); everything
between them is deterministic and scales with matches, not corpus size.

1. **Planner** (LLM call 1) — rewrites the question (typo/grammar fixes, intent
   preserved) and, when the UI mode is Auto, picks `query_type`
   (local/global/hybrid) and `hops` (1, or 2 for chained relations). Any
   failure falls back silently to the raw question + heuristic classification.
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
7. **Chunks** — `CONTAINS` candidates (≤ `CHUNK_CANDIDATE_LIMIT`), restricted to
   matched-entity docs when local; ranked by keyword frequency → top `TOP_CHUNKS`.
8. **Financial table** (money questions) — ALL `financial_instrument` entities,
   deliberately uncapped: numeric filters ("over 2M") need the complete set.
   Single-partition query on Cosmos (/type is the partition key).
9. **Prompt assembly** (LLM call 2) — what actually reaches the model:

| Retrieved | Into the prompt | .env knob |
|---|---|---|
| `TOP_ENTITIES` (10) matched entities | first `MAX_PROMPT_ENTITIES` (8), with attributes | `MAX_PROMPT_ENTITIES` |
| all traversed edges, ranked | first `MAX_PROMPT_RELATIONSHIPS` (15), deduped | `MAX_PROMPT_RELATIONSHIPS` |
| `TOP_COMMUNITIES` (3) communities | 3 × 800-char summary excerpt | `TOP_COMMUNITIES` |
| `TOP_CHUNKS` (4) chunks | 4 × 500-char excerpt | `TOP_CHUNKS` |
| financial table | ALL rows | (uncapped by design) |

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

## Known step-2+ TODOs

- `app/services/extract.py` still writes chunk files directly (chunks *reads*
  already go through the store) — move writes into the store in step 3.
- `GraphStore.export_snapshot()` — Cosmos backend must dump a temp JSON
  snapshot for the D3 HTML generator (file backend returns live paths).
- Cosmos `save_extraction()` should diff/upsert per entity id; relationships
  need a synthetic id (hash of source|target|relation_type|source_doc).
