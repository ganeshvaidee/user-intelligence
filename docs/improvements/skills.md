# Skills (System Prompt Engineering)

## What skills are

A skill is a Markdown file (`SKILL.md`) that tells Claude **what steps to follow**, **what rules to apply**, and **what output format to produce** for a specific task. Skills contain no Python — they are plain text loaded into Claude's system prompt at runtime.

The separation is intentional:
- **Skills** = the recipe (steps, rules, constraints, output format)
- **Tools** = the hands (actual DB queries, writes, API calls)

Changing how Claude behaves for a task means editing a Markdown file, not touching Python. Adding a new workflow means writing a new skill, not wiring new logic.

---

## File structure

Each skill lives in its own directory under `skills/`:

```
skills/
├── _base/
│   └── SKILL.md          ← shared: error handling, output format, safety rules
├── lookup-user/
│   └── SKILL.md          ← fetch + summarise a user record
├── user-risk-profile/
│   └── SKILL.md          ← 0–15 point risk scoring across 4 dimensions
└── offboard-user/
    └── SKILL.md          ← 5-step offboarding flow
```

Each `SKILL.md` has a YAML frontmatter block followed by the skill body:

```markdown
---
name: lookup-user
description: >
  Look up a user by ID or email and return a clear summary of their
  account status, role, last activity, and MFA status.
---

# Lookup User Skill

## Dependencies
Read first: `skills/_base/SKILL.md`

## Steps
1. If given a user ID (starts with `usr_`): call `get_user(user_id)`
2. If given an email: call `find_user_by_email(email)` to get the ID, then proceed
...
```

The frontmatter (`name`, `description`) is metadata only — it is stripped before the skill is injected into the system prompt.

---

## How skills are loaded

`load_skill()` in `flows/run_flow.py` reads one or more `SKILL.md` files, strips the YAML frontmatter, and concatenates them into a single string:

```python
def load_skill(*skill_names: str) -> str:
    parts = []
    for name in skill_names:
        path = SKILLS_DIR / name / "SKILL.md"
        content = path.read_text()
        if content.startswith("---"):
            end = content.index("---", 3) + 3
            content = content[end:].strip()
        parts.append(f"# SKILL: {name}\n\n{content}")
    return "\n\n---\n\n".join(parts)
```

The result is passed to `_build_system_prompt()`, which wraps it with a role declaration:

```python
def _build_system_prompt(skills_content: str) -> str:
    return (
        "You are a user intelligence assistant for an internal IT security team.\n"
        "You have access to user intelligence tools for all data operations.\n\n"
        "Follow the skills below precisely — they define your behavior for this task.\n\n"
        f"{skills_content}\n"
    )
```

The combined string becomes the `system` parameter on every Bedrock call. With prompt caching enabled, this content is processed once and served from cache on subsequent calls within the same flow.

---

## Dependency order

Skills are designed to compose. Later skills build on earlier ones without repeating their rules. Loading order matters — `_base` must always come first:

```
_base
  └── lookup-user
        └── user-risk-profile
              └── offboard-user
```

Each `SKILL.md` documents its own dependencies explicitly so callers know what to load. Passing them in the wrong order works syntactically but would give Claude conflicting or incomplete instructions.

---

## The four skills

### `_base`

Loaded in every flow. Defines shared conventions all other skills depend on:

- **Tool reference table** — lists every MCP tool with its purpose so Claude knows what's available
- **Error handling rules** — if a tool returns `{"error": "..."}`, surface it and stop; do not proceed with downstream steps
- **Output format template** — standard heading structure for all responses
- **Safety rules** — never call `deactivate_user` without confirmation; always call `flag_user` before `deactivate_user`; every write must include a reason string

### `lookup-user`

Instructs Claude to:
1. Resolve the identifier (user ID → `get_user`; email → `find_user_by_email` first)
2. Fetch activity (7-day window) and permissions in parallel if possible
3. Format a profile table with status emoji, MFA indicator, last login age
4. Automatically flag: no MFA, contractor/vendor type, dormant (>90 days), flagged status, elevated permissions

### `user-risk-profile`

Instructs Claude to run the full lookup steps plus a 30-day activity window and audit log, then score across four dimensions:

| Dimension | Max points | Key signals |
|---|---|---|
| Authentication | 6 | No MFA (+2), >10 failed logins (+2), >5 unique IPs (+2) |
| Permissions | 5 | Admin perms (+2), write to sensitive resources (+1 each, max 3), contractor with high perms (+2) |
| Behaviour | 4 | Failure rate >20% (+2), sensitive resource access (+1), off-hours activity (+1) |
| Account | 3 | Already flagged (+2), contractor type (+1), account age <30 days (+1) |

Classifies 0–2 as Low, 3–5 Medium, 6–9 High, 10+ Critical. Does **not** take any action — assessment only.

### `offboard-user`

Orchestrates a mandatory 5-step flow. Explicitly states it never skips steps even if the caller asks:

1. **Lookup** — run full lookup-user flow; stop if already inactive
2. **Risk assessment** — run full user-risk-profile flow
3. **Pre-deactivation flag** — call `flag_user(user_id, reason="Pre-offboarding flag — pending deactivation")` to create an audit trail even if deactivation is interrupted
4. **Confirmation gate** — present a summary table and require the user to respond with exactly `CONFIRM` (case-insensitive); cancel if anything else
5. **Deactivate** — call `deactivate_user` with the provided reason; confirm with a completion summary

Handles partial failure: if Step 5 fails after Step 3 (flag succeeded, deactivate failed), instruct the caller to retry manually — do not attempt to undo the flag.

---

## Skills as prompts in Claude Desktop

The MCP server also exposes skills as **prompts** via `@mcp.prompt()` in `server.py`. These appear in Claude Desktop's prompt picker and load the same `SKILL.md` files:

```python
@mcp.prompt()
def risk_assessment() -> str:
    """Assess a user's risk on a 0–15 point scale."""
    return _load_skills("_base", "lookup-user", "user-risk-profile")
```

Selecting a prompt in Claude Desktop before asking a question loads the skill instructions into context — the same content that `load_skill()` injects programmatically in the Bedrock flows.

---

## Limitations of the current approach

**No version control on skill behaviour.** A skill edit changes Claude's behaviour immediately for all callers. There's no staging or rollback mechanism — if a skill change breaks a flow, the only fix is to revert the file manually.

**No validation that skills were followed.** Claude might ignore a skill rule (e.g., skip the confirmation gate if the user request is worded persuasively). Nothing in the Python code enforces skill compliance — only the evals catch this after the fact.

**No skill unit testing.** Skills are only tested end-to-end via `test_flows.py`. There's no way to test a single skill in isolation (e.g., test `user-risk-profile` scoring without running a full Bedrock flow).

**Dependency order is implicit.** The correct loading order is documented in each `SKILL.md` but not enforced in code. Passing skills in the wrong order silently produces a malformed system prompt.

---

## Planned improvements

### 1. Enforce dependency order in `load_skill()`

**Problem:** `load_skill("user-risk-profile", "_base")` is accepted without error even though `_base` must always come first.

**How it works:** Read the `dependencies` field from each skill's frontmatter and validate that they appear earlier in the list before loading:

```python
import yaml

def _read_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    end = content.index("---", 3)
    return yaml.safe_load(content[3:end]) or {}

def load_skill(*skill_names: str) -> str:
    loaded = []
    for name in skill_names:
        content = (SKILLS_DIR / name / "SKILL.md").read_text()
        meta = _read_frontmatter(content)
        for dep in meta.get("dependencies", []):
            if dep not in loaded:
                raise ValueError(f"Skill '{name}' depends on '{dep}' which hasn't been loaded yet")
        loaded.append(name)
        ...
```

---

### 2. Skill compliance checking in evals

**Problem:** Tests check whether Claude called the right tools but not whether it followed the skill rules — e.g., did it present the confirmation gate before deactivating?

**How it works:** Add rule-specific assertions to the eval suite that mirror the skill's stated rules. These go beyond keyword matching to check ordering and presence of required elements:

```python
# From test_offboard_requires_confirmation — already partially done
failures += assert_tools_called(tools, ["flag_user"])          # Step 3 rule
failures += assert_tools_not_called(tools, ["deactivate_user"]) # Step 4 gate
if not any(kw in response.lower() for kw in ["confirm", "cannot be undone"]):
    failures.append("Confirmation gate not presented")          # Step 4 rule
```

Extend this pattern to cover every safety rule in `_base` and every mandatory step in `offboard-user`.

---

### 3. Skill versioning

**Problem:** There's no way to know which version of a skill produced a given response, making debugging hard and regressions invisible.

**How it works:** Add a `version` field to each skill's frontmatter. Log the skill name and version at the start of each flow alongside the request:

```yaml
---
name: user-risk-profile
version: "1.2"
---
```

```python
def load_skill(*skill_names: str) -> str:
    for name in skill_names:
        meta = _read_frontmatter(content)
        print(f"[SKILL] {name} v{meta.get('version', 'unversioned')}")
    ...
```

Versions in logs make it possible to correlate a behaviour change to a specific skill edit.