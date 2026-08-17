# Multi-Provider Support (Anthropic first)

## What it is

The flows can now run against four providers, selected with `LLM_PROVIDER`:

| `LLM_PROVIDER` | What it is | Client |
|---|---|---|
| `anthropic` (default) | Direct Anthropic API | `flows/anthropic_client.py` |
| `bedrock` | Claude via AWS Bedrock | `flows/bedrock_client.py` |
| `local` | Open-weight model on LM Studio/vLLM/SGLang | `flows/openai_compat_client.py` |
| `openai` | Hosted OpenAI API | `flows/openai_compat_client.py` |

The local target is **Muse Glimmer 30B** — Meta's Apache-2.0 agentic model, distilled from Muse Spark, with a 131K context, native tool calling, and a reasoning-strength control. Everything below was verified against it running on **LM Studio** (4-bit, 18.16 GB, Apple Silicon), which is the practical local stack on a Mac — vLLM has no usable Apple GPU backend.

```bash
pip install -r flows/requirements-local.txt
lms server start                    # LM Studio, port 1234
export LLM_PROVIDER=local
export LOCAL_BASE_URL=http://127.0.0.1:1234/v1
export LOCAL_MODEL_ID=meta/muse-glimmer
```

On Linux/NVIDIA the same adapter serves vLLM or SGLang unchanged — only `LOCAL_BASE_URL` and `LOCAL_MODEL_ID` differ:

```bash
vllm serve meta-models/Muse-Glimmer-30B --enable-auto-tool-choice --reasoning-parser ...
```

## How to test against it

Four steps, cheapest first. Do not skip to the last one — a flow run costs minutes, and the first three catch everything that is not model behaviour.

**1. Hermetic — no server, no credentials, ~2s.** Translation and the provider rules only:

```bash
python tests/test_openai_compat.py
python tests/test_provider_isolation.py
```

**2. Wiring check — server up, ~60s.** Reachability, tool calling, forced tool choice, reasoning traces, adapter refusals. Every failure names the specific fix:

```bash
LLM_PROVIDER=local python scripts/local_smoke.py
```

Its `max_tokens=400` for a one-word answer is deliberate. This model reasons before every reply, billed against the same budget; at `max_tokens=64` it spends the lot thinking and returns empty content with `finish_reason="length"` — which reads as a broken server and is not one.

**3. One real flow, ~4 min.** Two topologies, and only one of them needs the MCP server started by hand.

*Standalone* — `run_flow.py` spawns its own MCP server over stdio, so this is the only process:

```bash
./scripts/local.sh          # flow menu, option 1 is cheapest
```

*Service* — MCP server + orchestrator + client, three terminals:

```bash
python mcp-server/server.py --transport http --port 8001   # 1: unchanged
./scripts/local.sh serve                                   # 2: orchestrator on :8000
python client/cli.py                                       # 3: unchanged
```

**The orchestrator is the process that calls the model**, so it is the only one that needs `LLM_PROVIDER`. Exporting it in the MCP server's shell or the client's does nothing — the MCP server only executes tools, and the client only speaks HTTP to the orchestrator. `local.sh serve` sets it, plus `MCP_URL`, on the right process.

The raw equivalent of that middle command:

```bash
LLM_PROVIDER=local LOCAL_BASE_URL=http://127.0.0.1:1234/v1 \
  LOCAL_MODEL_ID=meta/muse-glimmer MCP_URL=http://localhost:8001 \
  python orchestrator/app.py --port 8000
```

Or the menu directly, without the script:

```bash
LLM_PROVIDER=local LOCAL_BASE_URL=http://127.0.0.1:1234/v1 \
  LOCAL_MODEL_ID=meta/muse-glimmer python flows/run_flow.py
```

**4. The eval suite.** It reads the same `llm_client`, so it needs no changes to target the local model — but it is not yet provider-parameterized, so its assertions were written against Claude and some will legitimately fail here:

```bash
LLM_PROVIDER=local ... python tests/test_flows.py --mode single
```

Re-seed after anything that writes: `python seed/seed.py`.

## Problem

The obvious way to support a second vendor is a provider abstraction: define the intersection of what everyone can do, express the flows in terms of that, and branch where they differ. That is the wrong trade here, and it is worth being explicit about why.

This repo exists to show how to write code *against Claude* — `cache_control` breakpoint placement that actually caches (`docs/improvements/prompt-caching.md`), adaptive thinking with `display="summarized"` (`docs/improvements/extended-thinking.md`), `output_config` effort, forced `tool_choice` for structured judges (`docs/improvements/llm-as-judge.md`), streaming, MCP. An intersection abstraction deletes every one of those from the readable path. The Anthropic code stops being the thing you can point at and becomes a special case buried under a wrapper.

So the requirement was: add providers **without** the Anthropic path getting one line blander.

## Solution

Three rules, each enforced by something other than good intentions.

### 1. The adapter adapts; the flows do not

`run_flow.py` and `tools.py` are unconditionally Anthropic-shaped. They send `cache_control`, `thinking`, `output_config`, `tool_choice={"type": "any"}` and read `block.type` / `response.stop_reason` / `response.usage.cache_read_input_tokens`, with no knowledge of what is underneath.

`flows/openai_compat_client.py` emulates that surface over `/v1/chat/completions`. It returns hand-rolled `Message` / `TextBlock` / `ToolUseBlock` / `Usage` objects that are duck-compatible with the SDK's — deliberately not `anthropic.types.*`, so the adapter is not coupled to that package's pydantic required-field churn.

The interesting translations:

| Anthropic | OpenAI-compatible | Note |
|---|---|---|
| `output_config={"effort": "high"}` | `Reasoning strength: high` appended to the system prompt | Same `low/medium/high/xhigh` vocabulary — a rename, not an approximation. Measured: 150 reasoning tokens at `low` vs 631 at `xhigh` on an identical prompt |
| `tool_choice={"type": "any"}` | `tool_choice="required"` | Enforced, not a hint — verified by asking a question that needed no tool and being handed a tool call anyway. This is what keeps the judges structured |
| `thinking={"type": "adaptive"}` | nothing on the wire | The model reasons natively; the trace returns in `reasoning_content` and becomes a `ThinkingBlock`, so `[THINKING — AUTH]` audit output works unchanged |
| `cache_control` breakpoints | dropped | vLLM prefix-caches automatically; there is no breakpoint to place |
| `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` | subtracted out of `input_tokens`, since Anthropic excludes cache reads and `usage.py` would otherwise double-count |
| one user turn of `tool_result` blocks | one `role: "tool"` message each | order preserved, so each still follows the assistant turn that requested it |
| `finish_reason: "stop"` | `stop_reason: "end_turn"` | `_run_tool_loop` exits on this and nothing else |

### 2. Incapable is fine. Silently incapable is not

`UnsupportedFeature` is raised for anything the provider cannot honour — `thinking`, an unknown effort level, or **any unrecognised parameter at all**. That last one matters most: the next Anthropic feature added to a flow must be either translated or refused. It must never reach the wire quietly discarded, because a dropped parameter produces a response that looks completely normal.

The one genuine exception is thinking blocks, where a refusal would be worse than a degradation — the score is still valid, only the audit trail is missing. `llm_client.supports("thinking_blocks")` gates it in `run_dimension_agent`, and the degradation is announced rather than inferred.

On LM Studio that gate is never taken, because traces *are* available: `reasoning_content` comes back and the adapter maps it to a `ThinkingBlock`. The gate exists for backends that do not send it — a plain llama.cpp server, or vLLM started without a `--reasoning-parser`. Since that field is a de-facto convention rather than part of the OpenAI schema, the adapter also warns once at runtime if thinking was requested and no trace arrived, which is the only silent no-op left in the design.

This is the *only* capability check in the flows, and it should stay that way. `_CAPABILITIES` in `llm_client.py` is a declaration, not a negotiation layer. The moment flows start branching per capability, the Anthropic path stops being the straightforwardly readable one.

### 3. The decay modes are tested, not documented

Two commits would quietly undo all of this, and both look reasonable in review: a second `import openai`, and an `if LLM_PROVIDER ==` outside the client modules. `tests/test_provider_isolation.py` fails on either, plus asserts that every resolved provider has a capability entry and that `anthropic` remains a superset of all of them — so a capability can never be dropped from the anthropic entry to make another provider's life easier.

`tests/test_openai_compat.py` covers the translations above. Both are hermetic, and the second skips cleanly when `openai` is not installed, since the default path must never require it.

## Effect on `usage.py`

`log_usage` now forces `cached=False` when the provider lacks `prompt_caching`. The flows send `cache_control` markers unconditionally — that is by design — but on vLLM those markers are dropped in translation, so without this every call would be reported `DEAD` and the column would stop meaning "misplaced breakpoint". Real prefix-cache hits still register as `HIT` through the `cached_tokens` mapping. Measured against a local run:

```
[USAGE] _run_tool_loop      in=452  cache_w=0  cache_r=2048  out=40  HIT
cache hit rate: 81.9% of input tokens served from cache
```

`cache_w` stays 0 permanently on this provider: there is no breakpoint to write and no write counter to read.

## What the first real run found: the duplicate loop

The adapter worked on the first try. The *flow* did not, and the reason is worth recording because it is a property of weak models in agentic loops generally, not of this model or this adapter.

Running `run_flow("Look up user usr_001…", ["_base", "lookup-user"])` against Muse Glimmer, the model:

1. called `get_user`, then `get_user_activity` + `get_user_permissions` in parallel — all correct
2. noticed that `lookup-user` asks for `get_user_activity(user_id, days=7)` while the result said `days: 30`
3. decided in its reasoning to "call again with days=7 to be precise"
4. re-issued the call **without the `days` argument**, got `days: 30` again, and repeated step 2

It looped until the `MAX_TOOL_ITERATIONS` circuit breaker and returned an empty answer. This was not an inability to emit the argument: asked directly, the model produces `{"user_id": "usr_001", "days": 7}` every time. The failure is holding an intention across a turn boundary under a long prompt — precisely the long-horizon consistency that a 4-bit 30B gives up relative to Claude.

**It is not a memory or context failure**, and that is the natural wrong inference. Tool results are not something the model has to retain: `_run_tool_loop` re-sends the entire conversation on every iteration, so the `days: 30` payload and the `tool_use` block that requested it sit side by side in the input on the turn where the model asks for it again. Nothing was forgotten. What failed is binding the result to the request that asked for it.

**Nor is it parameter count, and "open-weight" is a license, not a capability.** Discharging a sub-goal after a `tool_result` is installed by post-training on multi-turn tool-use trajectories, not by pretraining or by scale — a smaller model trained hard on agentic loops will do this better than a larger one that was not. Muse Glimmer is distilled from Muse Spark, and long-horizon agentic control is among the first things distillation gives up. Read the comparison with Claude that way; reading it as a size threshold predicts the wrong fix.

`_dispatch_tool_use` already detected the duplicates. It printed a warning and dispatched anyway, so the model received the identical bytes that had just failed to satisfy it. Two changes fixed it:

- An exact-duplicate **read** now returns a corrective error instead of re-dispatching, in the same style as the order guard. Writes are exempt — the state guards in `database.py` are the authority there — and a successful write clears the cached reads, since re-reading after a state change is a real question.
- That correction alone was **not enough**: the model read it, agreed with it in its reasoning, and made the identical call eight more times. So the guard escalates. On the third identical call it stops explaining and instructs: *stop calling tools, write the final answer now, mark anything you could not obtain as unavailable.*

**Why the escalation worked when the correction did not** — worth understanding before adding any further guard, because it is not what it looks like. A degenerate loop is self-reinforcing: once the first repeat is in the conversation, the context contains a demonstration that at this point the assistant calls `get_user_activity` with these arguments. Generation is pattern completion, so each repeat is further evidence for repeating, and the loop tightens on itself. A courteous error dict leaves that surface pattern intact — which is exactly how the model could read it, agree with it, and complete the pattern anyway. The escalated message works because it disrupts the pattern, not because it argues the point better.

The design lever is therefore **salience, not clarity**. The instinct on the next loop will be to write a more detailed explanation; that is precisely what already failed here.

With the escalation the same flow converges in 9 tool calls / 6 model turns, and the model handles the gap honestly:

> **Recent Activity (7 days):** … *Note: `get_user_activity` was called with default 30 days. Exact 7-day counts are unavailable from the returned data.*

Both changes are provider-neutral and dormant on the Anthropic path, which does not repeat an identical call three times.

## Measured on Muse Glimmer 30B (LM Studio, Apple Silicon, 4-bit)

| Flow | Result |
|---|---|
| `run_flow` lookup, `_base` + `lookup-user` | converges, ~255s, 9 tool calls, correct profile table |
| `run_dimension_agent("auth", "usr_005", thinking=True)` | converges, ~143s, `score 4/6` with evidence, 3 thinking blocks printed |
| Tool calling | correct names and arguments, including parallel calls in one turn |
| `tool_choice="required"` | enforced |
| Reasoning traces | returned via `reasoning_content` |

Expect minutes, not seconds: a lookup that Claude finishes in a few seconds takes ~4 minutes here. Fine for eval runs, not for anything interactive.

## What is still unverified

- **Order-guard recovery.** `ORDER_REQUIREMENTS` returns `{"error": "Cannot call flag_user before get_user_activity..."}` and relies on the model reading that, calling the prerequisite, and retrying. The duplicate loop suggests error-driven recovery is this model's weak spot, so the offboard flow needs its own run before being trusted.
- **Quantized arithmetic.** The 0–15 total is summed across four dimension agents. The single dimension tested scored sensibly, but 4-bit weights drift on arithmetic in ways that are easy to miss when the prose around the number reads fine.
- **`tests/test_flows.py` is not parameterized by provider.** That is where both belong.

## Files

| File | Role |
|---|---|
| `flows/llm_client.py` | provider toggle, `_CAPABILITIES`, `supports()` |
| `flows/openai_compat_client.py` | the entire adapter; the only file allowed to `import openai` |
| `flows/usage.py` | capability-aware `DEAD` verdict |
| `flows/run_flow.py` | one `supports("thinking_blocks")` gate in `run_dimension_agent` |
| `flows/requirements-local.txt` | optional `openai` extra |
| `tests/test_provider_isolation.py` | guards the rules above |
| `tests/test_openai_compat.py` | guards the translations |
