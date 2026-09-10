"""
Run the Glassbox agent against one of the five preset questions.

Usage:
    python run_agent.py           question 1, normal
    python run_agent.py 4         question 4, normal

To inject a failure, set CHAOS_MODE first:
    $env:CHAOS_MODE="timeout";  python run_agent.py 1
    $env:CHAOS_MODE="bad_json"; python run_agent.py 1
    $env:CHAOS_MODE="loop";     python run_agent.py 1
    $env:CHAOS_MODE="none"      to clear it again
"""

import sys

import chaos
from agent import MAX_ITERATIONS, run

QUESTIONS = {
    1: "What is the FDIC deposit insurance limit per depositor per bank?",
    2: "What does the GENIUS Act allow banks to do with payment stablecoins?",
    3: "What changes under the FDIC bank supervision rule effective November 2026?",
    4: "Is Basel III fully implemented in the US?",
    5: "How are regulators treating AI third-party risk in banking?",
}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    question = QUESTIONS[n]

    print(f"\nQuestion {n}: {question}")
    if chaos.active():
        print(f"Chaos mode:  {chaos.MODE} — {chaos.describe()}")
    print()
    print("-" * 70)

    state = run(question)

    print("-" * 70)
    print(f"\nStopped because:     {state['stop_reason']}")
    print(f"Turns used:          {state['iteration']} of {MAX_ITERATIONS}")
    print(f"Searches run:        {len(state['search_results'])}")
    print(f"Pages opened:        {len(state['read_pages'])}")
    print(f"Failed fetches:      {len(state['failed_fetches'])}")
    print(f"Failed extractions:  {len(state['failed_extractions'])}")
    print(f"Transient errors:    {len(state['transient_errors'])}")
    print(f"Facts gathered:      {len(state['claims'])}")

    if state["answer"]:
        if state["answer_confidence"] == "low":
            print("\nAnswer (LOW CONFIDENCE — turn budget ran out mid-research):")
        else:
            print("\nAnswer:")
        print(state["answer"])
    else:
        print("\nNo answer produced.")


if __name__ == "__main__":
    main()