# Prompt Caching

## Problem

Every call to `client.messages.create` sends the full system prompt (skills content) and the full `USER_TOOLS` list on every round. In a 3-round convergence flow with multiple tool calls per round, the same ~3KB of skills text and ~2KB of tool schemas is re-processed by the model every single time — wasting tokens and adding latency.

## Solution

Mark static content with `cache_control: {type: ephemeral}` so the API processes it once and serves subsequent calls from cache at ~90% lower token cost. Caching is transparent — it has no effect on Claude's output, only on cost and speed. It works identically on Bedrock and the direct Anthropic API; nothing in the request shape changes between providers.

## What gets cached

| Content | Location | Repeated across | Gain |
|---|---|---|---|
| System prompt (skills) | `_run_tool_loop()` | Every tool-use round in all flows | High |
| `USER_TOOLS` schemas | `_run_tool_loop()` | Every tool-use round in all flows | Medium |
| Judge system prompts | `_check_completeness`, `_critique_response` | Each convergence/reflection round | Low |

## How prompt caching works

The Anthropic API accepts `system` as either a plain string or a list of content blocks. To cache, use the list form and add a `cache_control` breakpoint to the block you want cached:

```python
system = [
    {
        "type": "text",
        "text": "...skills content...",
        "cache_control": {"type": "ephemeral"}
    }
]
```

For tools, add `cache_control` to all tools in the list. The API caches everything up to and including the last entry:

```python
cached_tools = [{**tool, "cache_control": {"type": "ephemeral"}} for tool in USER_TOOLS]
```

`cache_control: {type: ephemeral}` means the cache lives for 5 minutes — long enough to cover all rounds in a single flow, automatically expiring after.

## Changes

### `flows/run_flow.py` — `_run_tool_loop()`

Convert `system` from a string to a cached content block. Build a cached copy of the tools list with the breakpoint on the last entry.

```python
# Before
response = await client.messages.create(
    model      = MODEL_ID,
    max_tokens = 4096,
    system     = system_prompt,
    tools      = USER_TOOLS,
    messages   = msgs,
)

# After
cached_tools = [{**tool, "cache_control": {"type": "ephemeral"}} for tool in USER_TOOLS]
response = await client.messages.create(
    model      = MODEL_ID,
    max_tokens = 4096,
    system     = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
    tools      = cached_tools,
    messages   = msgs,
)
```

### `flows/tools.py` — judge calls

Same pattern for the static system prompts in `_check_completeness` and `_critique_response`. Low individual gain but correct practice — if the judge is called across multiple convergence rounds, the system prompt is cached after the first.

## Cache hit pattern per flow

**`run_flow` (single shot):**
```
Round 1, Call 1: system + tools → processed + cached
Round 1, Call 2: system + tools → served from cache ✓
Round 1, Call N: system + tools → served from cache ✓
```

**`run_flow_until_complete` (3 rounds):**
```
Round 1, Call 1: system + tools → processed + cached
Round 1, Call 2+: → cache hit ✓
Round 2, Call 1: → cache hit ✓  (cache still warm, same content)
Round 2, Call 2+: → cache hit ✓
Round 3, ...: → cache hit ✓
```

**`run_flow_with_reflection`:**
```
Phase 1, Call 1: system + tools → processed + cached
Phase 1, Call 2+: → cache hit ✓
Phase 3 (revision): → cache hit ✓  (same system prompt, same MCP session)
```

## Verification

1. Run option 5 (convergence loop, usr_005) — most rounds = most cache hits
2. Read the usage fields off the response object — this works on both providers and needs no console access:

   ```python
   print(response.usage.cache_creation_input_tokens)  # tokens written to cache (~1.25x cost)
   print(response.usage.cache_read_input_tokens)      # tokens served from cache (~0.1x cost)
   print(response.usage.input_tokens)                 # uncached tokens (full cost)
   ```

   Expect `cache_creation_input_tokens > 0` on the first call and `cache_read_input_tokens > 0` on Round 2+. If `cache_read_input_tokens` stays at zero across rounds, something is invalidating the prefix — a changed tool list, a different model ID, or dynamic content in the system prompt.

   On Bedrock, the same numbers are also visible in the AWS console under Model invocations. There is no equivalent console view on the direct Anthropic API — read them off the response.
3. Run `python tests/test_flows.py` — all 8 tests should pass unchanged (caching is transparent to output)
