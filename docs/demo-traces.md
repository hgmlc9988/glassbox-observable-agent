# Demo traces

Four public Langfuse traces from the Glassbox research agent. No sign-in
required. Each is one complete run of the agent, captured with OpenTelemetry.

---

## 1. Healthy run

**Question:** What is the FDIC deposit insurance limit per depositor per bank?

Eight turns, three searches, three pages read, three corroborating sources, one
clean cited answer. This is what the agent looks like when everything works.

https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/c2ed80c7809d3b6cf718e4222b54f7e4

| | |
|---|---|
| Latency | _fill in_ |
| Cost | _fill in_ |
| Tokens | _fill in_ |

---

## 2. Degraded run — right answer, wrong route

**Question:** What changes under the FDIC bank supervision rule effective
November 2026?

Twelve turns and **twelve searches** to open four pages. Turns 8, 9 and 10 are
three consecutive searches with the same stated reason: it needs a third
source. It already had nineteen facts.

The stopping rule counts distinct *sources*, not whether the question is
answered. So an agent with plenty of material kept looking for one more URL
until the turn budget ran out, then produced a low-confidence answer under the
forced-synthesis fallback.

Nothing here errors. The answer is good. Only the trace shows the waste.

https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/f0096f66cf0a162c67cd8f80bfbc0dae

| | |
|---|---|
| Latency | _fill in_ |
| Cost | _fill in_ |
| Tokens | _fill in_ |

---

## 3. Before the fix — budget exhausted, nothing returned

**Question:** Is Basel III fully implemented in the US?

Twelve turns, nine searches, two sites refused the request, and the run ended
with `answered: false`. All the research happened and none of it reached the
user. The `invoke_agent` span carries ERROR with
`error.type: step_budget_exceeded`.

This is the run that prompted the forced-synthesis change: an agent that does
the work and then refuses to speak is worse than one that answers with a
caveat.

https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/eb24a20458d6735c9aca74985e4b6029

| | |
|---|---|
| Latency | 1m 14s |
| Cost | $0.0252 |
| Tokens | 16,985 |

---

## 4. After the fix — same question, answer delivered

**Question:** Is Basel III fully implemented in the US?

Same agent, same question, two failed fetches again — but this run reaches a
cited answer with `answered: true` and `answer_confidence: normal`.

Note this trace predates the input/output instrumentation, so the question and
answer live in the span attributes rather than the header. The behaviour is the
point, not the presentation.

https://us.cloud.langfuse.com/project/cmtw2asiz01hdad0dbfisnzyv/traces/c1fc5a23f3110b385d4cd4efaec55436

| | |
|---|---|
| Latency | 1m 17s |
| Cost | $0.0312 |
| Tokens | 22,968 |

---

## Reading traces 3 and 4 together

Same question. Same tooling. Same two sites blocking the agent. One returns
nothing, one returns a cited answer.

Neither run logged an error a monitoring dashboard would catch. The difference
is only visible in the shape of the trace — and finding it is what produced the
fix.