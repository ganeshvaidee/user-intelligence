---
name: risk-permissions
description: >
  Score the Permissions risk dimension for a user (0–5 points).
  Checks admin access, write to sensitive resources, and contractor with high permissions.
  Call report_dimension_score as your final action.
dependencies: []
---

# Permissions Risk Dimension

You are scoring ONLY the Permissions dimension of a user's risk profile.
Your job: fetch the data you need, apply the scoring rules, then call `report_dimension_score`.

## Tools available
- `get_user(user_id)` — employee_type (contractor/vendor check)
- `get_user_permissions(user_id)` — full permission list with high-risk pre-flagged

## Steps

1. Call `get_user(user_id)` — note `employee_type`
2. Call `get_user_permissions(user_id)` — note `high_risk` list and `all_permissions`

## Scoring rules (max 5 points)

| Condition | Points |
|---|---|
| Has admin-level permissions | +2 |
| Has write to sensitive resources | +1 each, max +3 |
| Contractor/vendor with high-risk permissions | +2 |

Sensitive resources: `prod-database`, `user-data`, `billing`, `secrets`, `admin-panel`.
The `high_risk` list in `get_user_permissions` already filters for admin/write on sensitive resources.

Note: the +2 for admin and +1 each for write can stack. Max total is 5 points.

## How to use your thinking

Before calling `report_dimension_score`, reason through each condition explicitly:
- Does the user have any admin-level permissions? → +2 if yes
- How many write permissions to sensitive resources (prod-database, user-data, billing, secrets, admin-panel)? → +1 each, max +3
- Is the user a contractor or vendor AND has at least one high-risk permission? → +2 if both true
- Add up the points carefully — the write condition has a cap of +3, admin is separate +2.

## Final step

Call `report_dimension_score` with:
- `score`: total points (0–5)
- `max_score`: 5
- `factors`: list of conditions that added points, e.g. `["Admin permissions (+2)", "Write to billing (+1)", "Contractor with high perms (+2)"]`
- `evidence`: list of specific permissions, e.g. `["admin-prod-db (admin)", "write-billing (write)", "employee_type: contractor"]`
