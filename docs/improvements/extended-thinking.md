# Extended Thinking (Option 8)

> **Scope: Option 7 only.**
> Extended Thinking is enabled exclusively in `run_dimension_agent` (`flows/run_flow.py`), which is called only by `run_flow_parallel_risk` (option 7 — "Risk assessment (parallel agents + extended thinking)").
> Options 1–6 use standard model calls with no thinking parameter.

---

## Problem

The parallel dimension agents score risk correctly but implicitly. Claude reads data, applies scoring rules, and calls `report_dimension_score` — but the intermediate reasoning is invisible. On borderline cases (failure rate is 19.8%, threshold is 20%; account age is 31 days, threshold is 30) there's no way to verify the score without re-reading the raw data yourself.

## Solution

Adaptive thinking adds a `thinking` parameter to the model call. Claude reasons through the scoring conditions step by step *before* committing to a score. The reasoning is returned as a `ThinkingBlock` alongside the tool call — auditable, logged in verbose mode, and summarised in the report via the `reasoning` field.

---

## Where it lives

**One function:** `run_dimension_agent(dimension, user_id, verbose, thinking)` in `flows/run_flow.py`.

This function is called four times concurrently by `run_flow_parallel_risk` via `asyncio.gather`. Each call decides its own thinking depth independently.

---

## Implementation

### The model call

```python
response = await client.messages.create(
    model         = MODEL_ID,           # provider-dependent — see below
    max_tokens    = 10000,
    thinking      = {"type": "adaptive", "display": "summarized"},
    output_config = {"effort": "high"},
    system        = cached_system,
    tools         = cached_tools,
    messages      = messages,
)
```

Three parameters do the work:

| Parameter | Why |
|---|---|
| `thinking={"type": "adaptive"}` | Claude decides how much to think per request rather than spending a fixed budget. A cheap dimension like `account` (two fields) stops early; `behaviour` can reason longer when the data warrants it. |
| `display="summarized"` | **Required, not cosmetic.** The default is `"omitted"`, which returns thinking blocks whose `.thinking` field is an empty string. Without this the `[THINKING — …]` audit output below prints blank blocks — no error, just silently no reasoning to audit. |
| `output_config={"effort": "high"}` | Controls thinking depth and overall token spend. This is the successor to the old fixed-budget knob. |

`MODEL_ID` comes from `flows/llm_client.py`, which resolves it per provider — `us.anthropic.claude-sonnet-4-6` on Bedrock (an inference-profile ID, overridable with `BEDROCK_MODEL_ID`), `claude-sonnet-4-6` on the direct Anthropic API (overridable with `ANTHROPIC_MODEL_ID`). All three thinking parameters behave identically on both.

**No `temperature` here — and that's required, not an oversight.** Every other model call in this codebase passes `temperature=TEMPERATURE` (default `0`; see `docs/improvements/temperature-determinism.md`), but the API rejects any value other than `1` when `thinking` is enabled. `run_dimension_agent` builds its `create_kwargs` conditionally: `temperature=TEMPERATURE` is only added in the `else` branch, when `thinking=False`. Setting `thinking=True` and a non-default temperature together is a 400 error, not a silent override.

### Why not `budget_tokens`

This code previously used the fixed-budget form:

```python
thinking = {"type": "enabled", "budget_tokens": 8000}   # no longer used
```

That form is **deprecated on Sonnet 4.6 and rejected with a 400 on newer models** (Opus 4.7 and later, Sonnet 5, Fable 5). Since `BEDROCK_MODEL_ID` / `ANTHROPIC_MODEL_ID` are environment-overridable, pointing this project at a newer model would have failed all four dimension agents at once. Adaptive thinking plus `effort` is the supported replacement and works across every current model.

The tradeoff: there is no longer a hard per-request ceiling on thinking tokens. `max_tokens=10000` remains the enforced cap on the response as a whole (thinking plus output), and `effort` is the tuning knob — drop it to `"medium"` or `"low"` if the four concurrent agents cost more than you want.

### Handling the ThinkingBlock

`ThinkingBlock`s appear in `response.content` before text or tool_use blocks. They are:
- **Logged** when `verbose=True`
- **Included** in `messages.append({"role": "assistant", "content": response.content})` — required by the API for multi-turn correctness
- **Skipped** for tool dispatch — only `tool_use` blocks are routed to the MCP server

```python
for block in response.content:
    if block.type == "thinking":
        if verbose:
            print(f"\n[THINKING — {dimension.upper()}]\n{block.thinking}\n")

    elif block.type == "tool_use":
        if block.name == "report_dimension_score":
            score_result = block.input   # captures the structured score
        else:
            result = await execute_tool(session, block.name, block.input)
```

### The `reasoning` field

`_DIMENSION_SCORE_TOOL` has a required `reasoning` field:

```python
"reasoning": {
    "type": "string",
    "description": "One-sentence summary of the key factor that drove this score"
}
"required": ["score", "max_score", "factors", "evidence", "reasoning"]
```

Claude fills this after thinking — it's a concise version of the thinking block that surfaces in the synthesized report without forwarding the full thinking content. Because `reasoning` is **required**, the Agent Reasoning section always appears in option 7 output, confirming extended thinking ran.

---

## Skill guidance

Each `skills/risk-{dimension}/SKILL.md` has a "How to use your thinking" section that directs Claude to evaluate each condition with exact numbers before reporting:

**auth:**
```
Is MFA enabled? If not → +2
How many failed logins in 30 days? Is it > 10? → +2 if yes
How many unique IPs in 7 days? Is it > 5? → +2 if yes
When was the last login? Is it more than 90 days ago? → +1 if yes
Add up the points and verify the total is correct before reporting.
```

This steers the thinking toward systematic condition-checking rather than narrative prose. Without this guidance, Claude might spend tokens explaining context rather than verifying thresholds.

---

## What the output looks like

### All-in-one CLI (verbose=True)

Thinking blocks print to the terminal before each agent's score:

```
[THINKING — AUTH]
Checking get_user: mfa_enabled = false → +2.
get_user_activity(days=30): failures=15, total=60. 15 > 10 threshold → +2.
get_user_activity(days=7): unique_ips=8. 8 > 5 → +2.
Last login: 2 days ago. Not dormant → +0.
Total: 6/6.

[THINKING — PERMISSIONS]
get_user: employee_type = contractor.
get_user_permissions: admin-prod-db (admin) → +2.
write-billing (write, sensitive) → +1. write-users (write, sensitive) → +1.
deploy-prod (write, prod-infra — not in sensitive list) → +0.
Contractor with high-risk perms → +2.
Total: 2+2+2 = 6. Cap at 5 (max). Score: 5/5.
...
```

### Three-service mode (client → orchestrator)

Thinking blocks are not forwarded over SSE. The report always includes an Agent Reasoning section:

```
## Risk Assessment — usr_005 (Parallel Agents + Extended Thinking)

Risk Score: 13/15   Level: 🔴 Critical

### Score Breakdown
| Dimension      | Score | Key Factors                                    |
|----------------|-------|------------------------------------------------|
| Authentication | 6/6   | MFA disabled (+2), >10 failed logins (+2), ... |
| Permissions    | 5/5   | Admin perms (+2), Write to billing (+1), ...   |
| Behaviour      | 4/4   | Failure rate >20% (+2), Sensitive access (+1), |
| Account        | 1/3   | Contractor type (+1)                           |

### Recommended Action
Immediate deactivation recommended

### Agent Reasoning
- **Authentication:** No MFA on a contractor account with 15 failed logins from 8 external IPs
- **Permissions:** Admin DB access + write to billing combined with contractor status
- **Behaviour:** 25% failure rate exceeds 20% threshold; accessed secrets and admin-panel
- **Account:** Contractor type only — not flagged, not new
```

The Agent Reasoning section is proof extended thinking ran — it is populated exclusively from the `reasoning` field that each agent writes after thinking.

---

## Options comparison

| Option | Flow | Extended Thinking |
|---|---|---|
| 1 | Lookup | ❌ |
| 2 | Single-shot risk | ❌ |
| 3 | Offboarding | ❌ |
| 4 | Find by email + risk | ❌ |
| 5 | Convergence loop | ❌ |
| 6 | Critic-revise | ❌ |
| **7** | **Parallel agents** | **✅ per dimension agent** |

---

## Planned improvements

### Forward thinking to the client via SSE

Add a `thinking` event type to the SSE stream so the client can display thinking blocks in real time:

```
data: {"thinking_start": "auth"}\n\n
data: {"thinking": "MFA disabled → +2. Failed logins: 15 > 10 → +2..."}\n\n
data: {"thinking_end": "auth"}\n\n
data: {"text": "## Risk Assessment..."}\n\n
```

### Tune `effort` per dimension

All four agents currently run at `effort: "high"`. The permissions dimension (multiple conditions, write-permission counting with a cap) plausibly needs more depth than the account dimension (three simple conditions). Passing a per-dimension effort level would cut cost on the cheap dimensions without sacrificing accuracy on the expensive ones — measure first, since adaptive thinking already scales depth to the task and the difference may be smaller than it looks.

### Extend to convergence and reflection flows

Options 5 and 6 use a single agent for all four dimensions. Adding thinking there would make the full risk assessment auditable. Adaptive thinking should handle the larger scope on its own — the single agent reasons about all four dimensions, so it will simply think longer — but `max_tokens` would need raising above the current 4096 to leave room for both the reasoning and the response.
