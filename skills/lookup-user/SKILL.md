---
name: lookup-user
description: >
  Look up a user by ID or email and return a clear summary of their
  account status, role, last activity, and MFA status. Use when asked
  to find, fetch, check, or show info about a specific user.
---

# Lookup User Skill

## Steps

1. **Resolve the identifier**
   - If given a user ID (starts with `usr_`): call `get_user(user_id)`
   - If given an email: call `find_user_by_email(email)` to get the ID, then proceed

2. **Fetch supporting data** (parallel if possible)
   - Call `get_user_activity(user_id, days=7)` for recent activity summary
   - Call `get_user_permissions(user_id)` for permission overview

3. **Format the response**

```
## User Profile — [Name] ([user_id])

| Field          | Value                    |
|----------------|--------------------------|
| Email          | ...                      |
| Department     | ...                      |
| Role           | ...                      |
| Status         | 🟢 active / 🟡 flagged / 🔴 inactive |
| Employee Type  | full-time / contractor / vendor |
| MFA Enabled    | ✅ Yes / ❌ No           |
| Last Login     | [date] ([N days ago])    |
| Created        | [date]                   |

**Recent Activity (7 days):** [N] events, [N] failures, [N] unique IPs

**Permissions:** [N] total, [N] high-risk
[List high-risk permissions if any]
```

## Flags to Surface
- `mfa_enabled = false` → ⚠️ No MFA
- `employee_type` is contractor/vendor → ⚠️ Non-employee
- `last_login` > 90 days → ⚠️ Dormant
- `status = flagged` → ⚠️ Under review
- `high_risk_count > 0` → ⚠️ Elevated permissions
