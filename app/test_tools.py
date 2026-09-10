"""
Exercise all four tools in sequence, without an agent loop.

This is not the agent. It is a fixed pipeline that proves each tool works and
that their inputs and outputs actually fit together. Run it before writing any
graph code.
"""

from tools import extract_claims, fetch_page, synthesize, web_search

QUESTION = "What is the FDIC deposit insurance limit per depositor per bank?"


def main() -> None:
    print("=" * 60)
    print("1. web_search")
    print("=" * 60)
    results = web_search(QUESTION, max_results=3)
    for r in results:
        print(f"  {r['url']}")
    print(f"  -> {len(results)} results\n")

    print("=" * 60)
    print("2. fetch_page")
    print("=" * 60)
    first_url = results[0]["url"]
    print(f"  fetching {first_url}")
    page = fetch_page(first_url)
    print(f"  -> {len(page)} chars")
    print(f"  -> starts: {page[:150]}...\n")

    print("=" * 60)
    print("3. extract_claims")
    print("=" * 60)
    claims = extract_claims(page, QUESTION)
    for c in claims:
        print(f"  [{c['confidence']}] {c['claim']}")
    print(f"  -> {len(claims)} claims\n")

    print("=" * 60)
    print("4. synthesize")
    print("=" * 60)
    answer = synthesize(claims, QUESTION, [first_url])
    print(answer)


if __name__ == "__main__":
    main()
