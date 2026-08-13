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
| Judge system prompts | `_check_completeness`, `_critique_response` | — | **None — not cacheable, see below** |

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

For tools, put **one** breakpoint on the **last** tool. The API caches everything up to and including that entry, so a single marker covers every tool before it:

```python
def _cache_tools(tools: list[dict] | None) -> list[dict]:
    if not tools:
        return []
    return [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
```

> ⚠️ **Do not mark every tool.** The API allows at most **4** `cache_control` blocks per request. Marking each tool spends one breakpoint per tool, and with the system prompt taking a fourth, any flow exposing more than three tools fails outright:
>
> ```
> 400 invalid_request_error — A maximum of 4 blocks with cache_control may be provided. Found 6.
> ```
>
> This is not a tuning issue — the request never reaches the model. Caching behaviour is identical either way; only the breakpoint count differs.

`cache_control: {type: ephemeral}` means the cache lives for 5 minutes — long enough to cover all rounds in a single flow, automatically expiring after.

## Minimum cacheable prefix

A `cache_control` breakpoint is a request, not a guarantee. If the prefix it marks is shorter than the model's minimum, the API **silently declines to cache it** — no error, no warning, `cache_creation_input_tokens` comes back `0`, and you pay full price. Claude's output is byte-identical either way, which is why this is invisible without instrumentation.

| Model | Minimum cacheable prefix |
|---|---:|
| Claude Opus 5, Fable 5 | 512 tokens |
| Claude Sonnet 4.6, Sonnet 5, Opus 4.8 | 1024 tokens |
| Claude Opus 4.7 | 2048 tokens |
| Claude Opus 4.6, Opus 4.5, Haiku 4.5 | 4096 tokens |

This repo runs Sonnet 4.6, so the bar is **1024 tokens**. Measured consequences:

- The skills system prompt is ~2296 tokens — clears the bar, and the caching in `_run_tool_loop` produces real hits.
- The judge prefixes are ~180 tokens — cannot clear it, and no breakpoint placement fixes that. See the judge-callers section below.

The minimum is **not monotonic across model generations**: Opus 4.6 needs 4096 while the newer Opus 5 needs 512. Re-check it whenever `MODEL_ID` changes — a prompt that caches on one model can silently stop caching on another with no code change and no error.

> ⚠️ **Open question — is the tools breakpoint doing anything?** Render order is `tools` → `system` → `messages`, and caching is a prefix match, so the breakpoint on the *system* block already caches every tool ahead of it. The separate `_cache_tools` breakpoint may therefore be subsumed, and the tools-only prefix is likely under 1024 tokens on its own. `usage` reports totals rather than per-breakpoint figures, so this cannot be settled by reading numbers from a normal run — it needs an A/B (one run with `_cache_tools`, one without). Not yet done.

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
cached_tools = _cache_tools(tools)          # one breakpoint, on the last tool
response = await client.messages.create(
    model      = MODEL_ID,
    max_tokens = 4096,
    system     = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
    tools      = cached_tools,
    messages   = msgs,
)
```

### `flows/tools.py` — judge calls: deliberately NOT cached

An earlier version of this document recommended applying the same pattern to the judge system prompts, describing it as *"low individual gain but correct practice."* That was wrong on both counts, and the code has been corrected to match.

**It was not low gain — it was zero gain**, every call, guaranteed. Measured with `flows/usage.py` on a critic-revise run:

```
[USAGE] _critique_response  in=1440  cache_w=0  cache_r=0  out=980
```

**And it was not correct practice**, because a reader copying that pattern reproduces a no-op that looks right in review.

**The binding reason: the prefix is below the minimum.** The only cacheable content is the tool schema plus the system prompt — ~180 tokens against a 1024-token bar. The other ~1260 tokens in that measured call are the assessment under review: per-call content that sits after any breakpoint, so there is no static text left to push the prefix over the threshold.

Worth being precise about what is *not* the problem. That prefix is byte-identical on every call, forever — same tool schema, same system string, across iterations and flows and users. Structurally this is a textbook caching candidate. **Size alone rules it out**, and no breakpoint placement fixes it.

**A secondary note, not independently disqualifying.** `_critique_response` runs exactly once per flow (phase 2 of `run_flow_with_reflection`), so within a single run nothing reads what that call would have written, and the 1.25× write premium would be pure loss. Across runs it differs — the cache lives 5 minutes and the prefix never changes, so two runs inside that window would hit. Break-even is roughly two calls: 1.25× + 0.1× = 1.35×, versus 2.0× uncached. `_check_completeness` is a stronger candidate still, running once per convergence round (0–2 times at `max_rounds=3`) and able to pay off inside a single flow.

That ranks the two judges against each other; it is not why either is uncached. **If the prefix were 2000 tokens instead of 180, both would get a breakpoint.**

Both now pass `system` as a plain string and call `log_usage(..., cached=False)`, so they report `—` rather than being counted as misplaced breakpoints.

**The general lesson:** caching is worth applying where a large prefix is re-sent many times. A small prefix, a single call, or both means the correct answer is no breakpoint at all — and recognizing that is as much a part of using the feature well as placing breakpoints is.

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

### What these diagrams leave out

They track `system + tools` only. The `messages` array is absent — and it is the part that **grows**: every assistant turn and every tool result is appended and re-sent on the next iteration. With no breakpoint on the conversation, all of it is billed at full price on every call, while the carefully-cached static prefix sits at 0.1×.

Measured on a critic-revise run (option 6, `usr_005`):

| Call | uncached `input` | cached `cache_read` | uncached share |
|---|---|---|---|
| phase 1, call 1 | 331 | 0 (wrote 2296) | — |
| phase 1, call 2 | 2225 | 2296 | 49% |
| phase 3 | 3802 | 2296 | **62%** |

By phase 3 the uncached conversation has overtaken the cached prefix. Overall cache hit rate for that run: **31%** of input tokens.

### The conversation breakpoint

The fix is a breakpoint on the last content block of the most recent turn, so each iteration reads the previous one's entry instead of re-paying for it. Implemented in `_cache_conversation()` and called at all six tool-result append sites.

Because `messages` **grows** — unlike `system` and `tools`, which are byte-identical every call — a breakpoint in a fixed position is worthless. It has to **move forward** to the end of the array before each request:

```python
if tool_results:
    msgs.append({"role": "user", "content": tool_results})
    _cache_conversation(msgs)          # advance the marker
```

Three things make this work:

**Move, don't accumulate.** The 4-breakpoint cap is already 50% spent (`system` + `_cache_tools`), so adding one per iteration returns `400 A maximum of 4 blocks with cache_control may be provided` on the third. `_cache_conversation` strips stale markers, keeping the two most recent — if the newest misses, the older entry still turns a total miss into a partial hit.

**Stripping a marker invalidates nothing.** `cache_control` declares *where to cut* the prefix; it is not part of the cached content, and the prefix match is on the content bytes. This is what makes a moving breakpoint legal.

**The minimum does not bite here.** The 1024-token bar applies to the entire prefix ahead of the breakpoint, and `system` (~2296 tokens) renders before `messages` — so a conversation breakpoint clears it from the moment it exists, even when the conversation is one short turn. This is the exact inverse of the judge callers, which have no large prefix to sit behind.

Only tool-result turns are marked. Assistant turns hold `response.content` — SDK objects rather than dicts — so there is no key to set; skipping them is free because a tool-result turn always follows one. Plain-string turns (the convergence follow-up, the critic's issues message) are skipped too, which is wanted: the marker stays on the last tool-result turn and the next phase's first call reads it.

Projected effect on the run measured above (read 0.1×, write 1.25×, uncached 1.0×):

| Call | Today | With the breakpoint | Δ |
|---|---|---|---|
| phase 1, call 1 | 2296 w + 331 | unchanged (opening turn is a string — no marker yet) | 0 |
| phase 1, call 2 | 2296 r + 2225 = **2455** | 2296 r + 2225 w = **3011** | **+556** |
| phase 3 | 2296 r + 3802 = **4032** | 4521 r + 1577 w = **2423** | **−1609** |

Net ≈ **1050 tokens saved**, about 17% of that run. Note call 2 is a genuine *loss* — it writes a prefix nothing has read yet. The win arrives at call 3 and compounds, which is the honest summary: this pays off on long loops and is roughly break-even on short ones.

> ⚠️ **The 20-block lookback window.** A breakpoint searches back at most **20 content blocks** to find a prior entry. A turn with many parallel tool calls produces ~2 blocks per call, so two iterations of a 5-call turn sits near that limit. The symptom is a `WRITE` in the usage log where `HIT` was expected — which is the second reason for keeping two markers rather than one.

**Measured — 2026-08-13, option 5 (convergence, `usr_005`):**

```
[USAGE] run_flow_until_complete_stream  in=333  cache_w=2296  cache_r=0     out=227  WRITE
[USAGE] run_flow_until_complete_stream  in=1    cache_w=2221  cache_r=2296  out=99   HIT
[USAGE] run_flow_until_complete_stream  in=1    cache_w=147   cache_r=4517  out=923  HIT
```

`in` collapses to 1 on calls 2–3 — the marker sits on the last block of the last message, so nothing is left unmarked ahead of it. `cache_r=4517` on call 3 is exactly `cache_w=2221 + cache_r=2296` from call 2: each call reads everything the previous call wrote, confirming the chain rather than just a hit/miss bit. Weighting by 0.1×/1.25×/1.0×: old cost 8253, new cost 6846 — **17% saved**, matching the projection.

**Measured — same day, option 6 (critic-revise, `usr_006`):**

```
[USAGE] run_flow_with_reflection/p1  in=331   cache_w=0     cache_r=2296  out=236   HIT
[USAGE] run_flow_with_reflection/p1  in=1     cache_w=2228  cache_r=2296  out=104   HIT
[USAGE] run_flow_with_reflection/p1  in=1     cache_w=152   cache_r=4524  out=750   HIT
[USAGE] _critique_response           in=1597  cache_w=0     cache_r=0     out=1024  —
```

Same chaining (`4524 = 2296 + 2228`). Two things worth flagging so they aren't mistaken for bugs on a future run:

- **p1 call 1 shows `HIT`, not `WRITE`.** The `system+tools` cache entry is keyed on content, not on which flow sent it — option 5 and option 6 load the identical skill set (`_base`, `lookup-user`, `user-risk-profile`), so option 6's first call matched the entry option 5's first call had written minutes earlier, inside the 5-minute TTL. The *conversation* entry is still a first-time `WRITE` on call 2, as expected, because no prior run has this exact `messages` array.
- **`_critique_response` reports `—`, not `DEAD`.** This is the earlier judge fix (removing the no-op breakpoint) confirmed live rather than just by inspection.

## Verification

Cache behaviour is **permanently instrumented** in `flows/usage.py`. This section previously described the same check as a one-time manual procedure, which is why nothing noticed when the judge breakpoints turned out to be no-ops: a check that runs once during implementation stops protecting you the moment anything downstream changes.

`log_usage()` runs at all nine request sites and prints one line per call:

```
[USAGE] _run_tool_loop  in=2222  cache_w=0  cache_r=2296  out=99  HIT
```

`in` is the **full-price** portion. Verdicts:

| Verdict | Meaning |
|---|---|
| `WRITE` | Prefix cached for the first time — expected once per distinct prefix |
| `HIT` | Prefix served from cache — what every subsequent call should show |
| `DEAD` | A breakpoint was sent and nothing was cached. Prefix is probably under the minimum, or something upstream is invalidating it |
| `—` | No breakpoint sent, by design (the judge callers) |

Set `LLM_LOG_USAGE=0` to silence. `usage_summary()` returns per-site totals and an overall hit rate.

**To exercise the paths:**

1. **Option 5** (convergence loop, `usr_005`) — the main agentic loop. Note the streaming variant breaks on `stop or round_num == max_rounds`, so a flow that converges in round 1 calls **no** judge at all; the blocking variant breaks only on `round_num == max_rounds` and will call it.
2. **Option 6** (critic-revise, `usr_005`) — the only flow that exercises a judge unconditionally, since `_critique_response` has no guard.
3. `python tests/test_flows.py` — all 8 tests should pass unchanged; caching is transparent to output, which is exactly why the tests cannot detect any of this.

The same numbers are readable off any response object directly (`response.usage.cache_read_input_tokens`, etc.) and work identically on both providers. On Bedrock they are also visible in the AWS console under Model invocations; there is no equivalent console view on the direct Anthropic API.
