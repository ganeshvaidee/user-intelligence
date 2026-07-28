---
name: offboard-prepare
description: >
  Phase 1 of Human-in-the-Loop offboarding: look up the user, assess risk,
  apply the pre-deactivation flag, and return a summary for human review.
  STOP after flagging. Do NOT ask for confirmation. Do NOT deactivate.
  The human confirmation gate is handled by the calling client.
dependencies:
  - _base
  - lookup-user
  - user-risk-profile
---

# Offboard Prepare Skill (Phase 1 of 2)

Assess the user and flag the account — then stop.
Deactivation happens in Phase 2 (`offboard-confirm`) only if the human approves.

## Steps

### Step 1 — Lookup
Run the full lookup-user flow for the given user.
If the user is already `inactive`: stop and inform the caller — do not flag.
If the user is already `flagged`: note this and continue.

### Step 2 — Risk Assessment
Run the full user-risk-profile flow.
This determines urgency and shapes the summary the human will review.

### Step 3 — Pre-Deactivation Flag
Call:
```
flag_user(user_id, reason="Pre-offboarding flag — pending deactivation")
```
This creates an audit trail even if the human cancels in Phase 2.

### Step 4 — Output Summary for Human Review
Present this summary clearly so the human can make an informed decision:

```
## ⚠️  Pending Offboarding — [Name] ([user_id])

A human must confirm before this account is deactivated.

| Field               | Value                           |
|---------------------|---------------------------------|
| Name                | [name]                          |
| Email               | [email]                         |
| Department          | [dept]                          |
| Risk Level          | [🟢/🟡/🟠/🔴] [level]          |
| Risk Score          | [N]/18                          |
| Permissions         | [N] total, [N] high-risk        |
| Last Login          | [date]                          |
| Current Status      | flagged (pending deactivation)  |

**High-risk permissions that will be revoked if confirmed:**
[list them, or "None"]

**Account has been flagged. Awaiting human confirmation to deactivate.**
```

## CRITICAL: Stop Here

Do NOT ask the user to type CONFIRM.
Do NOT call `deactivate_user`.
Do NOT proceed to deactivation under any circumstances.

The client owns the confirmation gate. Your output will be reviewed by a human
who will decide whether to proceed with Phase 2.
