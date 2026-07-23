---
name: risk-account
description: >
  Score the Account risk dimension for a user (0–3 points).
  Checks if already flagged, contractor/vendor type, and account age.
  Call report_dimension_score as your final action.
dependencies: []
---

# Account Risk Dimension

You are scoring ONLY the Account dimension of a user's risk profile.
Your job: fetch the data you need, apply the scoring rules, then call `report_dimension_score`.

## Tools available
- `get_user(user_id)` — status, employee_type, created_at
- `get_audit_log(user_id)` — history of admin actions (flags, deactivations)

## Steps

1. Call `get_user(user_id)` — note `status`, `employee_type`, `created_at`
2. Call `get_audit_log(user_id)` — check for prior flag or deactivation entries

## Scoring rules (max 3 points)

| Condition | Points |
|---|---|
| Account already flagged (`status == "flagged"`) | +2 |
| Contractor or vendor (`employee_type` is "contractor" or "vendor") | +1 |
| Account age < 30 days (created_at within last 30 days) | +1 |

## How to use your thinking

Before calling `report_dimension_score`, reason through each condition explicitly:
- Is the account status "flagged"? → +2 if yes
- Is employee_type "contractor" or "vendor"? → +1 if yes
- What is the created_at date? Is the account less than 30 days old? → +1 if yes
- State the exact values from the data (e.g. "status: active → 0 points, employee_type: contractor → +1") before reporting.

## Final step

Call `report_dimension_score` with:
- `score`: total points (0–3)
- `max_score`: 3
- `factors`: list of conditions that added points, e.g. `["Already flagged (+2)", "Contractor type (+1)"]`
- `evidence`: list of specific data points, e.g. `["status: flagged", "employee_type: contractor", "created_at: 2026-06-01 (22 days ago)"]`
