# Memory / Persistence (Option 9)

> **Scope: Option 9 only** (`run_flow_parallel_risk_with_memory`).
> Options 1–8 do not persist or retrieve prior assessments.

---

## Problem

Every risk assessment previously started cold. Claude had no knowledge of whether it had assessed the same user before, whether scores were trending up or down, or whether a prior security incident had been recorded. Each run produced a point-in-time snapshot with no baseline for comparison.

## Solution

Add a persistence layer: save each completed assessment to a new `assessments` table in the existing SQLite database. On the next run, fetch the most recent prior assessment before launching the parallel agents. Pure Python computes the delta and appends a comparison section to the report.

**Design choice:** memory fetch and save are Python-driven via MCP calls — not instructions to Claude. The four dimension agents score independently and don't know about the prior assessment. Only the synthesis step uses it, purely for arithmetic comparison.

---

## Architecture

```
run_flow_parallel_risk_with_memory(user_id)
    │
    ├── 1. MCP session → get_prior_assessment(user_id) → close
    │      returns: prior assessment dict  OR  {"none": true}
    │
    ├── 2. asyncio.gather(4 parallel agents — unchanged from option 8)
    │      each agent: own session, extended thinking, report_dimension_score
    │
    ├── 3. _synthesize_risk_report(..., prior=prior)
    │      pure Python comparison: delta per dimension + overall trend
    │
    └── 4. MCP session → save_assessment(user_id, scores, level, summary) → close
```

Steps 1 and 4 each open and close a dedicated MCP session — they are not shared with the agent sessions.

---

## Database

### New table — `assessments`

Added to `seed/seed.py` with `IF NOT EXISTS` (existing databases do not need re-seeding):

```sql
CREATE TABLE IF NOT EXISTS assessments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    assessed_at TEXT NOT NULL,        -- datetime('now') at insert time
    total_score INTEGER,
    max_score   INTEGER,
    risk_level  TEXT,                 -- e.g. "🔴 Critical"
    auth_score  INTEGER,
    perms_score INTEGER,
    behav_score INTEGER,
    acct_score  INTEGER,
    summary     TEXT                  -- one-sentence summary from agent reasoning
);
```

### Database functions — `mcp-server/database.py`

```python
def fetch_prior_assessment(user_id: str) -> dict | None:
    """Return the most recent assessment row for user_id, or None."""

def save_assessment_record(user_id, total_score, max_score, risk_level,
                           auth_score, perms_score, behav_score, acct_score,
                           summary) -> bool:
    """Insert a new assessment row."""
```

Both follow the same pattern as `fetch_user`, `flag_user_record`, etc. — `get_connection()`, execute, return.

---

## MCP tools

### `get_prior_assessment` — `mcp-server/server.py`

```python
@mcp.tool()
def get_prior_assessment(user_id: str) -> dict:
    """
    Return the most recent saved risk assessment for a user.
    Returns the assessment dict if found, or {"none": true} if no prior record exists.
    """
    prior = fetch_prior_assessment(user_id)
    return prior if prior else {"none": True}
```

### `save_assessment` — `mcp-server/server.py`

```python
@mcp.tool()
def save_assessment(user_id, total_score, max_score, risk_level,
                    auth_score, perms_score, behav_score, acct_score,
                    summary) -> dict:
    """Save a completed risk assessment for future comparison."""
    save_assessment_record(...)
    return {"success": True, "user_id": user_id}
```

Both tools are also added to `USER_TOOLS` in `flows/tools.py` with matching JSON schemas.

---

## Synthesis with comparison — `_synthesize_risk_report`

The function signature is extended with an optional `prior` parameter:

```python
def _synthesize_risk_report(user_id, auth, perms, behav, acct,
                             prior=None, thinking=True) -> str:
```

**First run** (`prior.get("none") == True`):
```
### Prior Assessment
None — this is the baseline assessment. Scores will be compared on the next run.
```

**Subsequent runs** (prior exists):
```
### Change Since Prior Assessment
Prior: 13/18 (🔴 Critical) on 2026-06-24
Current: 16/18 — overall change: **+3**
- Authentication: ↑ 2 points
- Behaviour: ↑ 1 point
```

Only dimensions that changed are listed. If no dimension changed, it prints "No change in any dimension".

---

## `run_flow_parallel_risk_with_memory` — `flows/run_flow.py`

```python
async def run_flow_parallel_risk_with_memory(user_id: str, verbose: bool = True) -> str:
    # Step 1: fetch prior (before agents, Python-driven)
    async with start_mcp_session() as session:
        prior_raw = await execute_tool(session, "get_prior_assessment", {"user_id": user_id})
    prior = json.loads(prior_raw)

    # Step 2: parallel agents with extended thinking (identical to option 8)
    auth, perms, behav, acct = await asyncio.gather(
        run_dimension_agent("auth",        user_id, verbose, thinking=True),
        run_dimension_agent("permissions", user_id, verbose, thinking=True),
        run_dimension_agent("behaviour",   user_id, verbose, thinking=True),
        run_dimension_agent("account",     user_id, verbose, thinking=True),
    )

    # Step 3: synthesize with comparison section
    report = _synthesize_risk_report(user_id, auth, perms, behav, acct,
                                     prior=prior, thinking=True)

    # Step 4: save this assessment
    async with start_mcp_session() as session:
        await execute_tool(session, "save_assessment", {
            "user_id": user_id, "total_score": total, "max_score": max_total,
            "risk_level": level, ..., "summary": summary,
        })

    return report
```

The `summary` field is populated from the first non-empty `reasoning` value across the four dimension agents — a one-sentence description of the key risk finding, already produced by extended thinking.

---

## Options comparison

| Option | Parallel Agents | Extended Thinking | Memory |
|---|---|---|---|
| 1–6 | ❌ | ❌ | ❌ |
| 7 | ✅ | ❌ | ❌ |
| 8 | ✅ | ✅ | ❌ |
| **9** | **✅** | **✅** | **✅** |

---

## Verification

1. Run `python flows/run_flow.py` → option 9 → `usr_005`
   - First run: prints `[MEMORY] No prior assessment — this will be the baseline.`
   - Report ends with "None — this is the baseline assessment."
2. Run option 9 again → `usr_005`
   - Prints `[MEMORY] Prior: X/Y (Level) on YYYY-MM-DD`
   - Report includes "Change Since Prior Assessment" section
3. Verify the DB: `sqlite3 seed/users.db "SELECT * FROM assessments;"`
4. Re-seed (`python seed/seed.py`) — assessments table is preserved (`IF NOT EXISTS`)

---

## Planned improvements

### Trend over time (not just last run)

`get_prior_assessment` returns only the most recent row. A `get_assessment_history(user_id, limit=5)` tool could return multiple snapshots, letting Claude (or pure Python) identify trends: "score has increased for 3 consecutive assessments."

### Assessment expiry

Old assessments may be misleading if a user's role changed significantly. Add an `assessed_within_days` parameter to `get_prior_assessment` so stale records are ignored:

```python
def get_prior_assessment(user_id: str, max_age_days: int = 30) -> dict:
    # only return if assessed_at >= now - max_age_days
```

### Diff at dimension level

Currently the comparison section shows per-dimension point deltas. A more detailed diff would identify which *specific factors* changed — e.g. "failed logins went from 3 to 15" rather than just "Authentication: ↑ 2 points."
