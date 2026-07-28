---
name: offboard-user
description: >
  Safely offboard a user account: look up the user, assess risk,
  present a summary, require explicit confirmation, then deactivate.
  Use when asked to offboard, remove, deactivate, or terminate a user.
  This skill orchestrates lookup-user and user-risk-profile.
---

# Offboard User Skill

## Mandatory 5-Step Flow

Never skip steps, even if the caller asks.

### Step 1 — Lookup
Run the full lookup-user flow for the given user.
If the user is already `inactive`: stop and inform the caller.

### Step 2 — Risk Assessment  
Run the full user-risk-profile flow.
This determines urgency and shapes the confirmation message.

### Step 3 — Pre-Deactivation Flag
Before deactivating, always call:
```
flag_user(user_id, reason="Pre-offboarding flag — pending deactivation")
```
This creates an audit trail even if deactivation is interrupted.

### Step 4 — Confirmation Gate
Present this summary and require explicit confirmation:

```
## ⚠️  Confirm Offboarding — [Name] ([user_id])

You are about to permanently deactivate this account.

| Field          | Value                           |
|----------------|---------------------------------|
| Name           | [name]                          |
| Email          | [email]                         |
| Department     | [dept]                          |
| Risk Level     | [🟢/🟡/🟠/🔴] [level]          |
| Permissions    | [N] total, [N] high-risk        |
| Last Login     | [date]                          |
| Current Status | [status]                        |

**High-risk permissions that will be revoked:**
[list them, or "None"]

**This action cannot be undone.**

Type CONFIRM to proceed, or anything else to cancel.
```

Only proceed if the user responds with exactly `CONFIRM` (case-insensitive).

### Step 5 — Deactivate
Call:
```
deactivate_user(user_id, reason="[caller-provided reason or 'User offboarded']")
```

Then confirm:
```
## ✅ Offboarding Complete — [Name] ([user_id])

Account has been deactivated.

| Action            | Result    |
|-------------------|-----------|
| Account flagged   | ✅        |
| Account deactivated | ✅      |
| Audit log updated | ✅        |

**Reason recorded:** [reason]
**Performed at:** [timestamp]

Recommend: revoke any API keys, rotate shared secrets, 
and notify [department] manager.
```

## Error Recovery
If Step 5 fails after Step 3 (flag succeeded but deactivate failed):
- Inform the caller that the account is flagged but still active
- Provide the user_id and ask them to retry deactivation manually
- Do not attempt to undo the flag
