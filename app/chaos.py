"""
Glassbox — deliberate failure injection.

Real agents fail in ways that logs do not surface. This module reproduces three
of those failures on demand so each one can be captured as a trace and shown
beside a healthy run.

Controlled by the CHAOS_MODE environment variable:

    none      normal operation (default)
    timeout   the first fetch_page call times out
    bad_json  the first extract_claims call gets unparseable model output
    loop      the agent can never satisfy its own stopping condition

Failures are deterministic, not random. A demo that only breaks sometimes is
not a demo.
"""

import os

import httpx

MODE = os.environ.get("CHAOS_MODE", "none").lower()

_fired: set[str] = set()


def _fire_once(key: str) -> bool:
    """True the first time this key is asked about, False after."""
    if key in _fired:
        return False
    _fired.add(key)
    return True


def active() -> bool:
    return MODE != "none"


def maybe_break_fetch(url: str) -> None:
    """Raise a timeout on the first fetch, if timeout mode is on."""
    if MODE == "timeout" and _fire_once("fetch"):
        raise httpx.ReadTimeout(f"[chaos] injected timeout for {url}")


def maybe_break_json(raw: str) -> str:
    """Replace good model output with prose, if bad_json mode is on."""
    if MODE == "bad_json" and _fire_once("json"):
        return (
            "Certainly! Here are the key facts I found in the text. "
            "The first point concerns deposit insurance limits, and the "
            "second relates to ownership categories."
        )
    return raw


def coverage_threshold(default: float) -> float:
    """In loop mode, set a bar the agent can never clear."""
    return 0.999 if MODE == "loop" else default


def min_sources(default: int) -> int:
    """In loop mode, demand more sources than a run can realistically gather."""
    return 99 if MODE == "loop" else default


def describe() -> str:
    return {
        "none": "normal operation",
        "timeout": "first page fetch will time out",
        "bad_json": "first claim extraction will get unparseable output",
        "loop": "stopping condition is unsatisfiable, agent will loop to the limit",
    }.get(MODE, f"unknown mode: {MODE}")