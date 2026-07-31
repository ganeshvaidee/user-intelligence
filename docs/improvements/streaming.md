# Streaming

## Problem

Before streaming, every call to `client.messages.create` blocked until Claude finished generating the complete response. For a risk assessment that takes 20–30 seconds, the user saw nothing — then received everything at once. This is poor UX for a CLI tool and makes it impossible to tell whether the system is working.

## Solution

Use `client.messages.stream()` to receive text tokens as Claude generates them. Two streaming surfaces are implemented:

1. **Verbose CLI mode** — `_run_tool_loop` prints tokens in real time when `verbose=True`
2. **Orchestrator streaming endpoint** — `/flow/stream` returns an SSE stream that the client consumes token by token

---

## How streaming works in the Anthropic SDK

`client.messages.stream()` is an async context manager that yields text tokens incrementally:

```python
async with client.messages.stream(
    model=..., system=..., tools=..., messages=...
) as stream:
    async for text in stream.text_stream:
        print(text, end="", flush=True)   # prints each token as it arrives
    response = await stream.get_final_message()  # complete message for tool processing
```

`stream.text_stream` yields only text chunks — tool_use blocks are not streamed as text. After the stream completes, `get_final_message()` returns the full `Message` object with all content blocks (text + tool_use), which is needed to process tool calls.

This means streaming does not change tool execution logic — it only changes how text output is delivered to the user.

---

## Change 1: `_run_tool_loop` streaming (`flows/run_flow.py`)

When `verbose=True`, the loop now uses `client.messages.stream()` instead of `client.messages.create()`:

```python
if verbose:
    async with client.messages.stream(
        model      = MODEL_ID,
        max_tokens = 4096,
        system     = cached_system,
        tools      = cached_tools,
        messages   = msgs,
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)   # token arrives → printed immediately
        response = await stream.get_final_message()
    if any(b.type == "text" for b in response.content):
        print()   # newline after streamed text
else:
    response = await client.messages.create(...)   # non-verbose: unchanged
```

The text block loop no longer prints text (it was `if verbose and block.text: print(block.text)`). Text is now printed during the stream — the final message is only used to accumulate `accumulated_text` and to process tool calls.

Tool call logging (`[TOOL CALL]`, `[TOOL RESULT]`) still prints after the stream completes, so the verbose output is:

```
[streaming text tokens appear here as Claude writes them...]

[TOOL CALL] get_user_permissions({"user_id": "usr_005"})
[TOOL RESULT] {"total": 6, "high_risk_count": 5, ...}

[streaming text continues for next round...]
```

**When verbose=False** (orchestrator calls), `client.messages.create()` is still used — no streaming overhead for programmatic callers that don't display output.

---

## Change 2: Streaming async generators (`flows/run_flow.py`)

Three new async generator functions — one per flow pattern — for the orchestrator endpoint. Each runs the same logic as its blocking counterpart but `yield`s text chunks instead of returning a complete string. Judge and critic calls run silently between yields.

**Key design:** text is yielded immediately inside the `async with start_mcp_session()` context, token by token as Claude generates it. The session stays open during streaming and only closes after tool calls complete — never across a yield between rounds or phases. This keeps the anyio TaskGroup scoped to a single round/phase and prevents it from leaking into the event loop state between requests.

> **Bug fixed:** the initial implementation buffered all text inside the session and yielded it as a batch after the session closed. This caused text to appear all at once per model call rather than token by token — effectively defeating streaming. The fix was to yield inside the session, which is safe because each round/phase opens its own fresh session.

### `run_flow_stream` — single shot

One fresh MCP session per model call. Yields text tokens while the session is open; session closes after tool calls execute.

```python
while True:
    async with start_mcp_session() as session:   # fresh session each iteration
        async with client.messages.stream(...) as stream:
            async for text in stream.text_stream:
                yield text                        # ← immediate, session open
            response = await stream.get_final_message()

        for block in response.content:
            if block.type == "tool_use":
                result = await execute_tool(session, block.name, block.input)
                tool_results.append({...})

        messages.append(...)
        stop = response.stop_reason == "end_turn"
    # session closes here — tool calls done, no pending yield

    if stop:
        break
    if tool_results:
        messages.append(...)
```

### `run_flow_until_complete_stream` — convergence loop

Yields text tokens in real time within each round. The completeness judge runs after the session closes — no session is held open during the judge call.

```
Round 1: open session → yield tokens live → execute tools → close session
         completeness judge (silent — no MCP)
         if incomplete: append missing items to messages
Round 2: open session → yield tokens live → execute tools → close session
         completeness judge → complete → stop
```

### `run_flow_with_reflection_stream` — critic-revise

Each phase opens its own session. Text is yielded live within each session; the critic call runs after Phase 1's session closes.

```
Phase 1: open session → yield tokens live → execute tools → close session
Phase 2: critic (silent — no MCP)
         if no issues: return
Phase 3: open session → yield tokens live → execute tools → close session
```

Both phases share a `seen_calls` dict for duplicate detection across the full flow.

---

## Change 3: `/flow/stream` endpoint (`orchestrator/app.py`)

The `/flow/stream` endpoint routes all three flow types through their streaming generators:

```python
@app.post("/flow/stream")
async def run_flow_stream_endpoint(req: FlowRequest):
    async def generate():
        if req.flow_type == "single":
            gen = run_flow_stream(req.user_request, req.skill_names)
        elif req.flow_type == "convergence":
            gen = run_flow_until_complete_stream(req.user_request, req.skill_names, max_rounds=req.max_rounds)
        elif req.flow_type == "reflection":
            gen = run_flow_with_reflection_stream(req.user_request, req.skill_names)

        async for chunk in gen:
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

SSE event format:
```
data: {"text": "## Risk Assessment"}\n\n
data: {"text": " — Eve Contractor"}\n\n
...
data: {"done": true}\n\n
```

Errors are delivered as a final event:
```
data: {"error": "User not found"}\n\n
```

---

## Change 4: Client streaming (`client/cli.py`)

`call_orchestrator_stream` accepts `flow_type` and `max_rounds` and routes all flows through `/flow/stream`. The `main()` function always uses it — the blocking `call_orchestrator` is kept for programmatic use but no longer called from the CLI menu.

```python
def call_orchestrator_stream(user_request, skill_names, flow_type="single", max_rounds=3):
    with httpx.Client(timeout=CLIENT_TIMEOUT) as http:
        with http.stream("POST", f"{ORCHESTRATOR_URL}/flow/stream", json={
            "user_request": user_request,
            "skill_names":  skill_names,
            "flow_type":    flow_type,
            "max_rounds":   max_rounds,
        }) as response:
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event.get("done"):   print(); break
                if "error" in event:   print(f"\nERROR: {event['error']}"); sys.exit(1)
                if "text" in event:    print(event["text"], end="", flush=True)

# main() — all flows stream
call_orchestrator_stream(user_request, skill_names, flow_type)
```

---

## Streaming coverage by mode

| Mode | Surface | Streaming? |
|---|---|---|
| All-in-one CLI (`run_flow.py`) | Terminal | ✅ — `client.messages.stream()` when `verbose=True` |
| Three-service, flow_type=single | Client terminal via SSE | ✅ — `/flow/stream` |
| Three-service, flow_type=convergence | Client terminal via SSE | ✅ — `/flow/stream` (text per round, judge calls silent) |
| Three-service, flow_type=reflection | Client terminal via SSE | ✅ — `/flow/stream` (Phase 1 + Phase 3 text, critic silent) |
| Orchestrator verbose=False | None (programmatic) | ❌ — `client.messages.create()` unchanged |

---

## Planned improvements

### Phase boundary events

Currently the client sees a pause between rounds/phases with no indication of what's happening (judge or critic call in progress). Adding phase events to the SSE stream would let the client display a progress indicator:

```
data: {"text": "## Risk Assessment..."}\n\n
data: {"phase": "checking completeness"}\n\n   ← new: signals a judge call
data: {"text": "## Risk Assessment [Updated]"}\n\n
data: {"done": true}\n\n
```

### Progress events for tool calls

Currently tool calls execute silently between text streams from the client's perspective. Add SSE events for tool call start/end so the client can display a progress indicator:

```
data: {"tool_start": "get_user_activity"}\n\n
data: {"tool_done":  "get_user_activity"}\n\n
```