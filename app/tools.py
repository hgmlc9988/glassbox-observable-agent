"""
Glassbox research agent — tools.

Four tools, deliberately uninstrumented. OpenTelemetry comes in Phase 3.

Design notes:
  - No retry logic anywhere. Phase 5 injects failures on purpose, and retries
    added now would hide exactly the behaviour we want to make visible.
  - Timeouts are explicit rather than left to library defaults, so the timeout
    demo is reproducible.
  - MAX_PAGE_CHARS is the main cost lever. It caps how much page text reaches
    extract_claims, which is the tool whose token usage scales with input.
"""

import json
import os

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from tavily import TavilyClient

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


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search public sources. Returns url, title and a short snippet each."""
    response = _tavily.search(query, max_results=max_results)
    return [
        {
            "url": r["url"],
            "title": r.get("title", ""),
            "snippet": r.get("content", "")[:300],
        }
        for r in response["results"]
    ]


def fetch_page(url: str) -> str:
    """Download a page and return its visible text, truncated."""
    response = httpx.get(
        url,
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "glassbox-research/0.1"},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = " ".join(soup.get_text(separator=" ").split())
    return text[:MAX_PAGE_CHARS]


def extract_claims(page_text: str, question: str) -> list[dict]:
    """Pull factual claims relevant to the question out of page text."""
    prompt = f"""Extract factual claims from the text below that help answer this question.

Question: {question}

Text:
{page_text}

Return ONLY a JSON array. Each element must have exactly two keys:
  "claim"     - one factual statement, in your own words
  "confidence" - a number from 0 to 1

Return an empty array if the text contains nothing relevant.
No markdown, no code fences, no commentary."""

    raw = _llm.invoke(prompt).content
    return _parse_json_array(raw)


def synthesize(claims: list[dict], question: str, sources: list[str]) -> str:
    """Write a final cited answer from the accumulated claims."""
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

    return _llm.invoke(prompt).content


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
