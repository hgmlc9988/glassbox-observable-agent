"""
Glassbox research agent — tools, instrumented.

Each tool now opens an `execute_tool` span. The two tools that call Gemini open
a nested `chat` span inside it, so tool time and model time stay separable.

Design notes unchanged from before:
  - No retry logic. Phase 5 injects failures on purpose.
  - Timeouts explicit, so the timeout demo is reproducible.
  - MAX_PAGE_CHARS is the main cost lever.
"""

import json
import os

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from opentelemetry.trace import SpanKind, Status, StatusCode
from tavily import TavilyClient

from telemetry import record_usage, tracer

load_dotenv()

MODEL = "gemini-2.5-flash"
MAX_PAGE_CHARS = 12000
FETCH_TIMEOUT_SECONDS = 10.0

_llm = ChatGoogleGenerativeAI(
    model=MODEL,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ["GOOGLE_CLOUD_LOCATION"],
)

_tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])


def _chat(prompt: str, purpose: str) -> str:
    """Call Gemini inside a `chat` span.

    Every model call in the project goes through here, so token counts and cost
    are recorded in exactly one place.
    """
    with tracer().start_as_current_span(
        f"chat {MODEL}", kind=SpanKind.CLIENT
    ) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "gcp.vertex_ai")
        span.set_attribute("gen_ai.request.model", MODEL)
        span.set_attribute("glassbox.purpose", purpose)

        response = _llm.invoke(prompt)
        record_usage(span, response)

        return response.content


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search public sources. Returns url, title and a short snippet each."""
    with tracer().start_as_current_span("execute_tool web_search") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "web_search")
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("glassbox.query", query)

        response = _tavily.search(query, max_results=max_results)
        results = [
            {
                "url": r["url"],
                "title": r.get("title", ""),
                "snippet": r.get("content", "")[:300],
            }
            for r in response["results"]
        ]

        span.set_attribute("glassbox.result_count", len(results))
        return results


def fetch_page(url: str) -> str:
    """Download a page and return its visible text, truncated."""
    with tracer().start_as_current_span("execute_tool fetch_page") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "fetch_page")
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("url.full", url)

        try:
            response = httpx.get(
                url,
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": "glassbox-research/0.1"},
            )
            response.raise_for_status()
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise

        span.set_attribute("http.response.status_code", response.status_code)

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = " ".join(soup.get_text(separator=" ").split())
        truncated = text[:MAX_PAGE_CHARS]

        span.set_attribute("glassbox.page_chars_raw", len(text))
        span.set_attribute("glassbox.page_chars_kept", len(truncated))
        span.set_attribute("glassbox.truncated", len(text) > MAX_PAGE_CHARS)

        return truncated


def extract_claims(page_text: str, question: str) -> list[dict]:
    """Pull factual claims relevant to the question out of page text."""
    with tracer().start_as_current_span("execute_tool extract_claims") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "extract_claims")
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("glassbox.input_chars", len(page_text))

        prompt = f"""Extract factual claims from the text below that help answer this question.

Question: {question}

Text:
{page_text}

Return ONLY a JSON array. Each element must have exactly two keys:
  "claim"     - one factual statement, in your own words
  "confidence" - a number from 0 to 1

Return an empty array if the text contains nothing relevant.
No markdown, no code fences, no commentary."""

        raw = _chat(prompt, purpose="extract_claims")

        try:
            claims = _parse_json_array(raw)
        except ValueError as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", "unparseable_model_output")
            raise

        span.set_attribute("glassbox.claim_count", len(claims))
        return claims


def synthesize(claims: list[dict], question: str, sources: list[str]) -> str:
    """Write a final cited answer from the accumulated claims."""
    with tracer().start_as_current_span("execute_tool synthesize") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "synthesize")
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("glassbox.claim_count", len(claims))
        span.set_attribute("glassbox.source_count", len(sources))

        claims_text = "\n".join(f"- {c['claim']}" for c in claims)
        sources_text = "\n".join(f"[{i + 1}] {u}" for i, u in enumerate(sources))

        prompt = f"""Answer the question using only the claims below.

Question: {question}

Claims:
{claims_text}

Sources:
{sources_text}

Cite sources inline as [1], [2] and so on. If the claims do not settle the
question, say so plainly rather than guessing."""

        return _chat(prompt, purpose="synthesize")


def _parse_json_array(raw: str) -> list[dict]:
    """Strip code fences the model may add, then parse.

    Raises ValueError on unparseable output. That failure is intentional and
    becomes one of the Phase 5 demos, so it is not swallowed here.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned unparseable JSON: {cleaned[:200]}") from exc

    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array, got {type(parsed).__name__}")

    return parsed