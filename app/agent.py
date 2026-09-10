"""
Glassbox research agent — planner and loop, instrumented.

Span structure produced by one run:

    invoke_workflow glassbox-research      (root, one per run)
      invoke_agent researcher
        plan researcher            (iteration 1)
          chat gemini-2.5-flash
        execute_tool web_search
        plan researcher            (iteration 2)
          chat gemini-2.5-flash
        execute_tool fetch_page
        ...

`plan` and `execute_tool` spans are siblings under `invoke_agent`. Loop
iterations do not nest inside each other.

Two behaviours added after observing real runs:

  Forced synthesis. Early versions returned nothing when the turn budget ran
  out, even with a dozen facts gathered. An agent that does the research and
  then refuses to speak is worse than one that answers with a caveat. On the
  last turn, if any claims exist, it answers and marks the result
  low-confidence.

  Transient error tolerance. A single dropped connection used to destroy a
  whole run. Network failures now cost one turn instead of everything.

Deliberate omission: nothing here detects that the agent is repeating a search
it already ran. A production agent should catch that. This one must not, because
that repetition is the silent-loop behaviour the project exists to make visible.
"""

import json
import uuid
from typing import TypedDict

from opentelemetry.trace import Status, StatusCode

import chaos
from telemetry import WORKFLOW_NAME, tracer
from tools import _chat, extract_claims, fetch_page, synthesize, web_search

MAX_ITERATIONS = 12
COVERAGE_THRESHOLD = chaos.coverage_threshold(0.85)
MIN_SOURCES = chaos.min_sources(3)
SEARCH_BACKLOG_LIMIT = 3

# Turns reserved at the end for producing an answer from whatever was gathered.
RESERVE_TURNS = 1


class State(TypedDict):
    question: str
    conversation_id: str
    search_results: list[dict]
    unread_urls: list[str]
    read_pages: list[dict]
    failed_fetches: list[dict]
    failed_extractions: list[dict]
    transient_errors: list[dict]
    claims: list[dict]
    sources: list[str]
    iteration: int
    answer: str
    answer_confidence: str
    stop_reason: str


def new_state(question: str) -> State:
    return {
        "question": question,
        "conversation_id": str(uuid.uuid4()),
        "search_results": [],
        "unread_urls": [],
        "read_pages": [],
        "failed_fetches": [],
        "failed_extractions": [],
        "transient_errors": [],
        "claims": [],
        "sources": [],
        "iteration": 0,
        "answer": "",
        "answer_confidence": "",
        "stop_reason": "",
    }


def plan(state: State) -> dict:
    """Decide the next action. This is the only node that chooses anything."""
    with tracer().start_as_current_span("plan researcher") as span:
        span.set_attribute("gen_ai.operation.name", "plan")
        span.set_attribute("glassbox.iteration", state["iteration"])
        span.set_attribute(
            "glassbox.step_budget_remaining", MAX_ITERATIONS - state["iteration"]
        )

        claims_text = (
            "\n".join(f"- {c['claim']}" for c in state["claims"]) or "(none yet)"
        )
        unread = len(state["unread_urls"])
        unextracted = sum(1 for p in state["read_pages"] if not p["extracted"])

        prompt = f"""You are the planner for a research agent answering a question about
banking and financial regulation.

Question: {state["question"]}

Facts gathered so far:
{claims_text}

Current situation:
  - searches run: {len(state["search_results"])}
  - pages found but not yet opened: {unread}
  - pages opened but not yet mined for facts: {unextracted}
  - pages that could not be opened: {len(state["failed_fetches"])}
  - distinct sources that have yielded facts: {len(state["sources"])} of {MIN_SOURCES} required
  - turn: {state["iteration"]} of {MAX_ITERATIONS}

Choose ONE next action:
  "web_search"     - run a new search. Supply "query" with different wording
                     than before if earlier searches were unhelpful. Do NOT
                     search again while pages are still waiting to be opened.
  "fetch_page"     - open the next unopened page. Only if pages are waiting.
  "extract_claims" - mine an opened page for facts. Only if pages are waiting.
  "synthesize"     - write the final answer. Only if the facts above actually
                     answer the question AND at least {MIN_SOURCES} distinct
                     sources have corroborated them.

Return ONLY a JSON object with these keys:
  "next_action"     - one of the four strings above
  "coverage_score"  - 0 to 1, how completely the facts answer the question
  "reasoning"       - one sentence on why
  "query"           - the search text, only when next_action is "web_search"

No markdown, no code fences."""

        raw = _chat(prompt, purpose="plan")

        try:
            decision = _parse_json_object(raw)
        except (json.JSONDecodeError, ValueError):
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", "unparseable_planner_output")
            raise

        requested = decision["next_action"]
        coverage = float(decision.get("coverage_score", 0.0))
        action = requested

        # The planner proposes; these rules dispose.
        if action == "fetch_page" and unread == 0:
            action = "extract_claims" if unextracted else "web_search"
        if action == "extract_claims" and unextracted == 0:
            action = "fetch_page" if unread else "web_search"
        if action == "web_search" and unread >= SEARCH_BACKLOG_LIMIT:
            action = "extract_claims" if unextracted else "fetch_page"
        if action == "synthesize" and coverage < COVERAGE_THRESHOLD:
            action = "web_search"
        if action == "synthesize" and not state["claims"]:
            action = "web_search"
        if action == "synthesize" and len(state["sources"]) < MIN_SOURCES:
            action = "fetch_page" if unread else "web_search"

        # Last turn: answer with what we have rather than returning nothing.
        forced = False
        turns_left = MAX_ITERATIONS - state["iteration"]
        if turns_left < RESERVE_TURNS and state["claims"] and action != "synthesize":
            action = "synthesize"
            forced = True

        span.set_attribute("glassbox.coverage_score", coverage)
        span.set_attribute("glassbox.next_action", action)
        span.set_attribute("glassbox.requested_action", requested)
        span.set_attribute("glassbox.action_overridden", action != requested)
        span.set_attribute("glassbox.forced_synthesis", forced)
        span.set_attribute("glassbox.reasoning", decision.get("reasoning", ""))
        span.set_attribute("glassbox.source_count", len(state["sources"]))
        span.set_attribute("glassbox.unread_count", unread)

        return {
            "next_action": action,
            "coverage_score": coverage,
            "reasoning": decision.get("reasoning", ""),
            "query": decision.get("query", state["question"]),
            "forced": forced,
        }


def do_search(state: State, query: str) -> None:
    results = web_search(query, max_results=3)
    state["search_results"].extend(results)
    known = (
        {p["url"] for p in state["read_pages"]}
        | {f["url"] for f in state["failed_fetches"]}
        | set(state["unread_urls"])
    )
    for r in results:
        if r["url"] not in known:
            state["unread_urls"].append(r["url"])


def do_fetch(state: State) -> None:
    """Open the next unread page. Failures are recorded, not fatal."""
    url = state["unread_urls"].pop(0)
    try:
        text = fetch_page(url)
    except Exception as exc:
        state["failed_fetches"].append({"url": url, "error": type(exc).__name__})
        print(f"           fetch failed: {url} ({type(exc).__name__})")
        return
    state["read_pages"].append({"url": url, "text": text, "extracted": False})


def do_extract(state: State) -> None:
    """Mine an opened page for facts.

    Unparseable output marks the page done, so it is not retried forever.
    A network failure leaves the page unextracted so it can be tried again.
    """
    page = next(p for p in state["read_pages"] if not p["extracted"])
    try:
        found = extract_claims(page["text"], state["question"])
    except ValueError:
        page["extracted"] = True
        state["failed_extractions"].append(
            {"url": page["url"], "error": "unparseable_model_output"}
        )
        print(f"           extraction failed: {page['url']} (unparseable output)")
        return
    except Exception as exc:
        state["transient_errors"].append(
            {"where": "extract_claims", "error": type(exc).__name__}
        )
        print(f"           transient error in extraction ({type(exc).__name__})")
        return

    page["extracted"] = True
    state["claims"].extend(found)
    if found and page["url"] not in state["sources"]:
        state["sources"].append(page["url"])


def do_synthesize(state: State, forced: bool) -> None:
    state["answer"] = synthesize(
        state["claims"], state["question"], state["sources"]
    )
    state["answer_confidence"] = "low" if forced else "normal"
    state["stop_reason"] = "answered_forced" if forced else "answered"


def run(question: str, verbose: bool = True) -> State:
    """Run one research task. Produces exactly one trace."""
    state = new_state(question)

    with tracer().start_as_current_span(
        f"invoke_workflow {WORKFLOW_NAME}"
    ) as workflow_span:
        workflow_span.set_attribute("gen_ai.operation.name", "invoke_workflow")
        workflow_span.set_attribute("gen_ai.workflow.name", WORKFLOW_NAME)
        workflow_span.set_attribute(
            "gen_ai.conversation.id", state["conversation_id"]
        )
        workflow_span.set_attribute("glassbox.chaos_mode", chaos.MODE)

        with tracer().start_as_current_span("invoke_agent researcher") as agent_span:
            agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
            agent_span.set_attribute("gen_ai.agent.name", "researcher")
            agent_span.set_attribute(
                "gen_ai.conversation.id", state["conversation_id"]
            )

            _loop(state, verbose)

            agent_span.set_attribute("glassbox.iterations", state["iteration"])
            agent_span.set_attribute("glassbox.stop_reason", state["stop_reason"])
            agent_span.set_attribute(
                "glassbox.answer_confidence", state["answer_confidence"]
            )

            if state["stop_reason"] == "turn_limit_reached":
                agent_span.set_status(Status(StatusCode.ERROR))
                agent_span.set_attribute("error.type", "step_budget_exceeded")

        workflow_span.set_attribute("glassbox.iterations", state["iteration"])
        workflow_span.set_attribute("glassbox.stop_reason", state["stop_reason"])
        workflow_span.set_attribute("glassbox.claim_count", len(state["claims"]))
        workflow_span.set_attribute("glassbox.source_count", len(state["sources"]))
        workflow_span.set_attribute(
            "glassbox.search_count", len(state["search_results"])
        )
        workflow_span.set_attribute(
            "glassbox.failed_fetch_count", len(state["failed_fetches"])
        )
        workflow_span.set_attribute(
            "glassbox.failed_extraction_count", len(state["failed_extractions"])
        )
        workflow_span.set_attribute(
            "glassbox.transient_error_count", len(state["transient_errors"])
        )
        workflow_span.set_attribute("glassbox.answered", bool(state["answer"]))
        workflow_span.set_attribute(
            "glassbox.answer_confidence", state["answer_confidence"]
        )

    return state


def _loop(state: State, verbose: bool) -> None:
    while state["iteration"] < MAX_ITERATIONS:
        state["iteration"] += 1

        try:
            decision = plan(state)
        except Exception as exc:
            state["transient_errors"].append(
                {"where": "plan", "error": type(exc).__name__}
            )
            print(f"           transient error in planner ({type(exc).__name__})")
            continue

        action = decision["next_action"]

        if verbose:
            marker = " [FORCED]" if decision.get("forced") else ""
            print(
                f"[turn {state['iteration']:2d}] {action:<16} "
                f"coverage={decision['coverage_score']:.2f}  "
                f"sources={len(state['sources'])}  "
                f"unread={len(state['unread_urls'])}  "
                f"{decision['reasoning']}{marker}"
            )

        try:
            if action == "web_search":
                do_search(state, decision["query"])
            elif action == "fetch_page":
                do_fetch(state)
            elif action == "extract_claims":
                do_extract(state)
            elif action == "synthesize":
                do_synthesize(state, decision.get("forced", False))
                return
            else:
                state["stop_reason"] = f"unknown action: {action}"
                return
        except Exception as exc:
            state["transient_errors"].append(
                {"where": action, "error": type(exc).__name__}
            )
            print(f"           transient error in {action} ({type(exc).__name__})")

    state["stop_reason"] = "turn_limit_reached"


def _parse_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    parsed = json.loads(cleaned.strip())
    if not isinstance(parsed, dict):
        raise ValueError(f"Planner returned {type(parsed).__name__}, expected object")
    return parsed