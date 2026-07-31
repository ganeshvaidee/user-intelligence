# Multi-turn Conversation (Stateful Context)

## What it is

Every call to `client.messages.create` receives the full conversation history in the `messages` parameter. Claude sees not just the current question but every tool it has called, every result it received, and everything it has said so far. This is what makes the agentic loop work — Claude can reason over accumulated data rather than starting from scratch on each model call.

Multi-turn conversation is not a separate feature that gets "enabled" — it is the natural result of maintaining and growing the `msgs` list across loop iterations.

---

## How `msgs` is built

**File:** `flows/run_flow.py`, `_run_tool_loop()`

The conversation starts with the user's request:

```python
messages = [{"role": "user", "content": user_request}]
```

After each model call, the assistant's turn is appended — including any tool call blocks:

```python
msgs.append({"role": "assistant", "content": response.content})
```

After tool results are collected, they are appended as a user turn:

```python
msgs.append({"role": "user", "content": tool_results})
```

The loop then calls the model again with the grown `msgs` list. Claude sees the full history every time.

---

## What `msgs` looks like at each stage

### Initial state

```python
[
    {"role": "user", "content": "Give me a risk assessment for usr_005"}
]
```

### After model call 1 — Claude calls `get_user`

```python
[
    {"role": "user",      "content": "Give me a risk assessment for usr_005"},
    {"role": "assistant", "content": [
        ToolUseBlock(id="tu_001", name="get_user", input={"user_id": "usr_005"})
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_001",
         "content": '{"id": "usr_005", "name": "Eve Contractor", "mfa_enabled": false, ...}'}
    ]},
]
```

### After model call 2 — Claude calls `get_user_activity` + `get_user_permissions`

```python
[
    ... (prior turns) ...,
    {"role": "assistant", "content": [
        ToolUseBlock(id="tu_002", name="get_user_activity",   input={"user_id": "usr_005", "days": 30}),
        ToolUseBlock(id="tu_003", name="get_user_permissions", input={"user_id": "usr_005"}),
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_002", "content": '{"total": 60, "failures": 15, ...}'},
        {"type": "tool_result", "tool_use_id": "tu_003", "content": '{"high_risk_count": 5, ...}'},
    ]},
]
```

### After model call 3 — Claude writes its final answer (`stop_reason == "end_turn"`)

```python
[
    ... (prior turns) ...,
    {"role": "assistant", "content": [
        TextBlock(text="## Risk Assessment — Eve Contractor (usr_005)\n\nRisk Score: 12/15...")
    ]},
]
```

The loop breaks. `msgs` is returned to the caller so it can be extended in subsequent rounds.

---

## Conversation state across flow patterns

### `run_flow` — single pass

`msgs` grows only within one call to `_run_tool_loop`. After the flow returns, the conversation is discarded.

### `run_flow_until_complete` — across convergence rounds

`messages` is passed into each round and returned with the updated state. The same list grows across all rounds — Claude never loses context of prior tool results:

```python
messages = [{"role": "user", "content": user_request}]

for round_num in range(1, max_rounds + 1):
    async with start_mcp_session() as session:
        messages, round_text = await _run_tool_loop(system_prompt, messages, session, ...)
    # messages now contains all tool calls + results from this round

    # append the follow-up as a new user turn
    messages.append({
        "role":    "user",
        "content": "Your response is incomplete. Please also check:\n- Audit log"
    })
    # Round 2 starts with full history — Claude does not re-fetch what it already has
```

**Why this matters:** In Round 2, Claude can call `get_audit_log` without re-calling `get_user`, `get_user_activity`, or `get_user_permissions` — it already has those results in `messages`. Without shared state, every round would start from scratch.

### `run_flow_with_reflection` — across phases

The same `messages` list spans Phase 1 (initial response) and Phase 3 (revision). The revision phase appends the critic's issues as a user turn and calls `_run_tool_loop` again — Claude sees its full initial reasoning and can correct it without re-fetching data:

```python
# Phase 1 — initial response
messages, initial_text = await _run_tool_loop(system_prompt, initial_messages, session, ...)

# Phase 3 — revision (same messages list, same MCP session)
messages.append({
    "role":    "user",
    "content": "Your assessment has issues:\n- Score not justified\nPlease revise."
})
_, revised_text = await _run_tool_loop(system_prompt, messages, session, ...)
```

Claude's revision is grounded in the same tool results it already fetched — no new MCP calls needed unless it wants to double-check something.

---

## Tool result format

Tool results are appended as `user` role turns with `type: tool_result`. Each result must reference the `tool_use_id` of the call that produced it:

```python
{
    "type":        "tool_result",
    "tool_use_id": block.id,     # must match the ToolUseBlock.id exactly
    "content":     result,       # JSON string from execute_tool()
}
```

The `tool_use_id` is how Claude maps results back to the calls it made. If multiple tools were called in one response, all results are bundled into a single user turn — Claude processes them together.

---

## Limitations

### Context window growth

`msgs` grows with every tool call and every result. Sonnet 4.6 supports a 1M token context window — the same on Bedrock and the direct Anthropic API — but a long convergence loop with large tool results (e.g., 100 activity events) could approach that limit. There's no check on message size before the model call — the error from a context overflow would be an unhelpful API error.

Estimated token cost per round:
- `get_user` result: ~100 tokens
- `get_user_activity` result: ~500 tokens (20 events)
- `get_user_permissions` result: ~200 tokens
- Claude's response: ~300–600 tokens

A 3-round convergence flow costs roughly 3,000–4,000 tokens in message history alone — well within limits for current usage, but worth monitoring as flows get more complex.

### No conversation persistence across requests

Each call to `run_flow*` starts a fresh `messages` list. There is no memory of prior requests — Claude cannot say "I assessed usr_005 last week and flagged them; has anything changed?" The conversation ends when the flow function returns.

### Old tool results stay in context

Once a tool result is in `msgs`, it stays there for all subsequent model calls in that flow. In a long convergence loop, early tool results that are no longer relevant still consume context tokens and can influence Claude's reasoning. There's no mechanism to prune or summarise old turns.

---

## Planned improvements

### 1. Context window monitoring

**Problem:** No check exists on message size before the model call. A context overflow produces an unhelpful API error with no graceful handling.

**How it works:** Estimate token count before each model call and warn — or truncate old tool results — if approaching the limit:

```python
def _estimate_tokens(msgs: list[dict]) -> int:
    return len(json.dumps(msgs)) // 4   # rough: 1 token ≈ 4 chars

MAX_CONTEXT_TOKENS = 800_000   # warn at 80% of the 1M limit

if _estimate_tokens(msgs) > MAX_CONTEXT_TOKENS:
    print(f"[WARNING] Context at ~{_estimate_tokens(msgs):,} tokens — approaching limit")
```

A more sophisticated version would drop old tool results from earlier rounds (keeping the assistant turns that reference them) to free context space.

---

### 2. Conversation memory across requests

**Problem:** Each flow call starts fresh. Claude has no awareness of prior assessments of the same user — it always starts from raw data.

**How it works:** Persist a summary of each completed assessment to a store (SQLite, file, or the existing `audit_log` table). At the start of a new flow for the same user, inject the prior summary into the initial user message:

```python
async def run_flow(user_request, skill_names, ...):
    prior = load_prior_assessment(user_id_from_request)
    if prior:
        initial_message = (
            f"{user_request}\n\n"
            f"Note: This user was last assessed on {prior['date']}. "
            f"Prior finding: {prior['summary']}"
        )
    else:
        initial_message = user_request

    messages = [{"role": "user", "content": initial_message}]
    ...
```

This gives Claude the context to compare current state against a baseline — "Last week this user had no failed logins; now there are 15" — without needing a separate memory skill.

---

### 3. Tool result summarisation for long flows

**Problem:** Old tool results accumulate in `msgs` and consume context tokens even when Claude no longer needs them. A 5-round convergence loop with large activity logs could waste thousands of tokens on data already used.

**How it works:** After each round, replace verbose tool result content with a compact summary in the message history:

```python
def _summarise_tool_result(name: str, result: str) -> str:
    data = json.loads(result)
    if name == "get_user_activity":
        return json.dumps({
            "summarised": True,
            "total": data["total"],
            "failures": data["failures"],
            "unique_ips": data["unique_ips"],
        })
    return result   # other tools kept as-is

# After round completes, compress old tool results in msgs
for turn in messages:
    if turn["role"] == "user" and isinstance(turn["content"], list):
        for item in turn["content"]:
            if item["type"] == "tool_result":
                item["content"] = _summarise_tool_result(
                    tool_name_for(item["tool_use_id"]), item["content"]
                )
```

Saves context tokens on subsequent rounds while keeping the key facts Claude needs to avoid re-fetching.