"""Query planner pre-pass.

One cheap LLM call before retrieval that:
  - fixes typos / grammar and makes the question self-contained
    ("which rfps has indicative budgt over 2 milion?" → clean phrasing)
  - decides query_type (local / global / hybrid) better than keyword heuristics
  - decides hops (1 for direct facts, 2 for chained relations)

MUST never break the query flow — any failure falls back to the original
question with heuristic classification.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import Settings
from app.llm.client import get_chat
from app.llm.extractor import _parse_json_object

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

Return ONLY: {{"question": "...", "query_type": "local|global|hybrid", "hops": 1}}"""


async def plan_query(question: str, settings: Settings) -> dict:
    """Return {"question", "query_type", "hops", "planned": bool}.

    On any failure returns the original question with planned=False so the
    caller falls back to heuristic classification.
    """
    fallback = {"question": question, "query_type": "auto", "hops": 1, "planned": False}
    try:
        chat = get_chat(settings.model_query, temperature=0.0,
                        max_tokens=2048, json_mode=True)
        resp = await chat.ainvoke([
            SystemMessage(content=PLANNER_SYSTEM),
            HumanMessage(content=PLANNER_USER_TEMPLATE.format(question=question)),
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

        return {"question": rewritten, "query_type": qtype, "hops": hops, "planned": True}
    except Exception:
        return fallback
