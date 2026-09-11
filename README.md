# Glassbox — ObservableAgent

End-to-end OpenTelemetry observability for a LangGraph research agent on GCP.

An agent is a program whose control flow is decided at runtime by a
probabilistic model. You cannot read the code and know what it did. The only way
to reason about one is to record what it actually did, in causal order, with
enough attributes attached to answer questions you have not thought of yet.

This repo is a small research agent over public banking and financial-regulation
sources, instrumented so that every run is a trace you can open and read.

---

## See it without installing anything

Four public traces. No sign-in.

| Run | Latency | Cost | Tokens | What it shows |
|---|---|---|---|---|
| [Healthy](https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/c2ed80c7809d3b6cf718e4222b54f7e4) | 42s | $0.0139 | 13,468 | 8 turns, 3 sources, clean cited answer |
| [Budget exhausted, answer delivered](https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/f0096f66cf0a162c67cd8f80bfbc0dae) | 1m 06s | $0.0351 | 29,891 | 12 turns, **12 searches** to open 4 pages |
| [Budget exhausted, nothing returned](https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/eb24a20458d6735c9aca74985e4b6029) | 1m 14s | $0.0252 | 16,985 | All the research happened, none of it reached the user |
| [Answers despite blocked sources](https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/c1fc5a23f3110b385d4cd4efaec55436) | 1m 17s | $0.0312 | 22,968 | Two sites return 403, run completes anyway |

Runs 2 and 3 are the pair worth reading together. Both hit the turn limit. One
produces a cited answer marked low-confidence; the other produces nothing at
all. The difference is a fallback added after seeing run 3 in a trace.

Neither logs an error a monitoring dashboard would catch.

Full notes in [docs/demo-traces.md](docs/demo-traces.md).

---

## What the agent does

Ask it a banking question. It searches, opens pages, pulls out facts, and writes
a cited answer — looping until the facts corroborate across three sources or the
turn budget runs out.

Four tools:

| Tool | Does | Model call? |
|---|---|---|
| `web_search` | Query to URLs, via Tavily | No |
| `fetch_page` | URL to visible text, via httpx and BeautifulSoup | No |
| `extract_claims` | Page text to structured facts | Yes |
| `synthesize` | Facts to cited answer | Yes |

A planner node chooses the next action each turn. It is itself an LLM call, so
the route through the graph is a runtime fact rather than something visible in
the code — which is the whole reason this needs tracing.

---

## Stack

| Layer | Choice |
|---|---|
| Agent | LangGraph-shaped loop, Gemini 2.5 Flash on Vertex AI |
| Instrumentation | OpenTelemetry, GenAI semantic conventions (v1.37+) |
| Transport | OTLP over HTTP |
| Backend | Langfuse |
| Auth | Application Default Credentials, no service account keys |

The GenAI conventions are still marked Development, so instrumentation sets
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` to emit the newer
attribute names rather than the pre-1.36 ones.

Input and output are written under both `input.value` (vendor-neutral) and
`langfuse.observation.input` (what Langfuse reads first), so the instrumentation
is not tied to one backend.

---

## Span design

Designed before any instrumentation code was written, in
[docs/span-design.md](docs/span-design.md), then checked against real traces.

```
invoke_workflow glassbox-research      (root, one per run)
  invoke_agent researcher
    plan researcher            (iteration 1)
      chat gemini-2.5-flash
    execute_tool web_search
    plan researcher            (iteration 2)
      chat gemini-2.5-flash
    execute_tool fetch_page
    ...
```

Two decisions that matter:

**Loop iterations are siblings, not nested.** Six nested iterations produce a
staircase that runs off the edge of any trace viewer and makes
iteration-to-iteration comparison impossible.

**`chat` spans nest inside their `execute_tool` span.** Tool duration includes
prompt assembly, parsing and retries; chat duration is model time alone. Keeping
them separate is what lets you tell "the model was slow" from "our parsing was
slow."

Attributes follow `gen_ai.*` where the spec defines one. Everything else lives
under `glassbox.*` — no invented `gen_ai.` names.

---

## What the traces revealed

None of this was predictable from reading the code.

**The planner is most of the bill.** Across 14 runs: 198 `chat` spans, 142 of
them from the planner. Roughly 72% of model calls are the agent deciding what to
do, not doing it. Every optimisation instinct says to shrink the extraction
prompt. The traces say to shrink the planning loop.

**The stopping rule measured the wrong thing.** Run 2 above made twelve searches
to open four pages, three of them consecutive with the same stated reason: it
needed a third source. It already had nineteen facts. The rule counted distinct
*sources* rather than whether the question was answered. The answer was fine;
the route was wasteful, and only the trace shows it.

**Returning nothing was worse than answering with a caveat.** Three of five
questions originally ended with facts gathered and no output — run 3 above is
one of them. Forced synthesis on the final turn (answer anyway, mark it low
confidence) turned those into usable results. The `plan` span records
`glassbox.forced_synthesis` so these runs stay identifiable.

**Roughly a third of fetches are refused.** Reuters, Wolters Kluwer, Atlantic
Council and others return 403 to anything that is not a browser. An agent
researching the open web is at the mercy of who lets it in, and the failed-fetch
count per run is a real quality signal.

**Run-to-run variance is large.** Two runs of the same question, minutes apart,
differ by 6,000 tokens and produce different outcomes. Any evaluation of an
agent over the live web needs repeated runs, not one.

---

## Deliberate design choices

**No duplicate-search detection.** A production agent should notice it is
re-running a search it already ran. This one does not, because that repetition
is the behaviour the project exists to make visible. Removing it would delete
the demo.

**Failure injection is flagged.** `CHAOS_MODE` injects a fetch timeout,
unparseable model output, or an unsatisfiable stopping condition. Every injected
failure sets `glassbox.chaos_injected` on its span and the run records
`glassbox.chaos_mode`, so a demo trace can never be mistaken for a real
incident.

**Public sources only.** Every source is public web content. No internal,
proprietary or employer data appears anywhere in this project.

---

## Running it

```bash
python -m venv .venv
source .venv/bin/activate          # .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
```

Then a `.env` with:

```
GOOGLE_CLOUD_PROJECT=your-project
GOOGLE_CLOUD_LOCATION=us-central1
TAVILY_API_KEY=
LANGFUSE_AUTH=                     # base64 of "pk-lf-...:sk-lf-..."
LANGFUSE_HOST=https://us.cloud.langfuse.com
OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
OTEL_SERVICE_NAME=glassbox-research
```

```bash
gcloud auth application-default login
gcloud services enable aiplatform.googleapis.com

cd app
python run_agent.py 1              # five preset questions, 1 to 5
CHAOS_MODE=timeout python run_agent.py 1
```

Set a project-scoped billing budget before the first run. A twelve-turn run
makes around fifteen model calls.

---

## Deployment notes

Public Cloud Run services grant `roles/run.invoker` to `allUsers`. Under a
Google Cloud organization with domain-restricted sharing enforced — which is on
by default for organizations created since the secure-by-default rollout — that
binding is rejected, because `allUsers` is not covered by the allowed-domains
list.

The workarounds are to disable the policy, add the binding, and re-enable it,
or to use a conditional organization policy keyed on a resource tag. This
project sits outside any organization instead, which is the right call for a
throwaway demo and the wrong call for anything real.

---

## What I would do differently

- Measure sufficiency, not source count. The corroboration rule is the single
  biggest source of wasted turns.
- Cache fetched pages. Repeated runs re-download the same URLs.
- Shrink the planner prompt before touching anything else. It is 72% of model
  calls.
- Retry 403s with a different User-Agent, and record the retry as its own span
  rather than hiding it inside the fetch.
- Run each question five times before drawing conclusions. Single runs over the
  live web are not evidence.