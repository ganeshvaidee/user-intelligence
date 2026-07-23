# Human-in-the-Loop Offboarding (Option 3)

> **Scope: Option 3 only** — Full offboarding via the client CLI or all-in-one CLI.
> All other flows (1, 2, 4–9) are fully automated with no human pause.

---

## Problem

The original offboard skill (`offboard-user`) embedded the confirmation gate inside the agentic loop — Claude would ask "Type CONFIRM to proceed" and wait for the user to respond via stdin. This worked in the all-in-one CLI where stdin is interactive, but broke in the three-service mode: the orchestrator blocks on a single HTTP request with no mechanism for the human to inject a response mid-flow.

More fundamentally, a mid-flow pause conflates two distinct responsibilities:
- **AI responsibility:** assess the user, apply the audit flag, present the risk summary
- **Human responsibility:** decide whether to proceed with an irreversible action

Separating these into two API calls makes each responsibility explicit and the system simpler.

---

## Solution: Two-Phase Flow

Split offboarding into two separate, stateless HTTP calls. The client owns the human pause between them.

```
Phase 1: assess + flag
    Client → POST /offboard/prepare/stream
    Claude: lookup → risk assessment → flag_user → return summary
    Client ← streaming report

    Human reviews the report
    Human types CONFIRM or cancels

Phase 2: deactivate (only if confirmed)
    Client → POST /offboard/confirm/stream
    Claude: deactivate_user → return completion
    Client ← streaming completion
```

**The orchestrator is stateless between phases.** No session persistence, no queue, no async waiting. The only context carried between phases is `user_id` and `reason` — local variables in the client. The DB flag from Phase 1 is the durable state: if the human cancels, the account stays flagged as a security signal that offboarding was attempted.

---

## New skill files

### `skills/offboard-prepare/SKILL.md`

Steps 1–3 of the original offboard skill, with an explicit STOP instruction:

- **Step 1** — Full lookup-user flow. Stop and report if user is already inactive.
- **Step 2** — Full user-risk-profile flow.
- **Step 3** — `flag_user(user_id, reason="Pre-offboarding flag — pending deactivation")`
- **STOP.** Output the confirmation summary table. Do NOT ask for CONFIRM. Do NOT call `deactivate_user`.

The skill explicitly tells Claude: *"The human confirmation gate is handled by the calling client."*

### `skills/offboard-confirm/SKILL.md`

Step 5 only, with no pre-amble:

- Call `deactivate_user(user_id, reason)` — the human has already confirmed
- Output the completion summary

The skill tells Claude: *"The human has already confirmed. Proceed directly to deactivation."*

The original `skills/offboard-user/SKILL.md` is unchanged — it remains available for direct `run_flow` calls in the all-in-one CLI and backward-compatible usage.

---

## New flow functions — `flows/run_flow.py`

Both reuse the existing `run_flow()` — no new loop logic:

```python
async def run_flow_offboard_prepare(user_id: str, reason: str, verbose: bool = True) -> str:
    return await run_flow(
        user_request = f"Prepare offboarding for user {user_id}. Reason: {reason}. "
                       f"Run lookup, risk assessment, and pre-deactivation flag. "
                       f"Stop after flagging — do NOT ask for confirmation or deactivate.",
        skill_names  = ["_base", "lookup-user", "user-risk-profile", "offboard-prepare"],
        verbose      = verbose,
    )

async def run_flow_offboard_confirm(user_id: str, reason: str, verbose: bool = True) -> str:
    return await run_flow(
        user_request = f"Deactivate user {user_id}. Reason: {reason}. "
                       f"The human has confirmed. Proceed with deactivation.",
        skill_names  = ["_base", "offboard-confirm"],
        verbose      = verbose,
    )
```

---

## New orchestrator endpoints — `orchestrator/app.py`

Dedicated request model (user_id + reason, not the generic user_request + skill_names):

```python
class OffboardRequest(BaseModel):
    user_id: str
    reason:  str
```

Four endpoints — blocking and streaming for each phase:

| Endpoint | Phase | What it does |
|---|---|---|
| `POST /offboard/prepare` | 1 | Assess + flag, return JSON |
| `POST /offboard/prepare/stream` | 1 | Assess + flag, SSE stream |
| `POST /offboard/confirm` | 2 | Deactivate, return JSON |
| `POST /offboard/confirm/stream` | 2 | Deactivate, SSE stream |

The client uses the `/stream` variants so the human sees the report as it generates.

---

## Client as the agent — `client/cli.py`

`run_offboard_hitl` owns the full interaction loop for option 3:

```python
def run_offboard_hitl(user_id: str, reason: str) -> None:
    # Phase 1 — stream the assessment and flag
    print("PHASE 1: Assessing and flagging...")
    call_offboard_phase_stream("/offboard/prepare/stream", user_id, reason)

    # Human gate — client owns this, not the orchestrator
    print("⚠️  Account has been FLAGGED. Deactivation is pending your confirmation.")
    response = input("\nType CONFIRM to deactivate, anything else to cancel: ").strip()

    if response.upper() != "CONFIRM":
        print("❌ Cancelled. Account remains flagged for review.")
        return                                # Phase 2 never called

    # Phase 2 — stream the deactivation
    print("PHASE 2: Deactivating...")
    call_offboard_phase_stream("/offboard/confirm/stream", user_id, reason)
```

`call_offboard_phase_stream` is a thin SSE wrapper that POSTs `{user_id, reason}` to the given endpoint and prints tokens as they arrive — same pattern as `call_orchestrator_stream`.

---

## All-in-one CLI — `run_flow.py` option 3

`example_offboard()` mirrors the same two-phase structure for direct CLI use:

```python
async def example_offboard():
    user_id = input("User ID: ").strip() or "usr_005"
    reason  = input("Reason: ").strip() or "contractor contract ended"

    print("[PHASE 1] Running assessment and flagging...")
    await run_flow_offboard_prepare(user_id, reason)

    response = input("\nType CONFIRM to deactivate, anything else to cancel: ").strip()
    if response.upper() != "CONFIRM":
        print("❌ Cancelled. Account remains flagged.")
        return

    print("[PHASE 2] Deactivating...")
    await run_flow_offboard_confirm(user_id, reason)
```

---

## Why the client owns the confirmation gate

The client is the human-facing layer — it is the natural place for human interaction. Keeping the gate in the client means:

- The orchestrator stays stateless (no sessions, no queues, no async waiting)
- The human pause happens between two HTTP requests, not inside one
- The UX can evolve independently (add a timeout, show enriched context, send a Slack notification) without touching the orchestrator or the skills
- The gate can be bypassed programmatically by calling the two endpoints in sequence — useful for automated testing or scripted workflows

---

## Comparison: old vs new

| | Original (single-phase) | New (two-phase HITL) |
|---|---|---|
| Confirmation gate | Inside the agentic loop | In the client, between two HTTP calls |
| Works in three-service mode | ❌ (no way to respond mid-flow) | ✅ |
| Orchestrator state | Held for entire flow | None — stateless |
| Cancel behaviour | Account not flagged if cancelled before Step 3 | Account flagged even on cancel (audit trail) |
| Backward compatible | n/a | ✅ — `offboard-user` skill unchanged |

---

## Planned improvements

### Slack/email notification on Phase 1 completion

Instead of (or in addition to) the terminal prompt, Phase 1 could trigger a notification with an approve/reject link. The client would poll for the response or receive a webhook callback before calling Phase 2. This is the full async HITL pattern used in production security workflows.

### Timeout on pending confirmation

If the human doesn't respond within N minutes, automatically cancel Phase 2 and send an alert. The account remains flagged — a human will still need to act, but the time-sensitive deactivation decision has a bounded window.

### Audit trail for the human decision

Currently the human's CONFIRM is not recorded anywhere. Adding an `audit_log` entry like `"offboarding confirmed by operator via CLI at [timestamp]"` would make the full decision chain auditable.
