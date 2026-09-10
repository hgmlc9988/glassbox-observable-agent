# Span design — Glassbox / ObservableAgent

Design document for the OpenTelemetry trace structure of the Glassbox research
agent. Written before any instrumentation code, so the emitted traces can be
checked against an intended shape rather than whatever the libraries happen to
produce.

Conventions target the OpenTelemetry GenAI semantic conventions (v1.37+ format).
Because those conventions are still marked Development, instrumentation must set
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` to emit the newer
attribute names rather than the pre-1.36 ones.

---

## 1. What the agent does

A research agent over public banking and financial-regulation sources.

Graph shape: an LLM-backed planner node chooses the next action and loops until
the extracted claims sufficiently cover the question, or a step budget is
exhausted.

Tools:

| Tool | Input | Output | Notes |
|---|---|---|---|
| `web_search` | query string | list of URLs | fast, network-flaky |
| `fetch_page` | URL | page text | slow, network-bound |
| `extract_claims` | page text | structured claims | wraps an LLM call |
| `synthesize` | claims | cited answer | wraps an LLM call, terminal |

All sources are public web content. No internal, proprietary, or employer data
is used anywhere in this project.

---

## 2. Operation mapping

| Component | `gen_ai.operation.name` | Span kind | Span name |
|---|---|---|---|
| One graph run | `invoke_workflow` | internal | `invoke_workflow glassbox-research` |
| The research agent | `invoke_agent` | internal | `invoke_agent researcher` |
| Planner decision | `plan` | internal | `plan researcher` |
| Tool execution | `execute_tool` | internal | `execute_tool {tool_name}` |
| Gemini call | `chat` | client | `chat {model}` |

### Nesting rules

1. `invoke_workflow` is the root span. Exactly one per run.
2. `invoke_agent` is its only direct child.
3. `plan` and `execute_tool` spans are **siblings** under `invoke_agent`, in
   chronological order. Loop iterations do NOT nest inside each other.
4. A `chat` span nests **inside** the `execute_tool` span that triggered it.
   This applies to `extract_claims` and `synthesize`.
5. The planner is itself an LLM call, so **every** `plan` span contains exactly
   one nested `chat` span. There is no rule-based planner path.
6. A child span must start after its parent starts and end before its parent
   ends. Any drawing that violates this is wrong.

Rationale for rule 3: nesting loop iterations produces a staircase that runs off
the right edge of any trace viewer at depth 6+, and makes iteration-to-iteration
comparison impossible. Siblings keep every iteration at the same indent level so
a repeating pattern is visible at a glance.

Rationale for rule 4: the tool span and the chat span measure different things.
Tool duration includes prompt assembly, parsing, and retries; chat duration is
model time alone. Keeping both separate makes it possible to tell "the model was
slow" apart from "our parsing was slow."

Consequence of rule 5: span count grows at roughly 2x the number of planner
decisions. A run with N planner decisions and T tool calls has
`2 + 2N + T + (LLM-backed tool calls)` spans.

---

## 3. Attributes

### `invoke_workflow`

| Attribute | Value | Notes |
|---|---|---|
| `gen_ai.workflow.name` | `glassbox-research` | constant, MUST be low cardinality |
| `gen_ai.conversation.id` | UUID per run | |

Never put the user's query in `gen_ai.workflow.name`. It creates one metric time
series per unique query, which makes the metrics backend unusable and expensive.
The query text goes in a span event or a `glassbox.*` attribute instead.

### `invoke_agent`

| Attribute | Value |
|---|---|
| `gen_ai.agent.name` | `researcher` |
| `gen_ai.conversation.id` | inherited from workflow |

### `plan`

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `plan` |
| `glassbox.iteration` | int, starts at 1 |
| `glassbox.coverage_score` | float 0–1, planner's sufficiency judgement |
| `glassbox.step_budget_remaining` | int |
| `glassbox.next_action` | chosen tool name, or `synthesize` |

### `execute_tool`

| Attribute | Value |
|---|---|
| `gen_ai.tool.name` | `web_search`, `fetch_page`, `extract_claims`, `synthesize` |
| `gen_ai.tool.type` | `function` |
| `glassbox.iteration` | int, matches the `plan` span that chose it |

### `chat`

| Attribute | Value |
|---|---|
| `gen_ai.request.model` | e.g. `gemini-2.5-flash` |
| `gen_ai.response.model` | as returned |
| `gen_ai.usage.input_tokens` | int |
| `gen_ai.usage.output_tokens` | int |
| `gen_ai.response.finish_reasons` | list |
| `glassbox.cost_usd` | float, computed at span close |

### Custom namespace

All non-standard attributes use the `glassbox.` prefix. Standard `gen_ai.*`
names are never invented — if the spec does not define an attribute, it goes
under `glassbox.` instead.

---

## 4. Error recording

| Situation | How it is recorded |
|---|---|
| Tool raises | span status ERROR, `error.type` set to the exception class |
| Tool times out | span status ERROR, `error.type` = `timeout` |
| Model returns unparseable output | span status ERROR on the `execute_tool` span, not the `chat` span |
| Step budget exhausted | `invoke_agent` status ERROR, `error.type` = `step_budget_exceeded` |

A silent loop produces no errors at all. It is detected by span count and
`glassbox.iteration`, not by status. This is the point of the project.

---

## 5. Expected trace — query 1

Query: *What is the FDIC deposit insurance limit per depositor per bank?*

Straightforward, single-hop. The planner searches, reads one authoritative
source, and goes straight to synthesis without needing claim extraction.

Timeline scale below: 1 character = 0.05 seconds.

```
invoke_workflow glassbox-research       ================================================  2.40s
  invoke_agent researcher                ===============================================  2.30s
    plan researcher (iter 1)              =======                                         0.35s
      chat gemini-2.5-flash                =====                                          0.25s
    execute_tool web_search                       ======                                  0.30s
    plan researcher (iter 2)                             =======                          0.35s
      chat gemini-2.5-flash                         =====                                 0.25s
    execute_tool fetch_page                                 ========                       0.40s
    plan researcher (iter 3)                                         ======               0.30s
      chat gemini-2.5-flash                                           ====                0.20s
    execute_tool synthesize                                                 ======        0.30s
      chat gemini-2.5-flash                                            ====               0.20s
```

- Expected span count: **12**
- Expected max `glassbox.iteration`: **3**
- Expected ERROR statuses: none
- Expected total cost: baseline (4 model calls, all short inputs)

---

## 6. Expected trace — query 4

Query: *Is Basel III fully implemented in the US?*

Ambiguous and contested. The planner's coverage check keeps failing, so it
re-searches with reworded queries. This is the silent-loop demo.

One research round = search, fetch, extract. Each of the three actions needs a
planner decision first, and each planner decision carries a nested `chat`.
`extract_claims` carries a nested `chat` of its own.

Spans per round:

| Span | Nested child | Count |
|---|---|---|
| `plan` (choose search) | `chat` | 2 |
| `execute_tool web_search` | — | 1 |
| `plan` (choose fetch) | `chat` | 2 |
| `execute_tool fetch_page` | — | 1 |
| `plan` (choose extract) | `chat` | 2 |
| `execute_tool extract_claims` | `chat` | 2 |
| **Total per round** | | **10** |

Arithmetic: 3 rounds × 10 = 30, plus the give-up `plan` and its `chat` (2), plus
`synthesize` and its `chat` (2), plus `invoke_workflow` and `invoke_agent` (2).

```
invoke_workflow glassbox-research    ==============================================  14.0s
  invoke_agent researcher             =============================================  13.8s

    --- round 1 ---
    plan researcher (iter 1)           ==                                             0.4s
      chat gemini-2.5-flash             =                                             0.3s
    execute_tool web_search               ==                                          0.6s
    plan researcher (iter 2)                ==                                        0.4s
      chat gemini-2.5-flash                  =                                        0.3s
    execute_tool fetch_page                   ===                                     0.9s
    plan researcher (iter 3)                     ==                                   0.4s
      chat gemini-2.5-flash                       =                                   0.3s
    execute_tool extract_claims                    ====                               1.2s
      chat gemini-2.5-flash                         ===                               1.1s

    --- round 2 --- (coverage below threshold, reworded search)
    plan researcher (iter 4)                           ==                             0.4s
      chat gemini-2.5-flash                             =                             0.3s
    execute_tool web_search                              ==                           0.6s
    plan researcher (iter 5)                               ==                         0.4s
      chat gemini-2.5-flash                                =                          0.3s
    execute_tool fetch_page                                 ===                        0.9s
    plan researcher (iter 6)                                   ==                      0.4s
      chat gemini-2.5-flash                                     =                      0.3s
    execute_tool extract_claims                                  ====                  1.2s
      chat gemini-2.5-flash                                       ===                  1.1s

    --- round 3 --- (identical shape to round 2, iterations 7, 8, 9)
    ... 10 spans, same pattern

    --- give up and answer ---
    plan researcher (iter 10)                                            ==            0.4s
      chat gemini-2.5-flash                                               =            0.3s
    execute_tool synthesize                                                 ====       1.4s
      chat gemini-2.5-flash                                                  ===       1.3s
```

- Expected span count: **36**
- Expected max `glassbox.iteration`: **10**
- Expected ERROR statuses: **none**
- Expected cost relative to query 1: **roughly 10x** — 14 model calls instead of
  4, and 3 of them feed full regulatory documents into the model

The line that matters is "Expected ERROR statuses: none." Thirty-six spans, ten
planner iterations, ten times the cost, and the run reports complete success.
Nothing in a log file or an HTTP status code would surface this. Only the shape
of the trace does.

---

## 7. Acceptance test for Phase 2

Instrumentation is correct when:

1. A run of query 1 produces the tree in section 5 — 12 spans, same nesting.
2. A run of query 4 produces a tree matching section 6 in shape. Exact span
   count will vary with the planner's judgement; a result within roughly 30% of
   36 confirms the model is right. A result of 12 means the loop is not
   triggering and the coverage threshold needs tightening.
3. Every `chat` span carries non-zero input and output token counts.
4. Every `plan` and `execute_tool` span has exactly one parent, and it is
   `invoke_agent`.
5. Every `plan` span has exactly one child, and it is a `chat` span.
6. No `gen_ai.*` attribute exists that is not defined in section 3.

Any mismatch between prediction and reality is recorded in this document rather
than silently corrected. The gap is the finding.