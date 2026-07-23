---
name: user-intelligence-base
description: >
  Base skill for all user intelligence operations. Load this whenever
  performing any user lookup, risk assessment, or account management task.
  Defines shared error handling, output format, and MCP tool conventions.
---

# User Intelligence Base Skill

## MCP Server
All user data comes from the `user-intelligence` MCP server.
Never query a database directly — always use these tools:

| Tool                        | Purpose                                      |
|-----------------------------|----------------------------------------------|
| `get_user(user_id)`         | Fetch core user record                       |
| `find_user_by_email(email)` | Look up user by email                        |
| `get_user_activity(user_id, days)` | Recent activity log + summary stats  |
| `get_user_permissions(user_id)` | All permissions, high-risk ones flagged  |
| `get_audit_log(user_id)`    | History of admin actions on this account     |
| `flag_user(user_id, reason)` | Flag account for review (non-destructive)   |
| `deactivate_user(user_id, reason)` | Permanently deactivate account         |

## Error Handling
If any tool returns `{"error": "..."}`:
1. Surface the error clearly to the user
2. Do not proceed with downstream steps
3. Suggest corrective action (e.g. "Check the user ID and try again")

## Output Format
Always structure your response as:

```
## [Action Performed] — [User Name] ([user_id])

**Status:** [result]

[Key findings or details]

**Next steps:** [what should happen next, if anything]
```

## Safety Rules
- Never call `deactivate_user` without explicit user confirmation
- Always call `flag_user` before `deactivate_user` in automated flows
- Every write action must include a clear, specific reason string
- If user status is already `inactive`, do not attempt further actions
