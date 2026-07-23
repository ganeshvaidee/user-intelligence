---
name: offboard-confirm
description: >
  Phase 2 of Human-in-the-Loop offboarding: deactivate the user account.
  The human has already reviewed the Phase 1 summary and confirmed.
  The account is already flagged. Proceed directly to deactivation.
dependencies:
  - _base
---

# Offboard Confirm Skill (Phase 2 of 2)

## Purpose

This is Phase 2 of a two-phase Human-in-the-Loop offboarding flow.
The human has already reviewed the risk assessment and typed CONFIRM.
The account is already flagged from Phase 1.
Your only job is to deactivate the account and confirm completion.

## Step — Deactivate

Call:
```
deactivate_user(user_id, reason="[caller-provided reason]")
```

Then output the completion summary:

```
## ✅ Offboarding Complete — [Name] ([user_id])

Account has been deactivated following human confirmation.

| Action              | Result    |
|---------------------|-----------|
| Account flagged     | ✅ (Phase 1) |
| Human confirmed     | ✅        |
| Account deactivated | ✅        |
| Audit log updated   | ✅        |

**Reason recorded:** [reason]
**Performed at:** [timestamp]

Recommend: revoke any API keys, rotate shared secrets,
and notify [department] manager.
```

## Error Recovery
If `deactivate_user` fails:
- Inform the caller that the account is flagged but still active
- Provide the user_id and ask them to retry Phase 2 manually
- Do not attempt to undo the flag — it provides a security signal

## What This Skill Does NOT Do
- Does not re-run lookup or risk assessment (already done in Phase 1)
- Does not ask for confirmation again (human already confirmed)
- Does not revoke API keys or send notifications
