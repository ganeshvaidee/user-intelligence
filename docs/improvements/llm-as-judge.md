# LLM-as-Judge (Structured Output via `tool_choice`). Options 5 and 6

## What it is

LLM-as-Judge is a pattern where a second LLM call evaluates the output of the first — not to answer the user, but to assess quality, completeness, or correctness. The judge is a separate model call with its own system prompt, its own task, and its own output format.

In this project, two judges are used:

| Judge | Function | Option | Question it answers |
|---|---|---|---|
| Completeness | `_check_completeness()` | 5 | "Did the response cover everything the request asked for?" |
| Critic | `_critique_response()` | 6 | "Are there errors, unjustified claims, or gaps in this assessment?" |

Both use `tool_choice={"type": "any"}` to force structured output — the judge cannot respond with free text, it must call a tool and return a parsed dict. This is the key technique that makes judge output reliably parseable.

Both judges are called from **four** places — each flow has a blocking variant and a streaming variant:

| Judge | Blocking flow | Streaming flow |
|---|---|---|
| Completeness | `run_flow_until_complete()` | `run_flow_until_complete_stream()` |
| Critic | `run_flow_with_reflection()` | `run_flow_with_reflection_stream()` |

The streaming variants are the ones the orchestrator's `/flow/stream` endpoint uses, so in three-service mode (client → orchestrator) those are the judge calls that actually run. The judge call itself is never streamed — it happens silently between rounds, with the MCP session closed.

---

## How structured output works via `tool_choice`

The Anthropic API's `tool_choice` parameter controls whether Claude must call a tool:

| Value | Behaviour |
|---|---|
| `{"type": "auto"}` | Claude decides whether to call a tool (default) |
| `{"type": "any"}` | Claude must call one of the provided tools |
| `{"type": "tool", "name": "x"}` | Claude must call tool `x` specifically |

By passing a single judge tool and setting `tool_choice={"type": "any"}`, Claude is forced to call that tool — it cannot respond with prose. The result is always a `tool_use` block, which is parsed directly:

```python
result = await client.messages.create(
    tools       = [_COMPLETENESS_TOOL],
    tool_choice = {"type": "any"},      # forces tool_use, never text
    ...
)
return next(b for b in result.content if b.type == "tool_use").input
# → always returns a dict — no string parsing, no regex, no fragile extraction
```

This is a general pattern for getting structured output from Claude without needing a separate parsing step.

**On `MODEL_ID`:** the judges use the same model as the main flow, imported from `flows/llm_client.py`. That module selects the provider at import time — `BEDROCK_MODEL_ID` from `bedrock_client.py` when running on Bedrock, `MODEL_ID` from `anthropic_client.py` when calling the Anthropic API directly — and re-exports both as `MODEL_ID`. Judge code never references the provider-specific name.

---

## The completeness judge — `_check_completeness()`

**File:** `flows/tools.py`

**Used in:** `run_flow_until_complete()` and `run_flow_until_complete_stream()` — called between rounds to decide whether to continue or stop.

### Tool schema

```python
_COMPLETENESS_TOOL = {
    "name": "report_completeness",
    "description": "Report whether the response fully addresses the original request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "complete": {
                "type": "boolean",
                "description": "True if the response fully addresses the request with sufficient evidence.",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific data points or checks that are missing or insufficient.",
            },
        },
        "required": ["complete", "missing"],
    },
}
```

### The call

```python
async def _check_completeness(original_request: str, response: str) -> dict:
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 512,
        system      = [{"type": "text", "text": "You are a quality checker for user intelligence assessments. Be precise and critical.", "cache_control": {"type": "ephemeral"}}],
        tools       = [_COMPLETENESS_TOOL],
        tool_choice = {"type": "any"},
        messages    = [{
            "role":    "user",
            "content": (
                f"Original request: {original_request}\n\n"
                f"Response produced:\n{response}\n\n"
                "Is this response complete? What specific data points were not checked?"
            ),
        }],
    )
    return next(b for b in result.content if b.type == "tool_use").input
```

### What it returns

```python
{"complete": True,  "missing": []}
# or
{"complete": False, "missing": ["Audit log not checked", "Account age not mentioned"]}
```

### How the result is used

```python
check   = await _check_completeness(user_request, all_text)
missing = check.get("missing") or []   # defensive — LLMs sometimes omit empty arrays

if check.get("complete"):
    break   # done

missing_text = "\n".join(f"- {m}" for m in missing)
messages.append({
    "role":    "user",
    "content": f"Your response is incomplete. Please also check:\n{missing_text}",
})
# → feeds gaps back into the same conversation, Claude runs another tool-use pass
```

Note `check.get("missing") or []` — even though `missing` is marked `required` in the schema, Claude occasionally omits it when `complete=True`. Defensive access prevents a KeyError.

### The judge is skipped on the final round

In `run_flow_until_complete()` the `round_num == max_rounds` break comes *before* the judge call — there is no point asking "is this complete?" when no further round can act on the answer. So the judge runs at most `max_rounds - 1` times:

```python
for round_num in range(1, max_rounds + 1):
    messages, round_text = await _run_tool_loop(...)
    all_text += round_text

    if round_num == max_rounds:
        break                                    # ← judge never called on the last round

    check = await _check_completeness(user_request, all_text)
```

With the default `max_rounds=3` that is **2** judge calls, not 3.

### The two convergence implementations differ on the exit condition

The blocking and streaming variants do not break identically:

| | `run_flow_until_complete()` | `run_flow_until_complete_stream()` |
|---|---|---|
| Break condition | `if check.get("complete")` | `if check.get("complete") or not missing` |
| Judge sees | `all_text` — every round concatenated | `round_text` — the current round only |

Two consequences:

1. A verdict of `{"complete": false, "missing": []}` ends the streaming loop but not the blocking one. The streaming variant treats "nothing specific is missing" as good enough; the blocking variant keeps looping until the judge commits to `complete: true` or `max_rounds` runs out.
2. The blocking judge accumulates context across rounds, so by round 3 it is grading rounds 1+2+3 together. The streaming judge only ever sees the latest round's text, so gaps filled in an earlier round can be re-reported as missing.

Neither divergence is intentional design — it is drift between the two implementations, and worth collapsing into a shared helper.

---

## The critic judge — `_critique_response()`

**File:** `flows/tools.py`

**Used in:** `run_flow_with_reflection()` and `run_flow_with_reflection_stream()` — called once after the initial response to decide whether a revision pass is needed.

### Tool schema

```python
_CRITIQUE_TOOL = {
    "name": "report_critique",
    "description": "Report errors, unsupported claims, or gaps in the assessment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_issues": {
                "type": "boolean",
                "description": "True if there are substantive errors or gaps.",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific errors, unjustified claims, or gaps.",
            },
        },
        "required": ["has_issues", "issues"],
    },
}
```

### The call

```python
async def _critique_response(original_request: str, response: str) -> dict:
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 512,
        system      = [{"type": "text", "text": "You are a critical reviewer of user intelligence risk assessments. Check that risk scores are justified by the evidence shown. Flag any score inflation, unsupported conclusions, or missing caveats.", "cache_control": {"type": "ephemeral"}}],
        tools       = [_CRITIQUE_TOOL],
        tool_choice = {"type": "any"},
        messages    = [{
            "role":    "user",
            "content": (
                f"Original request: {original_request}\n\n"
                f"Assessment to review:\n{response}\n\n"
                "Are there errors, unjustified claims, or important gaps?"
            ),
        }],
    )
    return next(b for b in result.content if b.type == "tool_use").input
```

### What it returns

```python
{"has_issues": False, "issues": []}
# or
{"has_issues": True,  "issues": ["Risk score of 12 not justified — no evidence cited for off-hours activity", "Contractor status mentioned but not scored"]}
```

### How the result is used

```python
critique = await _critique_response(user_request, initial_text)
issues   = critique.get("issues") or []

if not critique.get("has_issues") or not issues:
    return initial_text   # no revision needed — return immediately

issues_text = "\n".join(f"- {issue}" for issue in issues)
messages.append({
    "role":    "user",
    "content": (
        f"Your assessment has the following issues:\n{issues_text}\n\n"
        "Please revise your assessment to address these points."
    ),
})
_, revised_text = await _run_tool_loop(
    system_prompt, messages, session, verbose, seen_calls,
    tools=tools, completed=completed,     # ← security guardrails carry into the revision
)
return revised_text
```

The revision happens in the same conversation thread — Claude sees all prior tool results and can correct without re-fetching data.

Note the `tools=` and `completed=` arguments. The revision pass runs under the **same** guardrails as the initial pass: `tools` is the skill-scoped tool list from `tools_for_skills(skill_names)`, and `completed` is the set of tools that have already succeeded, carried forward so the order guard (`ORDER_REQUIREMENTS`) does not reset. A critic cannot widen Claude's tool access, and Claude cannot use the revision pass to reach a tool the flow's skills never declared.

---

## Differences between the two judges

| | Completeness judge | Critic judge |
|---|---|---|
| Called in | `run_flow_until_complete` (+ `_stream`) | `run_flow_with_reflection` (+ `_stream`) |
| CLI option | 5 | 6 |
| Called how many times | Once between rounds — at most `max_rounds - 1` | Once per flow |
| Question | "What's missing?" | "What's wrong?" |
| Output drives | Whether to continue looping | Whether to revise |
| On positive result | Break the loop | Return initial response immediately |
| On negative result | Append missing items, loop again | Append issues, run revision pass |

---

## Limitations

**Judges are not grounded.** Both judges only see the text of Claude's response — they don't see the raw tool results. A judge can't verify that a score is mathematically correct or that a cited IP count matches the activity data. It can only assess whether the reasoning is internally consistent and the claims are plausible.

**Judge disagreement is not handled.** A single judge call decides completeness or quality. If the judge is wrong (false positive or false negative), there's no second opinion. The convergence loop may exit early if the judge incorrectly says `complete=True`, or run unnecessary rounds if it incorrectly says `complete=False`.

**`required` fields are not always respected.** Claude sometimes omits `missing` or `issues` when the array would be empty, despite the schema marking them `required`. The code handles this with `.get("missing") or []` but it's a reminder that schema `required` is advisory, not enforced.

**Judge system prompts are generic.** Both judges use short generic prompts. The completeness judge doesn't know what a *complete* risk assessment looks like; it infers from context. A more specific prompt with a rubric would catch more genuine gaps.

**A judge that returns no tool block crashes the flow.** Both judges end with:

```python
return next(b for b in result.content if b.type == "tool_use").input
```

`tool_choice={"type": "any"}` makes a text-only response unlikely, but `next()` on an empty iterator raises `StopIteration` — and there is no `default` argument and no try/except. If it happens (a refusal, a `max_tokens` truncation mid-block, a provider-side stop), the exception propagates out of the flow function and the orchestrator returns a 500 with no partial result, discarding work Claude has already completed. A `next(..., None)` with a fail-open fallback (`{"complete": True}` / `{"has_issues": False}`) would degrade to "no judge this round" instead of losing the whole response.

**The judge is not applied to the parallel risk flows.** Options 7–9 (`run_flow_parallel_risk`, `run_flow_parallel_risk_with_memory`) fan out to four dimension agents and merge them in `_synthesize_risk_report()` — pure Python string assembly plus a threshold lookup. No LLM adjudicates the four independent scores. Those agents do use the same forced-`tool_choice` structured-output technique (`report_dimension_score`), but as reporters, not as judges.

---

## Planned improvements

### 1. Rubric-based judge prompts

**Problem:** The completeness judge says "Be precise and critical" but doesn't know what a complete risk assessment actually requires. It can miss domain-specific gaps — e.g., not knowing that `get_audit_log` should always be called.

**How it works:** Pass a flow-specific rubric to the judge instead of a generic prompt:

```python
COMPLETENESS_RUBRICS = {
    "risk": (
        "A complete risk assessment must include: "
        "1) user record with MFA and employee type, "
        "2) 30-day activity with failure rate and unique IP count, "
        "3) full permissions list with high-risk flagged, "
        "4) audit log checked for prior admin actions, "
        "5) numeric score with per-dimension breakdown."
    ),
    "offboard": (
        "A complete offboarding must include: lookup, risk score, "
        "flag confirmation, and deactivation confirmation."
    ),
}

check = await _check_completeness(user_request, all_text, rubric=COMPLETENESS_RUBRICS["risk"])
```

---

### 2. Multi-judge voting

**Problem:** A single judge call can be wrong. A false `complete=True` exits the convergence loop early; a false `has_issues=True` triggers an unnecessary revision.

**How it works:** Run N judge calls in parallel and take a majority vote:

```python
votes = await asyncio.gather(*[
    _check_completeness(user_request, all_text)
    for _ in range(3)
])
complete = sum(1 for v in votes if v.get("complete")) >= 2   # majority
```

Three calls with a 2/3 majority threshold makes false positives and false negatives much less likely. Cost: 3× the judge calls per round — only worth it for high-stakes flows like offboarding.

---

### 3. Evidence-grounded critique

**Problem:** The critic sees only Claude's response text, not the raw tool results. It can't verify that a cited fact is accurate — e.g., that the failure rate percentage matches the actual activity data.

**How it works:** Pass the raw tool results alongside the response so the critic can cross-check:

```python
async def _critique_response(original_request, response, tool_results: list[dict]) -> dict:
    tool_summary = json.dumps(tool_results, indent=2)
    content = (
        f"Original request: {original_request}\n\n"
        f"Raw tool results:\n{tool_summary}\n\n"
        f"Assessment produced:\n{response}\n\n"
        "Check that every claim in the assessment is supported by the tool results. "
        "Flag any number, percentage, or conclusion not directly derivable from the data."
    )
```

This turns the critic from a plausibility checker into a fact-checker — much stronger, but requires threading the tool results through to the critique call.