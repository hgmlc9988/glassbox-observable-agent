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
iterations do not nest inside each other — six nested iterations would run off
the edge of any trace viewer and make comparison between them impossible.

Deliberate omission: nothing here detects that the agent is repeating a search
it already ran. A production agent should catch that. This one must not, because
that repetition is the silent-loop behaviour the project exists to make visible.
"""

import json
import uuid
from typing import TypedDict

from opentelemetry.trace import Status, StatusCode

from telemetry import WORKFLOW_NAME, tracer
from tools import _chat, extract_claims, fetch_page, synthesize, web_search

MAX_ITERATIONS = 12
COVERAGE_THRESHOLD = 0.85
MIN_SOURCES = 3
SEARCH_BACKLOG_LIMIT = 3


class State(TypedDict):
    question: str
    conversation_id: str
    search_results: list[dict]
    unread_urls: list[str]
    read_pages: list[dict]
    failed_fetches: list[dict]
    claims: list[dict]
    sources: list[str]
    iteration: int
    answer: str
    stop_reason: str


def new_state(question: str) -> State:
    return {
        "question": question,
        "conversation_id": str(uuid.uuid4()),
        "search_results": [],
        "unread_urls": [],
        "read_pages": [],
        "failed_fetches": [],
        "claims": [],
        "sources": [],
        "iteration": 0,
        "answer": "",
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
        except (json.JSONDecodeError, ValueError) as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", "unparseable_planner_output")
            raise

        requested = decision["next_action"]
        coverage = float(decision.get("coverage_score", 0.0))
        action = requested

        # The planner proposes; these rules dispose. Without them the planner
        # can choose an action there is no material for, answer prematurely, or
        # spend every turn searching.
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

        span.set_attribute("glassbox.coverage_score", coverage)
        span.set_attribute("glassbox.next_action", action)
        span.set_attribute("glassbox.requested_action", requested)
        span.set_attribute("glassbox.action_overridden", action != requested)
        span.set_attribute("glassbox.reasoning", decision.get("reasoning", ""))
        span.set_attribute("glassbox.source_count", len(state["sources"]))
        span.set_attribute("glassbox.unread_count", unread)

        return {
            "next_action": action,
            "coverage_score": coverage,
            "reasoning": decision.get("reasoning", ""),
            "query": decision.get("query", state["question"]),
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
    """Open the next unread page.

    A 403, 404 or timeout is recorded and the run continues. The failure is not
    hidden — fetch_page already marks its span as ERROR, and the URL lands in
    failed_fetches.
    """
    url = state["unread_urls"].pop(0)
    try:
        text = fetch_page(url)
    except Exception as exc:
        state["failed_fetches"].append({"url": url, "error": type(exc).__name__})
        print(f"           fetch failed: {url} ({type(exc).__name__})")
        return
    state["read_pages"].append({"url": url, "text": text, "extracted": False})


def do_extract(state: State) -> None:
    page = next(p for p in state["read_pages"] if not p["extracted"])
    found = extract_claims(page["text"], state["question"])
    page["extracted"] = True
    state["claims"].extend(found)
    if found and page["url"] not in state["sources"]:
        state["sources"].append(page["url"])


def do_synthesize(state: State) -> None:
    state["answer"] = synthesize(
        state["claims"], state["question"], state["sources"]
    )
    state["stop_reason"] = "answered"


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

        with tracer().start_as_current_span(
            "invoke_agent researcher"
        ) as agent_span:
            agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
            agent_span.set_attribute("gen_ai.agent.name", "researcher")
            agent_span.set_attribute(
                "gen_ai.conversation.id", state["conversation_id"]
            )

            _loop(state, verbose)

            agent_span.set_attribute("glassbox.iterations", state["iteration"])
            agent_span.set_attribute("glassbox.stop_reason", state["stop_reason"])

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
        workflow_span.set_attribute("glassbox.answered", bool(state["answer"]))

    return state


def _loop(state: State, verbose: bool) -> None:
    while state["iteration"] < MAX_ITERATIONS:
        state["iteration"] += 1
        decision = plan(state)
        action = decision["next_action"]

        if verbose:
            print(
                f"[turn {state['iteration']:2d}] {action:<16} "
                f"coverage={decision['coverage_score']:.2f}  "
                f"sources={len(state['sources'])}  "
                f"unread={len(state['unread_urls'])}  "
                f"{decision['reasoning']}"
            )

        if action == "web_search":
            do_search(state, decision["query"])
        elif action == "fetch_page":
            do_fetch(state)
        elif action == "extract_claims":
            do_extract(state)
        elif action == "synthesize":
            do_synthesize(state)
            return
        else:
            state["stop_reason"] = f"unknown action: {action}"
            return

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