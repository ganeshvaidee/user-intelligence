# How `run_flow_until_complete` works

## What it is

`run_flow_until_complete` is a **convergence loop**. It runs the normal agentic tool loop, then asks a second LLM call (the *completeness judge*) whether the response fully covered the request. If not, the missing items are fed back into the conversation and Claude runs another pass — using the same conversation thread and the same MCP server session — until either the judge is satisfied or `max_rounds` is reached.

---

## Function signature

```python
async def run_flow_until_complete(
    user_request: str,       # the question to answer
    skill_names:  list[str], # skills to load into the system prompt
    max_rounds:   int = 3,   # hard ceiling on iterations
    verbose:      bool = True,
) -> str:
```

---

## Step-by-step flow

```
┌─────────────────────────────────────────────────────┐
│  Load skills → build system prompt                  │
│  Open ONE MCP session (spans all rounds)            │
└────────────────────────┬────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   Round N           │
              │   _run_tool_loop()  │ ← Claude calls tools, writes answer
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  Completeness judge │ ← second LLM call (not Claude's answer)
              │  "Is this complete?"│
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  complete == true?  │
              │  OR round == max?   │──── YES ──→ return accumulated text
              └──────────┬──────────┘
                         │ NO
              ┌──────────▼──────────┐
              │  Append missing     │
              │  items to msgs      │──→ back to Round N+1
              └─────────────────────┘
```

---

## The two LLM calls per round

Each round involves **two separate model calls** — they serve completely different purposes:

| Call | Function | Role | Output |
|---|---|---|---|
| `_run_tool_loop()` | Main agent | Calls tools, writes answer | Free-text assessment |
| `_check_completeness()` | Judge | Reviews the answer | `{complete: bool, missing: [str]}` |

The judge uses `tool_choice={"type": "any"}` to force structured output — it cannot respond with free text. This makes the result safe to parse as a dict without any string parsing:

```python
result = await client.messages.create(
    tools       = [_COMPLETENESS_TOOL],
    tool_choice = {"type": "any"},        # forces a tool_use block
    ...
)
return next(b for b in result.content if b.type == "tool_use").input
# always returns: {"complete": bool, "missing": [...]}
```

---

## Conversation state across rounds

One critical design detail: **all rounds share the same `msgs` list and the same MCP session**. The conversation grows across rounds — Claude never loses context of what it already fetched:

```
After Round 1:
  msgs = [
    {user:      "Give me a thorough risk assessment for usr_005"},
    {assistant: [get_user tool call]},
    {user:      [get_user result]},
    {assistant: [get_user_activity tool call]},
    {user:      [activity result]},
    {assistant: [TextBlock: "## Risk Assessment — Eve Contractor..."]},
    # judge says: missing audit log check
    {user:      "Your response is incomplete. Please also check:\n- Audit log history"},
  ]

Round 2 starts from this msgs list — Claude already has all prior tool results.
It only fetches what it still needs (get_audit_log), not everything again.
```

---

## Walkthrough: `usr_005` (Eve Contractor)

**Request:**
```
"Give me a thorough risk assessment for usr_005"
```

**Skills loaded:** `_base`, `lookup-user`, `user-risk-profile`

**What Claude knows about usr_005 from the database:**
- Status: `active`, contractor, no MFA
- Permissions: `write-users`, `write-billing`, `admin-prod-db`, `read-secrets`, `access-admin-panel`, `deploy-prod` — 6 permissions, 5 high-risk
- Activity (30 days): 60 events, ~15 failures (25% failure rate), all from external IPs (`185.x.x.x` range), accessing `secrets`, `admin-panel`, `prod-database`
- Audit log: no prior admin actions

---

### Round 1

**`_run_tool_loop` — Claude's tool calls:**

```
Call 1 → Claude reads skill: "fetch user record first"
         → get_user("usr_005")
         ← {name: "Eve Contractor", status: "active", mfa_enabled: false,
            employee_type: "contractor", last_login: "2026-06-22T..."}

Call 2 → Claude reads skill: "fetch activity (30 days) and permissions in parallel"
         → get_user_activity("usr_005", days=30)
         → get_user_permissions("usr_005")
         ← activity: {total: 60, failures: 15, unique_ips: 12, events: [...]}
         ← permissions: {total: 6, high_risk_count: 5, high_risk: [...]}

Call 3 → Claude has enough to score, writes answer
         stop_reason = "end_turn"
```

**Claude's Round 1 output (approximate):**

```
## Risk Assessment — Eve Contractor (usr_005)

Risk Score: 12/15   Level: 🔴 Critical

| Dimension      | Score | Key Factors                              |
|----------------|-------|------------------------------------------|
| Authentication | 6/6   | No MFA, 25% failure rate, 12 unique IPs  |
| Permissions    | 5/5   | admin-prod-db, write-billing, deploy-prod|
| Behaviour      | 4/4   | Accessing secrets at odd hours           |
| Account        | 1/3   | Contractor type                          |

Recommended Action: Immediate deactivation
Evidence:
- No MFA on contractor account with admin-level database access
- 25% login failure rate from 12 different external IP addresses
- Active access to prod-database, secrets, and admin-panel
```

**Completeness judge call:**

```python
_check_completeness(
    original_request = "Give me a thorough risk assessment for usr_005",
    response         = "## Risk Assessment — Eve Contractor..."
)
```

Judge evaluates: *Did the assessment check the audit log? The skill says to fetch audit log history.*

```json
{
  "complete": false,
  "missing": [
    "Audit log — no prior admin actions were checked",
    "Account age — creation date not mentioned"
  ]
}
```

**Decision: not complete → continue to Round 2**

Appended to `msgs`:
```
"Your response is incomplete. Please also check:
- Audit log — no prior admin actions were checked
- Account age — creation date not mentioned"
```

---

### Round 2

Claude sees the follow-up in `msgs`. It already has the user record, activity, and permissions from Round 1 — no need to re-fetch.

**`_run_tool_loop` — Claude's tool calls:**

```
Call 1 → Claude reads the follow-up, decides it needs audit log and creation date
         → get_audit_log("usr_005")
         ← {user_id: "usr_005", entries: []}   (no prior admin actions)

Call 2 → Claude already has created_at from get_user in Round 1
         → no additional call needed, writes revised answer
         stop_reason = "end_turn"
```

**Claude's Round 2 output:**

```
## Risk Assessment — Eve Contractor (usr_005) [Updated]

Risk Score: 13/15   Level: 🔴 Critical

...same breakdown...

### Additional Findings
- Audit log: No prior administrative actions on record — this account
  has never been flagged or reviewed despite its risk profile
- Account age: Created ~180 days ago, contractor duration unclear

Recommended Action: Immediate deactivation
```

**Completeness judge call:**

```json
{
  "complete": true,
  "missing": []
}
```

**Decision: complete → break, return accumulated text**

---

### What gets returned

`all_text` is the **concatenation of every round's text output**:

```
[Round 1 assessment] + [Round 2 updated assessment]
```

Both are included because `all_text += round_text` accumulates across rounds. The caller receives the full progression, not just the final round.

---

## Hard ceiling: `max_rounds`

If the judge never returns `complete: true`, the loop exits after `max_rounds` (default: 3):

```python
if round_num == max_rounds:
    if verbose:
        print(f"\n[CONVERGENCE] Max rounds ({max_rounds}) reached.")
    break
```

The completeness check is skipped on the final round — the loop just exits. This prevents infinite loops if Claude consistently misses something the judge expects.

---

## When to use this vs `run_flow`

| | `run_flow` | `run_flow_until_complete` |
|---|---|---|
| LLM calls | 1 per tool round | 1 per tool round + 1 judge call per round |
| Cost | Lower | Higher |
| Use when | Task is well-defined, one pass is enough | Thoroughness matters and gaps are likely |
| Example | "Look up usr_005" | "Give me a **thorough** risk assessment" |
