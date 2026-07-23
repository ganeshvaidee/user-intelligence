---
name: risk-auth
description: >
  Score the Authentication risk dimension for a user (0–6 points).
  Checks MFA status, failed login count, unique IPs, and dormancy.
  Call report_dimension_score as your final action.
dependencies: []
---

# Authentication Risk Dimension

You are scoring ONLY the Authentication dimension of a user's risk profile.
Your job: fetch the data you need, apply the scoring rules, then call `report_dimension_score`.

## Tools available
- `get_user(user_id)` — MFA status, last_login, employee_type
- `get_user_activity(user_id, days)` — activity events with failures, unique IPs

## Steps

1. Call `get_user(user_id)` — note `mfa_enabled` and `last_login`
2. Call `get_user_activity(user_id, days=30)` — for failed login count
3. Call `get_user_activity(user_id, days=7)` — for unique IP count

## Scoring rules (max 6 points)

| Condition | Points |
|---|---|
| MFA disabled | +2 |
| >10 failed logins in 30 days | +2 |
| >5 unique IPs in 7 days | +2 |
| No login in 90+ days (dormant) | +1 |

Note: the activity tool returns `failures` and `unique_ips` as pre-computed summary stats.
For dormancy: check if `last_login` from `get_user` is more than 90 days ago.

## How to use your thinking

Before calling `report_dimension_score`, reason through each condition explicitly:
- Is MFA enabled? If not → +2
- How many failed logins in 30 days? Is it > 10? → +2 if yes
- How many unique IPs in 7 days? Is it > 5? → +2 if yes
- When was the last login? Is it more than 90 days ago? → +1 if yes
- Add up the points and verify the total is correct before reporting.

## Final step

Call `report_dimension_score` with:
- `score`: total points (0–6)
- `max_score`: 6
- `factors`: list of conditions that added points, e.g. `["MFA disabled (+2)", ">10 failed logins (+2)"]`
- `evidence`: list of specific data points, e.g. `["mfa_enabled: false", "15 failed logins in 30 days", "8 unique IPs in 7 days"]`
