# Multi-Agent Parallel Risk Scoring (Option 7)

## Problem

The single-agent risk assessment (`run_flow` with `user-risk-profile` skill) scores all four dimensions sequentially in one Claude conversation. Claude fetches data, scores auth, fetches more data, scores permissions, and so on — each step waits for the previous one. The four dimensions have entirely disjoint data requirements and no inter-dependencies. There is no reason for them to run one after another.

## Solution

Fan out to four independent Claude agents — one per risk dimension — running concurrently via `asyncio.gather`. Each agent is fully self-contained: it opens its own MCP session, sees only the tools it needs, fetches its own data, and returns a structured score. Pure Python synthesizes the final report.

---

## Architecture

```
run_flow_parallel_risk(user_id)
    │
    ├── asyncio.gather(                           ← all four launch simultaneously
    │     run_dimension_agent("auth",        user_id)   ← own session, own tools
    │     run_dimension_agent("permissions", user_id)   ← own session, own tools
    │     run_dimension_agent("behaviour",   user_id)   ← own session, own tools
    │     run_dimension_agent("account",     user_id)   ← own session, own tools
    │   )
    │
    └── _synthesize_risk_report(auth, perms, behav, acct)   ← pure Python
```

Wall-clock time ≈ slowest single agent, not the sum of all four.

---

## Scoped tool sets

Each agent only sees the MCP tools its dimension actually needs. This prevents cross-dimension tool calls and reduces Claude's decision space:

| Dimension | MCP Tools | Max Score |
|---|---|---|
| auth | `get_user`, `get_user_activity` | 6 |
| permissions | `get_user`, `get_user_permissions` | 5 |
| behaviour | `get_user_activity` | 4 |
| account | `get_user`, `get_audit_log` | 3 |

Each agent's tool list is built from `USER_TOOLS` by name:
```python
_DIMENSION_TOOLS = {
    "auth":        [t for t in USER_TOOLS if t["name"] in {"get_user", "get_user_activity"}],
    "permissions": [t for t in USER_TOOLS if t["name"] in {"get_user", "get_user_permissions"}],
    "behaviour":   [t for t in USER_TOOLS if t["name"] in {"get_user_activity"}],
    "account":     [t for t in USER_TOOLS if t["name"] in {"get_user", "get_audit_log"}],
}
```

---

## Structured output via `report_dimension_score`

Each agent ends by calling a special reporting tool instead of writing free text. This guarantees a parseable dict without fragile text extraction — the same pattern used by `_check_completeness` and `_critique_response`.

```python
_DIMENSION_SCORE_TOOL = {
    "name": "report_dimension_score",
    "description": "Call this when you have finished fetching data and scoring your dimension.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score":     {"type": "integer"},
            "max_score": {"type": "integer"},
            "factors":   {"type": "array", "items": {"type": "string"}},  # conditions that added points
            "evidence":  {"type": "array", "items": {"type": "string"}},  # specific data points
        },
        "required": ["score", "max_score", "factors", "evidence"],
    },
}
```

When the loop sees `block.name == "report_dimension_score"`, it captures `block.input` and exits:

```python
if block.name == "report_dimension_score":
    score_result = block.input    # {"score": 4, "max_score": 6, "factors": [...], "evidence": [...]}
    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": '{"status": "recorded"}'})
```

The loop exits as soon as the score is recorded — Claude doesn't need to continue.

---

## Dimension skill files

Four focused `SKILL.md` files, one per dimension, under `skills/risk-{dimension}/`. Each contains only the scoring rules for that dimension and which tools to call — no dependency on `_base` or other skills.

| Skill file | Scoring rules |
|---|---|
| `skills/risk-auth/SKILL.md` | MFA disabled (+2), >10 failed logins (+2), >5 unique IPs (+2), dormant >90 days (+1) |
| `skills/risk-permissions/SKILL.md` | Admin perms (+2), write to sensitive resources (+1 each max 3), contractor with high perms (+2) |
| `skills/risk-behaviour/SKILL.md` | Failure rate >20% (+2), sensitive resource access (+1), off-hours activity (+1) |
| `skills/risk-account/SKILL.md` | Already flagged (+2), contractor/vendor type (+1), account age <30 days (+1) |

Each skill ends with an explicit instruction to call `report_dimension_score` as the final step.

---

## `run_dimension_agent` implementation

```python
async def run_dimension_agent(dimension: str, user_id: str) -> dict:
    system_prompt = _build_system_prompt(load_skill(f"risk-{dimension}"))
    messages      = [{"role": "user", "content": f"Score the {dimension} risk dimension for user {user_id}."}]
    all_tools     = _DIMENSION_TOOLS[dimension] + [_DIMENSION_SCORE_TOOL]
    cached_tools  = _cache_tools(all_tools)   # one breakpoint, on the last tool
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    score_result  = None

    async with start_mcp_session() as session:
        while True:
            response = await client.messages.create(
                model=MODEL_ID, max_tokens=2048,
                system=cached_system, tools=cached_tools, messages=messages,
            )
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "report_dimension_score":
                        score_result = block.input
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": '{"status": "recorded"}'})
                    else:
                        result = await execute_tool(session, block.name, block.input)
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "assistant", "content": response.content})
            if score_result or response.stop_reason == "end_turn":
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

    return score_result or {"score": 0, "max_score": 0, "factors": [], "evidence": []}
```

Key points:
- Each agent opens and closes its own MCP session — sessions cannot be shared across concurrent agents
- Prompt caching is applied to both `cached_system` and `cached_tools`
- Fallback `{"score": 0, ...}` guards against an agent that fails to call the reporting tool

---

## `_synthesize_risk_report` — pure Python synthesis

No coordinator LLM call. The four dimension scores are summed and formatted directly:

```python
def _synthesize_risk_report(user_id, auth, perms, behav, acct) -> str:
    total = auth["score"] + perms["score"] + behav["score"] + acct["score"]
    level = classify(total)   # 0-2 Low, 3-5 Medium, 6-9 High, 10+ Critical
    # format markdown table + evidence bullets
```

This is faster, cheaper, and more predictable than a synthesizer LLM call — the structure of the report is fixed, not creative.

---

## Integration

| Surface | How |
|---|---|
| All-in-one CLI | Option 7 in `run_flow.py` menu |
| Orchestrator `/flow` | `flow_type="risk-parallel"` |
| Orchestrator `/flow/stream` | `flow_type="risk-parallel"` (returns full report as one SSE event) |
| Client CLI | Option 7 — user provides user ID directly |

Note: `run_flow_parallel_risk` takes `user_id` directly (not a free-text `user_request`), because the parallel agents each need a resolved user ID to fetch their data. The client menu passes the user ID as the request.

---

## Comparing serial vs parallel

| | Serial (`run_flow` + `user-risk-profile`) | Parallel (`run_flow_parallel_risk`) |
|---|---|---|
| Claude instances | 1 | 4 |
| Model calls | 3–5 (multi-round tool loop) | 4 (one per agent, concurrent) |
| MCP sessions | 1 | 4 (concurrent) |
| Wall-clock time | Sum of all rounds | ≈ slowest single agent |
| Score basis | One conversation with all data | Four independent assessments |
| Synthesis | Claude writes the full report | Pure Python formats the table |

---

## Planned improvements

### Parallel MCP calls within each agent

Currently each dimension agent fetches its data sequentially (e.g., the auth agent calls `get_user` then `get_user_activity`). Since both are read-only, they could run concurrently:

```python
# Instead of sequential tool execution in the loop:
results = await asyncio.gather(
    execute_tool(session, "get_user", {"user_id": user_id}),
    execute_tool(session, "get_user_activity", {"user_id": user_id, "days": 30}),
)
```

This stacks a second level of parallelism — four agents each making concurrent tool calls — for the maximum possible speedup.

### Coordinator LLM for narrative synthesis

`_synthesize_risk_report` formats a fixed table. A coordinator LLM call could produce a more nuanced narrative — weighing which dimension is most alarming, cross-referencing factors across dimensions (e.g., contractor with no MFA AND admin permissions is more alarming than either alone):

```python
coordinator_prompt = f"Given these four dimension scores: {scores}, write a concise risk summary that highlights the most critical cross-dimension concerns."
```

### Streaming per-agent progress

Currently `/flow/stream` returns the full parallel result as one event. With phase events, the client could show live progress as each agent completes:

```
data: {"phase": "agents running"}\n\n
data: {"agent_done": "behaviour", "score": "4/4"}\n\n
data: {"agent_done": "account", "score": "1/3"}\n\n
data: {"agent_done": "auth", "score": "6/6"}\n\n
data: {"agent_done": "permissions", "score": "5/5"}\n\n
data: {"text": "## Risk Assessment..."}\n\n
data: {"done": true}\n\n
```
