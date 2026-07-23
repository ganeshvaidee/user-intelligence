# Agentic Loop (option 5)

## What it is

The agentic loop is the core execution pattern of this project. Instead of Claude answering a question in a single call, it runs in a conversation cycle — calling Bedrock, executing whatever tools Claude requests, feeding the results back, and repeating — until Claude decides it has enough information to write its final answer.

Claude drives the loop entirely. The Python code only dispatches tool calls and feeds results back. It has no logic for which tools to call, in what order, or when to stop — all of that is Claude's decision.

---

## The core loop — `_run_tool_loop()`

**File:** `flows/run_flow.py`

```python
async def _run_tool_loop(
    system_prompt: str,
    messages:      list[dict],
    session,
    verbose:       bool = False,
    seen_calls:    dict | None = None,
) -> tuple[list[dict], str]:
```

### What it does step by step

```
1. Call Bedrock with current messages + tools + system prompt
2. For each block in the response:
     - TextBlock  → accumulate text
     - ToolUseBlock → execute tool via MCP → collect result
3. Append assistant turn (including any tool calls) to messages
4. If stop_reason == "end_turn" → break
5. If there are tool results → append as user turn → go to 1
```

### What grows in `msgs` across iterations

Each pass through the loop adds two turns to `msgs`:

```
Initial:
  [{role: user, content: "Give me a risk assessment for usr_005"}]

After Bedrock call 1 (Claude calls get_user):
  [{role: user,      content: "Give me a risk assessment..."},
   {role: assistant, content: [ToolUseBlock(get_user, {user_id: usr_005})]},
   {role: user,      content: [tool_result: {name: Eve, status: active, mfa: false...}]}]

After Bedrock call 2 (Claude calls get_user_activity + get_user_permissions):
  [...same as above...,
   {role: assistant, content: [ToolUseBlock(get_user_activity), ToolUseBlock(get_user_permissions)]},
   {role: user,      content: [tool_result: {...activity...}, tool_result: {...permissions...}]}]

After Bedrock call 3 (Claude writes final answer, stop_reason = end_turn):
  [...same...,
   {role: assistant, content: [TextBlock("## Risk Assessment — Eve Contractor...")]}]
   ← loop breaks here
```

By the time Claude writes its final answer, it can see every tool it called, every result it received, and everything it has said — the full reasoning trail is in `msgs`.

### Multiple tools in one call

Claude can return multiple `ToolUseBlock`s in a single response when it decides it needs several things and they don't depend on each other. The loop collects all results before making the next Bedrock call:

```python
tool_results = []
for block in response.content:
    if block.type == "tool_use":
        result = await execute_tool(session, block.name, block.input)
        tool_results.append({...})          # collect all

# all results appended together as one user turn
msgs.append({"role": "user", "content": tool_results})
```

### Duplicate call detection

`seen_calls` tracks every `tool_name:{args}` pair called so far. If Claude calls the same tool with the same arguments a second time, a warning is printed:

```
[DUPLICATE TOOL CALL] get_user({"user_id": "usr_005"}) already called — redundant MCP call
```

In the convergence loop, `seen_calls` is shared across all rounds so cross-round duplicates are caught too. The call still executes — this is diagnostic only.

### Stop condition

The only exit condition is `response.stop_reason == "end_turn"`. Claude sets this when it decides it has enough data to write its answer. The loop has no timeout, no maximum iteration count, and no data-based exit condition — it runs as long as Claude keeps returning tool calls.

---

## The three flow patterns built on `_run_tool_loop`

### Pattern 1 — `run_flow()` — single shot

Simplest pattern. One MCP session, one call to `_run_tool_loop`, return the text.

```python
async with start_mcp_session() as session:
    _, response_text = await _run_tool_loop(system_prompt, messages, session, verbose)
return response_text
```

```
user request → [_run_tool_loop] → response_text
```

### Pattern 2 — `run_flow_until_complete()` — convergence loop

After each `_run_tool_loop` call, a completeness judge checks whether the response covers the original request. If not, missing items are appended to `messages` as a follow-up and the loop runs again — in the same conversation thread.

```
for round in range(max_rounds):
    open MCP session
    messages, text = _run_tool_loop(messages)   ← conversation grows each round
    close MCP session

    check = _check_completeness(request, text)  ← judge LLM call (no MCP)
    if complete → break
    messages.append("Please also check: {missing}")  ← feed gaps back
```

Key design decisions:
- **Fresh MCP session per round** — the HTTP connection to the MCP server can be dropped during the judge call (which can take 10–30s). A new session per round avoids the TaskGroup crash.
- **Same `messages` list across rounds** — Claude retains all prior tool results. Round 2 only fetches what Round 1 missed.
- **Same `seen_calls` dict across rounds** — duplicate detection spans the full flow, not just one round.
- **`max_rounds` hard ceiling** — exits after N rounds even if the judge never returns `complete: true`.

### Pattern 3 — `run_flow_with_reflection()` — critic-revise

Phase 1: `_run_tool_loop` produces an initial response.
Phase 2: A critic LLM reviews it for errors and unjustified claims.
Phase 3: If issues are found, Claude revises — in the same conversation thread, with the same MCP session.

```python
seen_calls = {}

async with start_mcp_session() as session:
    # Phase 1
    messages, initial_text = await _run_tool_loop(..., seen_calls)

    # Phase 2 — critique (no MCP needed)
    critique = await _critique_response(request, initial_text)

    if not critique.get("has_issues"):
        return initial_text   # done, no revision needed

    # Phase 3 — revision in same conversation
    messages.append({role: user, content: "Your assessment has issues: ..."})
    _, revised_text = await _run_tool_loop(messages, session, ..., seen_calls)

return revised_text
```

Key design decisions:
- **One MCP session for all three phases** — the critique call is fast (no tool calls), so the session doesn't time out between Phase 1 and Phase 3.
- **Same conversation thread for revision** — Claude sees all prior tool results in `messages` and can correct its answer without re-fetching any data.
- **Early exit if no issues** — if the critic finds nothing wrong, the initial response is returned immediately.

---

## Limitations of the current approach

**No loop timeout or max iterations.** `_run_tool_loop` runs until `end_turn`. If Claude enters a pathological tool-calling pattern (unlikely but possible), the loop runs indefinitely. There's no circuit breaker.

**Tool calls are serial within a round.** Even when Claude returns multiple `ToolUseBlock`s in one response, `execute_tool` is called sequentially in a `for` loop. For two independent calls like `get_user_activity` and `get_user_permissions`, there's no reason they can't run concurrently.

**No max_tokens guard.** `msgs` grows unboundedly. A very long flow could approach the model's context window limit, causing a Bedrock error. There's no check on message list size before calling.

**`stop_reason` is the only signal.** The loop trusts Claude to stop. There's no secondary check — e.g., if Claude loops on `tool_use` without making progress, the code won't detect it.

---

## Planned improvements

### 1. Parallel tool execution within a round

**Problem:** When Claude returns `[get_user_activity, get_user_permissions]` in one response, they are executed sequentially even though they are independent — one result is not needed to call the other.

**How it works:** Collect all `tool_use` blocks first, then run them concurrently with `asyncio.gather`:

```python
tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

# Run all tool calls in this round concurrently
results = await asyncio.gather(*[
    execute_tool(session, b.name, b.input)
    for b in tool_use_blocks
])

tool_results = [
    {"type": "tool_result", "tool_use_id": b.id, "content": r}
    for b, r in zip(tool_use_blocks, results)
]
```

The results are then appended in the same order as the `tool_use` blocks so the conversation structure is unchanged. Wall-clock time for a round with N independent tools drops from N×latency to 1×latency.

**Constraint:** Tool results must be returned in the same order and with the matching `tool_use_id` — `asyncio.gather` preserves order, so this is safe.

---

### 2. Max iterations guard

**Problem:** No circuit breaker exists. A runaway loop (Claude repeatedly calling tools without reaching `end_turn`) would run until Bedrock returns an error or the process is killed.

**How it works:** Add a `max_iterations` parameter with a sensible default:

```python
async def _run_tool_loop(..., max_iterations: int = 20):
    iteration = 0
    while True:
        iteration += 1
        if iteration > max_iterations:
            print(f"[WARNING] Tool loop hit max_iterations ({max_iterations}) — forcing stop")
            break
        ...
```

20 iterations is generous — a typical risk assessment uses 3–5 Bedrock calls. Hitting 20 is a signal something is wrong, not a normal operating condition.

---

### 3. Context window guard

**Problem:** `msgs` grows with every tool call and every result. For large activity logs or many rounds in the convergence loop, the message list could approach the model's context limit (200K tokens for Claude Sonnet), causing a Bedrock error with no graceful handling.

**How it works:** Estimate token count before each Bedrock call and warn (or truncate old tool results) if approaching the limit:

```python
import json

def _estimate_tokens(msgs: list[dict]) -> int:
    # rough estimate: 1 token ≈ 4 chars
    return len(json.dumps(msgs)) // 4

MAX_TOKENS_BEFORE_WARN = 150_000

if _estimate_tokens(msgs) > MAX_TOKENS_BEFORE_WARN:
    print(f"[WARNING] Conversation approaching context limit — consider truncating old tool results")
```

A more sophisticated version would summarise or drop old tool results from earlier rounds, keeping only the assistant turns that reference them.