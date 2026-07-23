---
name: user-risk-profile
description: >
  Build a risk assessment for a user based on their activity patterns,
  permissions, account configuration, and status. Use when asked to
  assess, evaluate, review risk, or check if a user is suspicious.
  Outputs a risk score and recommended action.
---

# User Risk Profile Skill

## Dependencies
Read first:
1. `skills/_base/SKILL.md`
2. `skills/lookup-user/SKILL.md`

## Steps

1. **Fetch all data** using the lookup-user skill steps plus:
   - `get_user_activity(user_id, days=30)` — full 30-day window
   - `get_audit_log(user_id)` — prior admin actions

2. **Score each risk dimension** (0–3 points each):

### Dimension: Authentication
| Condition                         | Points |
|-----------------------------------|--------|
| MFA disabled                      | +2     |
| >10 failed logins in 30 days      | +2     |
| >5 unique IPs in 7 days           | +2     |
| No login in 90+ days (dormant)    | +1     |

### Dimension: Permissions
| Condition                         | Points |
|-----------------------------------|--------|
| Has admin-level permissions       | +2     |
| Has write to sensitive resources  | +1 each (max +3) |
| Contractor/vendor with high perms | +2     |

### Dimension: Behaviour
| Condition                         | Points |
|-----------------------------------|--------|
| Failure rate > 20%                | +2     |
| Accessing sensitive resources     | +1     |
| Activity outside business hours   | +1     |

### Dimension: Account
| Condition                         | Points |
|-----------------------------------|--------|
| Already flagged                   | +2     |
| Contractor or vendor type         | +1     |
| Account age < 30 days             | +1     |

3. **Classify total score:**

| Score | Risk Level | Recommended Action          |
|-------|------------|-----------------------------|
| 0–2   | 🟢 Low     | No action needed            |
| 3–5   | 🟡 Medium  | Monitor, review permissions |
| 6–9   | 🟠 High    | Flag account, notify manager|
| 10+   | 🔴 Critical| Immediate deactivation      |

4. **Output format:**

```
## Risk Assessment — [Name] ([user_id])

**Risk Score:** [N]/15   **Level:** [🟢/🟡/🟠/🔴] [Low/Medium/High/Critical]

### Score Breakdown
| Dimension      | Score | Key Factors                    |
|----------------|-------|--------------------------------|
| Authentication | N/6   | [e.g. No MFA, 12 failed logins]|
| Permissions    | N/5   | [e.g. admin-prod-db, write-billing] |
| Behaviour      | N/4   | [e.g. 35% failure rate]        |
| Account        | N/3   | [e.g. contractor]              |

### Recommended Action
[Specific recommendation based on score]

### Evidence
[2–4 bullet points of the most concerning specific findings]
```

## Important
- Present the evidence concisely — a security reviewer needs to act fast
- Do NOT automatically take any action — output the assessment only
- The offboard-user skill handles taking action based on this assessment
