"""Query planner pre-pass.

One cheap LLM call before retrieval that:
  - fixes typos / grammar and makes the question self-contained
    ("which rfps has indicative budgt over 2 milion?" → clean phrasing)
  - decides query_type (local / global / hybrid) better than keyword heuristics
  - decides hops (1 for direct facts, 2 for chained relations)
  - decomposes a genuinely COMPOUND question (several separate asks in one
    message) into self-contained sub-questions — each retrieved separately
    later so no single part starves another of chunks/entities, then
    synthesised in ONE call that answers each part. Still exactly the same
    2 LLM calls total (this reuses the SAME planner call, not an extra one).

The decomposition cap is configurable via MAX_SUBQUESTIONS in .env (default 3).
Setting it to 0 removes the cap — the planner is still told to only split
genuinely separate asks (never forced padding), but nothing then truncates
what it returns. An ABSOLUTE_MAX safety ceiling always applies regardless of
the .env value, because every extra sub-question is its own retrieval pass
(×2 if a scope/dual-track is active) — LLM cost stays fixed at 2 calls, but
retrieval fan-out is not free, and a hallucinated 50-way split would still be
a real cost/latency problem even with "no cap" requested.

MUST never break the query flow — any failure falls back to the original
question with heuristic classification and a single-item sub_questions list
(i.e. decomposition silently no-ops on failure).
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.extractor import _parse_json_object

# Hard ceiling independent of MAX_SUBQUESTIONS=0 ("no cap") — protects against
# a runaway/hallucinated split regardless of what the user configures.
ABSOLUTE_MAX_SUBQUESTIONS = 10

PLANNER_SYSTEM = """You are a query planner for a GraphRAG system over RFP (Request for Proposal) documents.
The graph contains entities (clients, standards, budgets, technologies, deliverables, locations, lenders…)
and typed relationships (requires, has_budget, governed_by, acquired, located_in…).
You return STRICT JSON only — no prose, no markdown fences."""

PLANNER_USER_TEMPLATE = """Rewrite and classify this user question.

Original question: "{question}"

Tasks:
1. "question": rewrite it cleanly — fix typos and grammar, expand abbreviations,
   keep the user's intent EXACTLY. Do not add constraints that are not there.
2. "query_type": one of
   - "local"  — about one or a few specific entities/documents
   - "global" — cross-corpus: compares, aggregates, filters or lists across all RFPs
   - "hybrid" — both a specific entity focus and a cross-corpus aspect
3. "hops": 1 for direct facts about matched entities; 2 only when the question
   chains relations through intermediate entities (e.g. "lenders of companies
   that acquired targets in Indonesia").
4. "sub_questions": if the question genuinely asks SEPARATE, DISTINCT things
   (e.g. "What are Oracle's lenders AND what ESG standards do they follow?"),
   split it into self-contained sub-questions, each answerable on its own —
   repeat the subject in each part if needed so it stands alone. Do NOT split
   a question just because it is long or has multiple clauses describing ONE
   thing — only split when there are genuinely separate asks. {sub_limit_note}
   If it is a single ask, return a list containing just the rewritten question
   from task 1.

Return ONLY:
{{"question": "...", "query_type": "local|global|hybrid", "hops": 1,
  "sub_questions": ["...", "..."]}}"""


def _sub_limit_note(cap: int) -> str:
    if cap and cap > 0:
        return f"Return AT MOST {cap}."
    return "There is no fixed limit, but split only as many times as there are genuinely distinct asks."


async def plan_query(question: str, settings: Settings) -> dict:
    """Return {"question", "query_type", "hops", "sub_questions", "planned"}.

    "sub_questions" is ALWAYS a non-empty list — a single-item list (just the
    rewritten question) when the question was not decomposed, so callers never
    need to special-case "not compound". Capped at settings.max_subquestions
    (0 = no cap from the user's side), but never above ABSOLUTE_MAX_SUBQUESTIONS.

    On any failure returns the original question with planned=False so the
    caller falls back to heuristic classification.
    """
    fallback = {"question": question, "query_type": "auto", "hops": 1,
                "sub_questions": [question], "planned": False}
    configured_cap = getattr(settings, "max_subquestions", 3)
    effective_cap = (
        ABSOLUTE_MAX_SUBQUESTIONS if not configured_cap or configured_cap <= 0
        else min(configured_cap, ABSOLUTE_MAX_SUBQUESTIONS)
    )
    try:
        chat = get_chat(settings.model_query, temperature=0.0,
                        max_tokens=2048, json_mode=True)
        prompt = PLANNER_USER_TEMPLATE.format(
            question=question, sub_limit_note=_sub_limit_note(configured_cap),
        )
        resp = await chat.ainvoke([
            SystemMessage(content=PLANNER_SYSTEM),
            HumanMessage(content=prompt),
        ])
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = _parse_json_object(content)

        rewritten = str(data.get("question") or "").strip()
        qtype = str(data.get("query_type") or "").strip().lower()
        hops = data.get("hops")

        if not rewritten or len(rewritten) < 5:
            return fallback
        if qtype not in ("local", "global", "hybrid"):
            qtype = "auto"
        hops = 2 if hops == 2 else 1

        raw_subs = data.get("sub_questions")
        sub_questions = [
            s.strip() for s in raw_subs if isinstance(s, str) and s.strip()
        ] if isinstance(raw_subs, list) else []
        sub_questions = sub_questions[:effective_cap] or [rewritten]

        return {"question": rewritten, "query_type": qtype, "hops": hops,
                "sub_questions": sub_questions, "planned": True}
    except Exception:
        return fallback
