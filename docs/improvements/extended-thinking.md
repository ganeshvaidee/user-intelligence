# Extended Thinking (Options 8 and 9)

> **Scope: options 8 and 9.**
> Extended Thinking is enabled exclusively in `run_dimension_agent` (`flows/run_flow.py`). That function is called by two flows:
> - `run_flow_parallel_risk` — option **7** passes `thinking=False`, option **8** passes `thinking=True`
> - `run_flow_parallel_risk_with_memory` — option **9**, which hardcodes `thinking=True`
>
> So option 7 runs the same four parallel agents with thinking off, so comparing **7 with 8** shows what thinking alone changes. Options 1–7 otherwise use standard model calls with no thinking parameter.

---

## What it is

Extended thinking spends extra compute at inference time. Before writing the response the model generates reasoning tokens — a working area that is not the answer and is not addressed to the reader. Only then does it produce the user-facing output.

Without it, the model emits the answer token by token from the first forward pass onward. Every word is committed as it is generated; there is no place to work something out and then decide not to use it. Extended thinking supplies that place, and the budget buys four things:

- **Decomposition** — split a rule with several conditions into one check at a time, instead of evaluating the whole thing in one pass.
- **Hypothesis and self-critique** — propose a value, then test it against the rule that asked for it.
- **Backtracking** — reach for the wrong number, notice the mismatch, and discard it. This is the one that cannot be simulated by a more detailed prompt, because it requires having already written the wrong thing down.
- **Planning before committing** — sequence the steps, then execute, rather than deciding the shape of the answer while emitting it.

In this codebase `display` is set to `"summarized"`, so that reasoning comes back in the response and is printed — see [Implementation](#implementation). It is a working area, not a hidden one.

## Problem

The parallel dimension agents score risk correctly but implicitly. Claude reads data, applies scoring rules, and calls `report_dimension_score` — but the intermediate reasoning is invisible, and the output is a single integer with no way to check it short of re-reading the raw data yourself.

The specific failure this guards against is **window conflation**. Two of the four auth rules are scoped to different time windows, so the agent calls `get_user_activity` twice and holds two payloads with identical field names, distinguished only by the `days` argument sent in an earlier message. Reading a field from the wrong one is a silent two-point error. Worked through on real data below.

## Solution

Adaptive thinking adds a `thinking` parameter to the model call. Claude reasons through the scoring conditions step by step *before* committing to a score. The reasoning is returned as a `ThinkingBlock` alongside the tool call — auditable, logged in verbose mode, and summarised in the report via the `reasoning` field.

---

## When to use it

**Use extended thinking when working out the answer takes many steps, but the answer itself is short and cannot be revised.**

That is the shape it pays for. A dimension agent emits `score: 4` — one number — but reaching it means checking four conditions against exact values and summing them: a long path to a short answer. Without a scratchpad the model must produce that integer in a single forward pass.

| Turn it on when | Leave it off when |
|---|---|
| The output is a **commitment** — a score, a classification, a go/no-go | The work is retrieval and formatting (option 1 — the skill's rules fully determine the output) |
| Working it out needs **exact comparisons** or must keep several similar values straight | The tool sequence is fixed and the mapping is mechanical |
| A wrong intermediate step needs to be **recoverable** — reached for, recognised, discarded | Every intermediate step is forced by the data with no room to go wrong |
| You need to **defend** the answer, not just produce it — the trace is evidence | You need **reproducibility** (see the temperature constraint below) |
| The call is **one-shot** — no judge or critic downstream to catch it | A cheaper mechanism already covers you — a completeness judge (option 5) or critic (option 6) catches omissions after the fact for far fewer tokens |

### The mechanism, on real data

`usr_005` (Eve Contractor) in the seed database, scored against the four rules in `skills/risk-auth/SKILL.md`. Two of them are window-scoped, which is where the trouble is:

```
rule: MFA disabled                  → +2
rule: >10 failed logins in 30 days  → +2      ← 30-day window
rule: >5  unique IPs  in  7 days    → +2      ←  7-day window
rule: no login in 90+ days          → +1
```

The three tool results the agent has in context:

```
get_user(usr_005)                   → mfa_enabled: 0, employee_type: contractor,
                                      last_login: 18 days ago, created_at: 74 days ago
get_user_activity(usr_005, days=30) → {total: 60, failures: 15, unique_ips: 60}
get_user_activity(usr_005, days=7)  → {total:  0, failures:  0, unique_ips:  0}
```

**Without extended thinking — 6/6, incorrect:**

```
MFA disabled                → +2
15 failed logins > 10       → +2
60 unique IPs > 5           → +2      ← read from the 30-day payload
Last login 18 days ago      → +0
Total: 6/6
```

**With extended thinking — 4/6, correct.** The reasoning binds each value to its window before summing, and one of those steps is a retraction:

```
mfa_enabled = 0 → +2. Running total 2.
Rule 2 is scoped to 30 days. 30-day failures = 15. 15 > 10 → +2. Running total 4.
Rule 3 is scoped to 7 days, not 30. I have two payloads — take the days=7 one.
  7-day unique_ips = 0. 0 > 5 is false → +0. Running total 4.
  (The 30-day figure is 60, which would score. Wrong window. Discard it.)
Dormancy: last_login 18 days ago against a 90-day threshold → +0.
Total 4/6.
```

**Why the first one failed.** Not confusing `failures` with `unique_ips` — those are distinct field names. Confusing **which of two identically-shaped payloads** a field came from. The only thing separating the two is the `days` argument sent in an earlier message, and the wrong value is the persuasive one: 60 unique IPs across 60 events means every request came from a different address, exactly the pattern the rule exists to catch. Two points too high on a six-point dimension, and it propagates to the headline — 16/15 instead of 14/15.

The line in parentheses is the part a better prompt cannot replace. The model reaches for 60, recognises the window mismatch, and drops it. Doing that requires having written the wrong number down first, which is precisely what a single forward pass has nowhere to do. Not more intelligence — somewhere to write the intermediate step down, and permission to cross it out.

Note that this, rather than a near-threshold comparison, is the case that actually bites in this dataset. 15/60 = 25% against a 20% threshold is not close; keeping two windows straight across four rules is.

#### The example only works on an aged database

`seed_activity` places all 60 of Eve's events within `random.randint(1, 168)` hours — the whole history is inside 7 days at the moment of seeding, and ages out of that window afterwards:

| Database state | `days=30`<br>`total`/`failures`/`unique_ips` | `days=7`<br>`total`/`failures`/`unique_ips` | Correct auth score |
|---|---|---|---|
| Immediately after `python seed/seed.py` | 60 / 15 / 60 | **59 / 15 / 59** | 6/6 |
| After ~18 days | 60 / 15 / 60 | **0 / 0 / 0** | 4/6 |

On a fresh seed the two payloads are nearly identical, both windows clear the >5 threshold, and the conflation is invisible — correct and incorrect reasoning both land on 6/6. Reproducing the discrimination above needs a database that has had time to age past the 7-day boundary.

### What it costs

Accuracy at the moment the model commits to an answer, paid in output tokens, latency, and determinism — `temperature` is forced to `1` (see below). If a flow never has to commit to a short, final answer, all three are spent for nothing.

**Compare options 7 and 8 to isolate thinking** — identical four-agent architecture, `thinking=False` vs `True`. Comparing 5 with 8 confounds thinking with parallel decomposition and with judge-vs-no-judge.

---

## Where it lives

**One function:** `run_dimension_agent(dimension, user_id, verbose, thinking)` in `flows/run_flow.py`.

This function is called four times concurrently via `asyncio.gather` — by `run_flow_parallel_risk` (options 7 and 8) and by `run_flow_parallel_risk_with_memory` (option 9). Each call decides its own thinking depth independently.

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

`MODEL_ID` comes from `flows/llm_client.py`, which resolves it per provider — `us.anthropic.claude-sonnet-4-6` on Bedrock (an inference-profile ID, overridable with `BEDROCK_MODEL_ID`), `claude-sonnet-4-6` on the direct Anthropic API (overridable with `ANTHROPIC_MODEL_ID`). All three thinking parameters behave identically on those two.

On the `local` provider they do not, and `run_dimension_agent` gates on `llm_client.supports("thinking_blocks")` because of it. `thinking` sends nothing on the wire — the model reasons unprompted and returns the trace in `reasoning_content`, which the adapter maps back to a `ThinkingBlock`; `effort` becomes a `Reasoning strength: high` line in the system prompt; and the `temperature=1` constraint below does not apply, since that belongs to Anthropic extended thinking specifically. See `docs/improvements/multi-provider.md`.

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

Claude fills this after thinking — it's a concise version of the thinking block that surfaces in the synthesized report without forwarding the full thinking content. Because `reasoning` is **required**, the Agent Reasoning section appears in options 7, 8 and 9 alike. Note what that means: the section's *presence* does not confirm thinking ran, since option 7 fills the same field without it. Only the `[THINKING — …]` blocks in verbose output do.

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
Checking get_user: mfa_enabled = 0 → +2.
get_user_activity(days=30): failures=15, total=60. 15 > 10 threshold → +2.
get_user_activity(days=7): unique_ips=0. Not > 5 → +0.
  (30-day unique_ips is 60, but rule 3 is scoped to 7 days. Wrong window.)
Last login: 18 days ago against a 90-day threshold. Not dormant → +0.
Total: 4/6.

[THINKING — PERMISSIONS]
get_user: employee_type = contractor.
get_user_permissions: admin-prod-db (admin), access-admin-panel (admin) → +2.
write-users (write, user-data — sensitive) → +1.
write-billing (write, billing — sensitive) → +1.
read-secrets (secrets is sensitive, but level is read) → +0.
deploy-prod (write, prod-infra — not in the sensitive list) → +0.
Contractor with high-risk perms → +2.
Total: 2+2+2 = 6. Cap at 5 (max). Score: 5/5.
...
```

The permissions trace shows the same shape without the retraction: two of Eve's six permissions look like they should score and do not — `read-secrets` because the resource is sensitive but the level is `read`, `deploy-prod` because the level is `write` but `prod-infra` is not in the sensitive list. Both require holding resource and level together rather than matching on either alone.

### Three-service mode (client → orchestrator)

Thinking is forwarded over SSE as `{"thinking": "<delta>", "dimension": "<name>"}` events, which `client/cli.py` renders dimmed to **stderr** so it stays separable from the report on stdout — see `docs/improvements/streaming.md`. The report itself always includes an Agent Reasoning section:

```
## Risk Assessment — usr_005 (Parallel Agents + Extended Thinking)

Risk Score: 14/15   Level: 🔴 Critical

### Score Breakdown
| Dimension      | Score | Key Factors                                        |
|----------------|-------|----------------------------------------------------|
| Authentication | 4/6   | MFA disabled (+2), >10 failed logins (+2)           |
| Permissions    | 5/5   | Admin perms (+2), Write to sensitive (+2), ...      |
| Behaviour      | 4/4   | Failure rate >20% (+2), Sensitive access (+1), ...  |
| Account        | 1/3   | Contractor type (+1)                               |

### Recommended Action
Immediate deactivation recommended

### Agent Reasoning
- **Authentication:** No MFA on a contractor account with 15 failed logins in 30 days; no activity at all in the last 7 days, so the unique-IP rule does not fire
- **Permissions:** Admin DB and admin-panel access plus write to user-data and billing, combined with contractor status — 6 points capped at 5
- **Behaviour:** 15 failures out of 60 events = 25%, above the 20% threshold; accessed secrets, prod-database and admin-panel; events outside 08:00–18:00
- **Account:** Contractor type only — not flagged, and 74 days old so not new
```

Those are the scores for the current aged database — 4 + 5 + 4 + 1 = 14/15. The window-conflation error described above would report Authentication as 6/6 and a headline of 16/15, which exceeds the maximum and is the cheapest signal that something went wrong.

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
| 7 | Parallel agents | ❌ — `thinking=False` |
| **8** | **Parallel agents + extended thinking** | **✅ per dimension agent** |
| **9** | **Parallel + extended thinking + memory** | **✅ per dimension agent** |

---

## Planned improvements

### Tune `effort` per dimension

All four agents currently run at `effort: "high"`. The permissions dimension (multiple conditions, write-permission counting with a cap) plausibly needs more depth than the account dimension (three simple conditions). Passing a per-dimension effort level would cut cost on the cheap dimensions without sacrificing accuracy on the expensive ones — measure first, since adaptive thinking already scales depth to the task and the difference may be smaller than it looks.

### Extend to convergence and reflection flows

Options 5 and 6 use a single agent for all four dimensions. Adding thinking there would make the full risk assessment auditable. Adaptive thinking should handle the larger scope on its own — the single agent reasons about all four dimensions, so it will simply think longer — but `max_tokens` would need raising above the current 4096 to leave room for both the reasoning and the response.
