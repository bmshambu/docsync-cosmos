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
    The split logic here mirrors the RFP extractor's dedicated decompose prompt
    (build_rfp_decompose_prompt) — same standalone test, drop-admin rule, and
    "uncertain → KEEP" default — so the Query tab and the RFP path reason about
    splitting identically; only the delivery differs (inline in this single
    planner call vs. a batched call over an extracted list).

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

# NOTE: task 4 ("sub_questions") is the SAME question-splitting logic as the RFP
# extractor's dedicated decompose prompt — RFP_DECOMPOSE_USER_TEMPLATE /
# build_rfp_decompose_prompt in app/llm/prompts.py. They are intentionally kept
# in two places because they are delivered differently (here: inline in the
# single planner call over ONE typed question; there: a batched call over a
# numbered LIST of extracted questions). If you tune the split rules — standalone
# test, drop-admin, uncertain→KEEP, examples — UPDATE BOTH so the Query tab and
# the RFP path keep reasoning about splitting identically.
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
4. "sub_questions": decide whether this is ONE ask or several, and split only a
   genuinely compound question. Retrieval embeds the whole string once, so a
   strong part and a weak part dilute each other's match — split only when the
   parts would each retrieve different content.
   SPLIT only when ALL of these are true:
     - It contains two or more DISTINCT substantive asks joined by "and", ";",
       "as well as", or parallel phrasing (e.g. "Describe X and explain Y" where
       X and Y are different topics) — not one topic rephrased.
     - Every part is a COMPLETE, self-contained question answerable on its own —
       NO pronouns/pointers that depend on the other clause ("those", "the
       above", "it", "them", "such services") without naming the topic; repeat
       the subject in each part if needed.
     - Every part is substantive (methodology, approach, qualifications, service
       delivery, risk, etc.) — not administrative.
   Do NOT split (keep as one) when:
     - Only one real ask is present, even if the sentence is long or has detail.
     - A clause is an orphan/fragment that loses meaning without the other
       (e.g. "…and how your firm charges for those expenses" depends on the
       expenses topic in the first clause).
     - Both clauses ask for the same answer topic.
     - Splitting would create stubs like "Provide details" with no topic.
     - You are uncertain — DEFAULT TO KEEP (return the single rewritten question).
   If one clause is administrative (attach a form, sign, W-9, portal, yes/no),
   OMIT that clause — do not emit it as a sub-question.
   Rewrite each emitted part as a complete standalone sentence with explicit
   topic and ask. {sub_limit_note}
   Examples:
     - SPLIT: "Describe your change management strategy and your training
       approach for project managers." -> ["Describe your change management
       strategy.", "Describe your training approach for project managers."]
     - KEEP: "Explain your approach to data conversion and how converted data is
       validated before load." (one integrated data-conversion topic)
     - SPLIT + drop admin: "Describe your audit methodology and attach a signed
       W-9." -> ["Describe your audit methodology."]
   If it is a single ask, return a list with just the rewritten question from task 1.

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
