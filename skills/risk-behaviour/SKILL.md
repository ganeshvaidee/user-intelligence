---
name: risk-behaviour
description: >
  Score the Behaviour risk dimension for a user (0–4 points).
  Checks login failure rate, sensitive resource access, and off-hours activity.
  Call report_dimension_score as your final action.
dependencies: []
---

# Behaviour Risk Dimension

You are scoring ONLY the Behaviour dimension of a user's risk profile.
Your job: fetch the data you need, apply the scoring rules, then call `report_dimension_score`.

## Tools available
- `get_user_activity(user_id, days=30)` — activity events with failure stats and resource access

## Steps

1. Call `get_user_activity(user_id, days=30)`
2. From the result: compute failure rate = `failures / total` (if total > 0)
3. From `events`: check if any access `prod-database`, `secrets`, `admin-panel`, `billing`, `user-data`
4. From `events`: check if any `timestamp` falls outside 08:00–18:00 local business hours

## Scoring rules (max 4 points)

| Condition | Points |
|---|---|
| Login failure rate > 20% | +2 |
| Accessing sensitive resources (prod-db, secrets, admin-panel, billing, user-data) | +1 |
| Activity outside business hours (before 08:00 or after 18:00) | +1 |

Use the `failures` and `total` summary fields for failure rate.
For resource and time checks, inspect the `events` list (up to 20 events returned).

## How to use your thinking

Before calling `report_dimension_score`, reason through each condition explicitly:
- What is the failure rate? (failures / total). Is it > 20%? → +2 if yes. If total is 0, failure rate is 0.
- Do any events show access to sensitive resources (prod-database, secrets, admin-panel, billing, user-data)? → +1 if yes
- Do any event timestamps fall outside 08:00–18:00? → +1 if yes
- State the exact numbers (e.g. "15 failures out of 60 events = 25% > 20% threshold") to justify each point.

## Final step

Call `report_dimension_score` with:
- `score`: total points (0–4)
- `max_score`: 4
- `factors`: list of conditions that added points, e.g. `["Failure rate >20% (+2)", "Sensitive resource access (+1)"]`
- `evidence`: list of specific data points, e.g. `["25% failure rate (15/60 events)", "Accessed prod-database, secrets", "Login at 02:34 UTC"]`
