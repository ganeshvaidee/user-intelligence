# AI / Claude Concepts Used in This Project

## TODOs — Concepts to Explore Next (Evals)

- [ ]  **LLM-as-Judge for eval assertions** — the *runtime* judges are built (see Done below and concept 6); this TODO is about the **eval suite**. `tests/test_flows.py` still asserts with string and regex matching (`assert_response_contains`, `extract_risk_score`). Replace those with a second Claude call that evaluates whether the response is accurate, well-justified, and actionable. More robust than string checks against non-deterministic output.
- [ ]  **Golden dataset** — a fixed set of (request, expected_tools_called, expected_score_range, expected_keywords) tuples that cover all users and all flow types. Currently tests are hand-written per scenario; a dataset makes coverage gaps visible.
- [ ]  **Consistency evals** — run the same request N times and check that scores and tool call sequences are stable. LLM outputs are non-deterministic; high variance on the same input is a signal the skill instructions are ambiguous.

---

## Done

- [X]  **LLM-as-Judge (runtime)** — see concept 6 below and `docs/improvements/llm-as-judge.md`. Completeness judge in option 5, critic judge in option 6, both in blocking and streaming variants. **Not** used in options 1–4 or 7–9. Distinct from the eval-suite judge still open in the TODOs above.
- [X]  **Prompt Caching** — see concept 8 below and `docs/improvements/prompt-caching.md`.
- [X]  **Streaming** — see concept 9 below and `docs/improvements/streaming.md`.
- [X]  **Multi-Agent (Parallel Subagents)** — see concept 10 below and `docs/improvements/multi-agent-parallel.md`.
- [X]  **Extended Thinking** — see concept 11 below and `docs/improvements/extended-thinking.md`. **Only used in options 8 and 9 (parallel agents + extended thinking).**
- [X]  **Memory / Persistence** — see concept 12 below and `docs/improvements/memory-persistence.md`. **Only used in option 9 (parallel + extended thinking + memory).**
- [x]  **Score accuracy evals** — `extract_risk_score` + `assert_score_in_range` in `tests/test_flows.py`. Handles multiple formatting variants (structured template, informal prose, etc.). Single-agent tests use dual-path fallback (score or keyword); parallel agent tests enforce numeric score strictly.
- [x]  **Human-in-the-Loop** — see concept 13 below and `docs/improvements/human-in-the-loop.md`. Two-phase offboarding with a client-owned confirmation gate: `prepare` looks up, scores, and flags; the client blocks on `CONFIRM`; `confirm` deactivates. Cross-phase order-guard handling is the load-bearing detail — `run_flow_offboard_confirm()` verifies in the DB that the account is actually `flagged` before seeding `completed={"flag_user"}`, because the guard tracks the *current conversation* and phase 2 is a new one. Regression-tested in `tests/test_offboard_hitl.py` (4/4), which drives the real flows rather than the reimplemented loop in `test_flows.py`.

---

## Built but NOT working — do not mark done

- [ ]  **Hooks** — see `docs/improvements/hooks.md`. **The hook is never invoked.** `.claude/hooks/skill_regression.sh` is written and checked in, but there is no `hooks` block in any settings file — nothing registers it against `PostToolUse` or any other event. The enable-flag `.claude/hooks/regression.enabled` is also absent, so the script would `exit 0` immediately even if it were wired up. (The docs also disagree with themselves on the event name — `PostToolUse` in one place, `FileChanged` in another.) To finish: add a `hooks` block to a checked-in `.claude/settings.json` and create the flag file.

- [ ]  **Regression suite on skill changes** — depends entirely on Hooks above. `tests/test_flows.py --mode single` runs fine manually; nothing runs it automatically on a SKILL.md edit.

---

## TODOs — Other Concepts to Explore

- [ ]  **Batch Processing** — use the Anthropic Batch API to run risk assessments on a list of users (e.g. all contractors) in parallel rather than serially. Useful for bulk audits.

> Human-in-the-Loop and Hooks were previously listed here as `[x]` *and* in Done — duplicated. HITL is now genuinely complete (see Done); Hooks is under **Built but NOT working** above.

---

## 0. Model Provider: Bedrock or the Anthropic API

Every concept below runs against a Claude model, and the project can reach that model two ways. `flows/llm_client.py` picks one at **import time** and re-exports a single `client` and `MODEL_ID` that the rest of the codebase uses. No other file knows which provider is active:

```python
# flows/llm_client.py
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

if LLM_PROVIDER == "bedrock":
    from bedrock_client import client, BEDROCK_MODEL_ID as MODEL_ID
else:
    from anthropic_client import client, MODEL_ID
```

**The default is the direct Anthropic API** — Bedrock is opt-in via `LLM_PROVIDER=bedrock`.

|  | Direct Anthropic API (default) | AWS Bedrock |
|---|---|---|
| Selected by | `LLM_PROVIDER` unset or any value other than `bedrock` | `LLM_PROVIDER=bedrock` |
| Client module | `flows/anthropic_client.py` | `flows/bedrock_client.py` |
| SDK class | `anthropic.AsyncAnthropic()` | `anthropic.AsyncAnthropicBedrock(...)` |
| Auth | `ANTHROPIC_API_KEY`, or an `ant auth login` profile | AWS credentials from the `default` boto3 profile (access key, secret, session token) |
| Region | n/a | `us-west-2`, set in `bedrock_client.py` |
| Default model ID | `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` (a cross-region inference profile) |
| Model override | `ANTHROPIC_MODEL_ID` | `BEDROCK_MODEL_ID` |
| Extra dependency | none | `boto3` |

The model IDs differ in form: Bedrock inference-profile IDs carry a `us.` region prefix and an `anthropic.` vendor prefix, while the direct API takes the bare model name. This is why code samples in these docs say `MODEL_ID` rather than either literal — `llm_client.py` resolves it.

**What does *not* change between providers:** the Messages API request and response shape, tool schemas, `tool_choice`, prompt caching (`cache_control`), extended thinking, streaming, and the `usage` fields. Every concept documented below works identically on both. The only provider-specific things in this project are client construction, credentials, and the model-ID string.

Setup instructions for each provider are in the [README](../README.md#3-model-access).

---

## 1. Evals (Evaluation Testing)

The project has an eval suite in `tests/test_flows.py` that tests the full end-to-end pipeline — not unit tests of Python functions, but tests of Claude's behaviour given a real user request.

Three types of assertions are used:

**Tool call assertions** — did Claude call the right tools?

```python
assert_tools_called(tools, ["get_user_activity", "get_user_permissions"])
assert_tools_not_called(tools, ["deactivate_user"])  # safety check
```

**Response content assertions** — does the output contain expected content?

```python
assert_response_contains(response, ["Alice", "alice@company.com", "Engineering"])
assert_response_not_contains(response, ["deactivate", "immediate"])
```

**Safety rule assertions** — were the skill safety rules followed?

```python
# flag_user must be called before deactivate_user, never after
if tools.index("flag_user") > tools.index("deactivate_user"):
    failures.append("SAFETY VIOLATION: deactivate_user called before flag_user")
```

Each test runs a real flow against the real MCP server and a real model API — no mocking. This catches the failure mode where mocked tests pass but real Claude behaviour diverges from what the skill intended.

Tests cover: lookup by ID, lookup by email, MFA warning surfacing, high-risk scoring, low-risk scoring, confirmation gate enforcement, inactive user handling, and the flag-before-deactivate safety rule.

---

## 2. Tool Use (Function Calling)

Claude is given a list of tool schemas (`USER_TOOLS`) and decides which to call, with what arguments, and in what order. The core Claude capability — instead of answering from training data, it calls real functions to get live data.

The tools are defined twice: as FastMCP `@mcp.tool()` decorators (for the MCP protocol) and as Anthropic JSON schemas (for the model API call). Claude sees the JSON schemas; the MCP server executes the actual Python.

```python
# What Claude sees (flows/tools.py)
USER_TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch a user record by ID. Returns name, email, status...",
        "input_schema": { "type": "object", "properties": { "user_id": {...} } }
    },
    ...
]

# What actually runs (mcp-server/server.py)
@mcp.tool()
def get_user(user_id: str) -> dict:
    return fetch_user(user_id)
```

---

## 3. Agentic Loop

Claude doesn't answer in one shot. It runs in a loop — call the model, execute tools, feed results back, call the model again — until it decides it has enough information (`stop_reason == "end_turn"`). Claude drives the loop; the code just dispatches whatever Claude returns.

```
while True:
    response = call model(messages, tools)

    for block in response:
        if tool_use → execute tool → collect result
        if text     → accumulate

    if stop_reason == "end_turn" → break
    append tool results to messages → loop again
```

Claude decides which tools to call, in what order, and when to stop. The loop has no exit condition based on data — only on Claude's signal.

---

## 4. Skills (System Prompt Engineering)

Skills are Markdown files injected into Claude's system prompt. They tell Claude *what steps to follow*, *what rules to apply*, and *what output format to produce* — without any Python logic. The skill instructs; the tool executes.

Skills compose via dependency order — each skill builds on the ones before it:

```
_base                     ← shared: error handling, output format, safety rules
  └── lookup-user         ← fetch + summarise a user record
        └── user-risk-profile   ← score across 4 risk dimensions
              └── offboard-user ← lookup → risk → flag → confirm → deactivate
```

Loading order matters — always load `_base` first:

```python
load_skill("_base", "lookup-user", "user-risk-profile")
# → concatenated into Claude's system prompt
```

The skill files also double as **prompts** in Claude Desktop via `@mcp.prompt()`, appearing in the prompt picker so users can select the right skill before asking a question.

---

## 5. MCP (Model Context Protocol)

The MCP server exposes database operations as tools Claude can call. It supports two transports:


| Transport       | When used                                                                  |
| --------------- | -------------------------------------------------------------------------- |
| stdio           | Claude Desktop, all-in-one CLI — server spawned as subprocess             |
| streamable-HTTP | Three-service mode — server runs independently, client connects over HTTP |

The client (`tools.py`) opens an MCP session, dispatches Claude's `tool_use` blocks to it via `session.call_tool()`, and returns results — Claude never touches the database directly.

```
Claude decides to call get_user("usr_005")
  → execute_tool(session, "get_user", {"user_id": "usr_005"})
    → session.call_tool(...)   ← MCP protocol
      → server.py get_user()
        → database.py fetch_user()
          → SQLite
```

The client switches transport based on the `MCP_URL` environment variable — if set, HTTP; if unset, stdio subprocess.

---

## 6. LLM-as-Judge (Structured Output via `tool_choice`)

Two separate LLM calls act as judges — not to answer the user, but to evaluate Claude's own output:


| Judge        | Function                | Option | Question it answers                                        |
| ------------ | ----------------------- | ------ | ---------------------------------------------------------- |
| Completeness | `_check_completeness()` | 5      | "Did the response cover everything the request asked for?" |
| Critic       | `_critique_response()`  | 6      | "Are there errors, unjustified claims, or gaps?"           |

Each judge is called from two places — the blocking flow (`run_flow_until_complete`, `run_flow_with_reflection`) and its streaming twin (`..._stream`). The streaming variants are what `/flow/stream` uses, so those are the judge calls that run in three-service mode. Judge calls are never streamed to the client; they run silently between rounds.

Both use `tool_choice={"type": "any"}` to force structured output instead of prose. This is a key pattern — the judge is forced to call a tool so the result is always a parseable dict, with no fragile string extraction:

```python
result = await client.messages.create(
    tools       = [_COMPLETENESS_TOOL],
    tool_choice = {"type": "any"},   # Claude MUST call the tool, not write prose
    ...
)
# returns: {"complete": bool, "missing": [...]}
return _first_tool_input(
    result, {"complete": True, "missing": [], "judge_unavailable": True}
)
```

`tool_choice` forces a tool call but can't guarantee one — truncation or a refusal can leave no readable block. `_first_tool_input()` fails open to the supplied default and tags it `judge_unavailable`, which propagates out as a `warnings` entry on the API response rather than being silently reported as a passed check.

See `docs/improvements/llm-as-judge.md` for the full design, including the remaining limitation: judges see only the response text, not the raw tool results, so they check plausibility rather than facts.

---

## 7. Multi-turn Conversation (Stateful Context)

The `msgs` list grows across tool-use rounds within a single flow. Every tool call Claude made and every result it received stays in context. Claude can see its full history and does not re-fetch data it already has.

```
msgs after two tool rounds:
[
  {user:      "Give me a risk assessment for usr_005"},
  {assistant: [ToolUseBlock(get_user)]},
  {user:      [tool_result: {...user data...}]},
  {assistant: [ToolUseBlock(get_user_activity), ToolUseBlock(get_user_permissions)]},
  {user:      [tool_result: {...activity...}, tool_result: {...permissions...}]},
  {assistant: [TextBlock("## Risk Assessment...")]},
]
```

In the convergence loop, conversation state accumulates across multiple rounds — so Round 2 only fetches what Round 1 missed, not everything again. In the critic-revise pattern, the revision phase continues the same thread so Claude can correct its answer without any additional tool calls.

---

## 8. Prompt Caching

Every call to `client.messages.create` sends the full system prompt (skills content) and the full `USER_TOOLS` list. In a 3-round convergence flow, the same ~3KB of skills text and ~2KB of tool schemas is processed by the model on every single model call — wasting tokens and adding latency.

Prompt caching marks static content with `cache_control: {type: ephemeral}` so the API processes it once and serves subsequent calls from cache at ~90% lower token cost. The mechanism and request shape are identical on Bedrock and the direct Anthropic API. The cache lives for 5 minutes — long enough to cover all rounds in a single flow.

### What is cached

**System prompt** — the skills content is identical across all rounds within a flow. Converted from a plain string to a content block with a cache breakpoint:

```python
system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
```

**Tools list** — the tool list never changes within a flow. A single cache breakpoint goes on the **last** tool, via the `_cache_tools()` helper in `run_flow.py`:

```python
cached_tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
```

The API caches all tools up to and including that entry, so one marker covers the whole list. Marking every tool instead would exceed the **4 breakpoints per request** limit and return a `400` — see `docs/improvements/prompt-caching.md`.

**Judge system prompts** — `_check_completeness` and `_critique_response` also cache their static system strings. Low individual gain but correct practice — the system prompt is cached after the first judge call and served from cache in any subsequent rounds.

### Cache hit pattern

```
Round 1, Call 1:  system + tools → processed and cached
Round 1, Call 2+: system + tools → served from cache ✓
Round 2, Call 1:  system + tools → cache still warm, served from cache ✓
Round 2, Call 2+: system + tools → served from cache ✓
```

In a 3-round convergence flow with 3 model calls per round, 8 of 9 calls are cache hits on the system prompt and tools — the most expensive tokens in each request.

### Where the changes live

- `flows/run_flow.py` — `_run_tool_loop()`: system prompt and tools
- `flows/tools.py` — `_check_completeness()` and `_critique_response()`: judge system prompts

No other files changed. Caching is transparent — it has no effect on Claude's output, only on cost and latency.

See `docs/improvements/prompt-caching.md` for the full design.

---

## 9. Streaming

Before streaming, every model call blocked until Claude finished the entire response. For a 20–30 second risk assessment the user saw nothing, then received everything at once. Streaming delivers text tokens as Claude generates them.

Two surfaces are implemented:

**Verbose CLI mode** (`_run_tool_loop`, `verbose=True`) — switches from `client.messages.create()` to `client.messages.stream()` and prints each token immediately as it arrives. Tool call logging still appears between rounds.

**Orchestrator SSE endpoint** (`/flow/stream`) — a new FastAPI endpoint backed by the `run_flow_stream` async generator. Yields SSE events token by token; the client consumes them and prints to the terminal in real time.

```
data: {"text": "## Risk Assessment"}\n\n
data: {"text": " — Eve Contractor (usr_005)"}\n\n
...
data: {"done": true}\n\n
```

The key pattern — `stream.text_stream` yields text tokens, `stream.get_final_message()` returns the complete message for tool processing:

```python
async with client.messages.stream(...) as stream:
    async for text in stream.text_stream:
        yield text                          # stream to caller
    response = await stream.get_final_message()  # process tool calls from final message
```

All three flow types stream. Judge and critic calls run silently between rounds — only Claude's text reaches the client.


| Mode                                | Streaming?                     |
| ----------------------------------- | ------------------------------ |
| All-in-one CLI, verbose=True        | ✅ tokens printed as generated |
| Three-service client, any flow_type | ✅ SSE from`/flow/stream`      |

See `docs/improvements/streaming.md` for the full design.

---

## 10. Multi-Agent Parallel Risk Scoring

The single-agent risk assessment scores all four dimensions (Authentication, Permissions, Behaviour, Account) sequentially in one conversation. Each dimension has entirely disjoint data requirements — there is no reason for them to wait on each other.

This concept fans out to **four independent Claude agents**, one per dimension, running concurrently via `asyncio.gather`. Each agent:

- Opens its own MCP session
- Sees only the MCP tools its dimension needs (scoped tool sets)
- Fetches its own data and applies its scoring rules
- Returns a structured score by calling a `report_dimension_score` tool (same `tool_choice`-style pattern as the LLM judges)

Pure Python synthesizes the final report — no coordinator LLM call needed.

```
asyncio.gather(
    run_dimension_agent("auth",        user_id)  ← get_user + get_user_activity
    run_dimension_agent("permissions", user_id)  ← get_user + get_user_permissions
    run_dimension_agent("behaviour",   user_id)  ← get_user_activity
    run_dimension_agent("account",     user_id)  ← get_user + get_audit_log
)
→ _synthesize_risk_report(auth, perms, behav, acct)   ← pure Python
```

**Structured output per agent:** each agent calls `report_dimension_score` as its final action, returning `{score, max_score, factors, evidence}`. No text parsing — the result is a dict captured directly from the tool_use block, the same pattern used by `_check_completeness` and `_critique_response`.

**Skill scoping:** four new `SKILL.md` files under `skills/risk-{dimension}/`, each containing only the scoring rules and tool instructions for that dimension. Agents can't drift into other dimensions' logic.

**New flow:** `run_flow_parallel_risk(user_id)` — available as option 7 in the CLI, `flow_type="risk-parallel"` in the orchestrator, and wired through `/flow/stream`.

See `docs/improvements/multi-agent-parallel.md` for the full design.

---

## Orchestration Patterns (Coding Patterns)

These are not AI concepts — they are coding patterns built on top of the AI concepts above. They compose the agentic loop, LLM-as-judge, multi-turn conversation, and multi-agent primitives into reusable flow functions.

### Single Shot (`run_flow`)

Plain agentic loop. Claude calls tools until it decides it's done.

```
user request → [agentic loop] → response
```

### Convergence Loop (`run_flow_until_complete`)

After each round a completeness judge checks whether the response fully covered the request. If not, missing items are fed back and Claude runs another pass in the same conversation thread.

```
round 1: [agentic loop] → response
         → completeness judge → {complete: false, missing: ["audit log"]}
         → "Your response is incomplete. Please also check: ..."
round 2: [agentic loop continues same conversation] → response
         → completeness judge → {complete: true} → done
```

### Critic-Revise (`run_flow_with_reflection`)

Runs the full agentic loop once, then a critic LLM reviews the output. If issues are found, Claude revises in the same conversation thread — retaining all prior tool results without re-fetching.

```
phase 1: [agentic loop] → initial response
phase 2: critic LLM → {has_issues: true, issues: [...]}
phase 3: [agentic loop continues same conversation] → revised response
```

### Parallel Risk (`run_flow_parallel_risk`)

Fans out to four dimension agents concurrently, synthesizes with pure Python.

```
asyncio.gather(auth agent, permissions agent, behaviour agent, account agent)
→ _synthesize_risk_report()
```

### Comparison


|                   | `run_flow`    | `run_flow_until_complete` | `run_flow_with_reflection`   | `run_flow_parallel_risk`          |
| ----------------- | ------------- | ------------------------- | ---------------------------- | --------------------------------- |
| Extra LLM calls   | None          | 1 judge/round             | 1 critic + optional revision | 4 agents concurrent               |
| Self-correction   | None          | Completeness gap-filling  | Error/claim verification     | Independent per-dimension         |
| MCP sessions      | 1             | 1 per round               | 1 shared                     | 4 concurrent                      |
| Extended Thinking | ❌            | ❌                        | ❌                           | ✅ per dimension agent            |
| Use when          | Task is clear | Thoroughness matters      | Accuracy matters             | Risk scoring speed + auditability |

---

## 11. Extended Thinking

> **Scope: Option 7 only** (`run_flow_parallel_risk` → `run_dimension_agent`). Options 1–6 do not use extended thinking.

Extended Thinking enables Claude to reason step-by-step *before* producing its final output. The thinking is returned as a separate `ThinkingBlock` alongside the response. For risk scoring, this makes the scoring logic auditable — you can see exactly which conditions Claude evaluated, what the data showed, and why each triggered (or didn't).

**Without extended thinking** (options 1–6): Claude reads the data and produces a score. The reasoning is implicit — you see `Authentication: 6/6` but not how Claude counted the failed logins or whether it applied the right threshold.

**With extended thinking** (option 7): Claude thinks through each condition explicitly before calling `report_dimension_score`:

```
[THINKING — AUTH]
MFA disabled → +2.
Failed logins: 15 out of 60 events in 30 days. 15 > 10 threshold → +2.
Unique IPs in 7 days: 8. 8 > 5 threshold → +2.
Last login: recent. Dormant condition not met → +0.
Total: 6/6.
```

### How it works

The `thinking` parameter is added to the model call in `run_dimension_agent`:

```python
response = await client.messages.create(
    model         = MODEL_ID,
    max_tokens    = 10000,
    thinking      = {"type": "adaptive", "display": "summarized"},
    output_config = {"effort": "high"},
    system        = cached_system,
    tools         = cached_tools,
    messages      = messages,
)
```

**Adaptive**, not a fixed budget: Claude decides how deeply to think per request, and `effort` tunes that depth. `display="summarized"` is required — the default is `"omitted"`, which returns thinking blocks with empty text and would silently blank out the `[THINKING — …]` output above. The older `{"type": "enabled", "budget_tokens": N}` form is deprecated on Sonnet 4.6 and rejected outright on newer models; see `docs/improvements/extended-thinking.md` for the migration rationale.

`ThinkingBlock`s in the response are logged in verbose mode and skipped for tool routing — only `tool_use` blocks are dispatched to the MCP server.

### Where to see it

**All-in-one CLI** (`python flows/run_flow.py` → option 7, verbose=True): thinking blocks print as `[THINKING — AUTH]`, `[THINKING — PERMISSIONS]` etc. before each agent's score.

**Three-service mode** (client → orchestrator → option 7): thinking is not forwarded over SSE, but the synthesized report always includes an **Agent Reasoning** section — a one-sentence summary each agent writes after thinking, captured via the required `reasoning` field in `report_dimension_score`:

```
### Agent Reasoning
- **Authentication:** No MFA on a contractor account with 15 failed logins from 8 external IPs
- **Permissions:** Admin DB access combined with contractor status
- **Behaviour:** 25% failure rate exceeds 20% threshold; accessed secrets and admin-panel
- **Account:** Contractor type only — not flagged, not new
```

### Skill guidance

Each dimension skill file (`skills/risk-{dimension}/SKILL.md`) has a "How to use your thinking" section that instructs Claude to evaluate each condition explicitly with exact numbers before reporting — guiding the thinking toward systematic condition-checking rather than free-form prose.

See `docs/improvements/extended-thinking.md` for the full design.

---

## 12. Memory / Persistence

> **Scope: Option 9 only** (`run_flow_parallel_risk_with_memory`). Options 1–8 do not persist or retrieve prior assessments.

Every risk assessment previously started cold — Claude had no awareness of whether it had assessed the same user before or whether the risk profile was trending up or down. Memory adds continuity: each completed assessment is saved to the database, and the next run retrieves it before launching the agents. Pure Python computes the delta and adds a comparison section to the report.

**No skill changes needed.** The memory fetch and save are Python-driven via MCP tool calls — not instructions to Claude. The agents score their dimensions independently (unchanged from option 7/8); only the synthesis step sees the prior result.

### How it works

```
run_flow_parallel_risk_with_memory(user_id)
    │
    ├── MCP → get_prior_assessment(user_id)   ← Python fetches, agents don't know
    │
    ├── asyncio.gather(4 dimension agents with extended thinking)
    │
    ├── _synthesize_risk_report(..., prior=prior)   ← comparison section added
    │
    └── MCP → save_assessment(user_id, scores...)   ← persisted for next run
```

### New MCP tools

Two new tools added to the MCP server and `USER_TOOLS`:


| Tool                                         | What it does                                                       |
| -------------------------------------------- | ------------------------------------------------------------------ |
| `get_prior_assessment(user_id)`              | Returns last saved assessment dict, or`{none: true}` if first time |
| `save_assessment(user_id, total_score, ...)` | Writes current scores + level + summary to`assessments` table      |

### What the output looks like

**First run** — baseline message at the bottom:

```
### Prior Assessment
None — this is the baseline assessment. Scores will be compared on the next run.
```

**Subsequent runs** — delta comparison:

```
### Change Since Prior Assessment
Prior: 13/18 (🔴 Critical) on 2026-06-24
Current: 16/18 — overall change: **+3**
- Authentication: ↑ 2 points
- Behaviour: ↑ 1 point
```

### Database

New `assessments` table in `seed/users.db` (created with `IF NOT EXISTS` so existing DBs don't need re-seeding). Stores per-dimension scores, total, risk level, and a one-sentence summary sourced from the agents' `reasoning` field.

See `docs/improvements/memory-persistence.md` for the full design.

---

## 13. Human-in-the-Loop

> **Scope: Option 3 (Full offboarding) only.** All other flows are fully automated.

The original offboard skill had a confirmation gate inside the flow — Claude would ask "Type CONFIRM to proceed." This works in the all-in-one CLI (interactive stdin) but fails in the three-service mode where the orchestrator is blocking on an HTTP request with no way for the human to respond mid-flow.

The fix: split offboarding into two separate, stateless API calls with the client owning the human pause in between.

### How it works

```
Client → POST /offboard/prepare/stream
             Claude: lookup → risk → flag → return summary
         ← streaming report (risk score, permissions, last login)

Client presents summary to human
Human reviews and types CONFIRM (or cancels)

Client → POST /offboard/confirm/stream   (only if CONFIRM)
             Claude: deactivate_user → return completion
         ← streaming completion report
```

**The orchestrator is stateless.** No session, no queue, no mid-flow pausing. The only state between phases is `user_id` and `reason` — held in local variables in the client. The DB flag applied in Phase 1 is the durable state: if the human cancels, the account stays flagged as a security signal.

### Two new skills

| Skill | Steps | STOP condition |
|---|---|---|
| `offboard-prepare` | Lookup → risk → flag | Stops after flagging — explicitly told not to deactivate |
| `offboard-confirm` | Deactivate only | Human confirmed; proceeds directly |

### Two new orchestrator endpoints

```
POST /offboard/prepare/stream  →  Phase 1 SSE stream
POST /offboard/confirm/stream  →  Phase 2 SSE stream
```

Both take `{user_id, reason}` — not the generic `{user_request, skill_names}` — since the skills are fixed for these endpoints.

### Client as the agent

The client is the human-facing layer — it owns the confirmation gate:

```python
call_offboard_phase_stream("/offboard/prepare/stream", user_id, reason)  # Phase 1

response = input("Type CONFIRM to deactivate: ").strip()
if response.upper() != "CONFIRM":
    print("Cancelled. Account remains flagged.")
    return

call_offboard_phase_stream("/offboard/confirm/stream", user_id, reason)   # Phase 2
```

The existing `offboard-user` skill and single-phase flow are unchanged — backward compatible for direct `run_flow` calls.

See `docs/improvements/human-in-the-loop.md` for the full design.

---

## Deployment Modes: claude.ai vs Claude Desktop vs Python Service

The same Claude model powers all three modes. What differs is what Claude has access to and how much programmatic control you have over its behaviour.

### claude.ai

Claude in the browser. No local access. You can paste instructions and have a conversation, but Claude can only reason over what you give it in the chat. Cannot reach local databases, filesystems, or APIs.

### Claude Desktop

Same Claude model, same cloud inference, but with two local superpowers:

**1. Local MCP servers** — Claude can call tools that run on your machine. In this project `get_user`, `flag_user`, etc. hit a local SQLite file that claude.ai could never reach. Claude Desktop spawns `mcp-server/server.py` as a subprocess and communicates over stdin/stdout.

**2. Local skills/context** — Project instructions loaded from local SKILL.md files (or selected via the MCP prompt picker) tell Claude how to behave for your specific domain. The model inference still happens in Anthropic's cloud — what's local is the tooling and the instructions.

Claude Desktop closes the gap between "a general Claude conversation" and "a Claude that knows your data and your domain rules" — without needing to build a full application.

### Python Service (orchestrator + client)

---

## Client Options 1–9: Skills → Tools → Flow Pattern

The client presents 9 options. Each option is NOT a unique skill combination — instead, options share skills but differ in **flow pattern** (how Claude processes the task). Here is the full chain:

| Option | Task | Skills Used | Tools Claude Calls | Flow Pattern |
|---|---|---|---|---|
| 1 | Look up user | `_base`, `lookup-user` | `get_user`, `find_user_by_email`, `get_user_activity`, `get_user_permissions` | Single-shot agentic loop |
| 2 | Risk assessment | `_base`, `lookup-user`, `user-risk-profile` | Same as 1 + `get_audit_log` | Single-shot agentic loop |
| 3 | Full offboarding | `_base`, `lookup-user`, `user-risk-profile`, `offboard-user` | Same as 2 + `flag_user`, `deactivate_user` | Single-shot + Human-in-the-Loop |
| 4 | Find by email + risk | `_base`, `lookup-user`, `user-risk-profile` | Same as 2 | Single-shot (different prompt, same skills/tools) |
| 5 | Risk (convergence) | `_base`, `lookup-user`, `user-risk-profile` | Same as 2 | Convergence loop (judge after each round) |
| 6 | Risk (critic-revise) | `_base`, `lookup-user`, `user-risk-profile` | Same as 2 | Critic-revise (critique then revise in same thread) |
| 7 | Risk (parallel agents) | `risk-auth`, `risk-permissions`, `risk-behaviour`, `risk-account` | `get_user`, `get_user_activity`, `get_user_permissions`, `get_audit_log` (scoped per agent) | 4 concurrent dimension agents |
| 8 | Risk + extended thinking | `risk-auth`, `risk-permissions`, `risk-behaviour`, `risk-account` | Same as 7 (scoped per agent) | 4 concurrent dimension agents + extended thinking |
| 9 | Risk + thinking + memory | `risk-auth`, `risk-permissions`, `risk-behaviour`, `risk-account` | Same as 7 + `get_prior_assessment`, `save_assessment` (Python-driven) | 4 concurrent agents + extended thinking + memory persistence |

### Key Insights

- **Options 2, 4, 5, 6 share skills and tools** — the *flow pattern* is what differs. Option 2 is fastest (one pass); option 5 loops until complete; option 6 critiques then revises.
- **Options 7, 8, 9 use different skills** — the dimension-specific skills (`risk-auth`, etc.) replace the monolithic `user-risk-profile` skill, enabling parallel agents with scoped tool access.
- **Option 3 is unique** — it includes write tools (`flag_user`, `deactivate_user`) and adds a human-in-the-loop confirmation gate between phases.
- **Memory is Python-driven** — option 9's `get_prior_assessment` and `save_assessment` are called by Python, not instructed by the skill. Claude never knows it's comparing to prior results.



The full power layer. Adds programmatic control that Claude Desktop can't provide:

- **Parallel agents** — `asyncio.gather` across 4 dimension agents simultaneously
- **Extended thinking** — adaptive thinking + `effort` on the model call, not exposed in Claude Desktop
- **Streaming** — SSE token-by-token delivery to the client
- **Convergence/reflection** — second LLM judge calls between rounds
- **Memory/persistence** — prior assessments stored and compared across runs
- **HITL** — two-phase offboarding with client-owned confirmation gate

### Capability comparison

| Capability | claude.ai | Claude Desktop | Python Service |
|---|---|---|---|
| Chat with Claude | ✅ | ✅ | ✅ (via client CLI) |
| Query local SQLite DB | ❌ | ✅ via MCP | ✅ via MCP |
| Domain-specific skill rules | Manual paste | ✅ project instructions | ✅ loaded programmatically |
| Single-agent risk assessment | ❌ | ✅ | ✅ |
| Parallel agents + extended thinking | ❌ | ❌ | ✅ options 7–9 |
| Convergence / reflection loops | ❌ | ❌ | ✅ options 5–6 |
| Streaming SSE to client | ❌ | ❌ | ✅ |
| Memory across sessions | ❌ | ❌ | ✅ option 9 |
| HITL two-phase offboarding | ❌ | ✅ (in-chat CONFIRM) | ✅ (two HTTP calls) |

Claude Desktop and the Python service are complementary — Desktop for interactive ad-hoc queries, the service for automated pipelines and richer flow patterns.
