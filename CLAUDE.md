# User Intelligence — Design Document

## What this project does

An IT-security agentic flow: Claude reads **skills** (natural-language rules) and calls **tools** (database operations) to look up users, assess risk, and offboard accounts. Model calls go through the **Anthropic API** by default, with Bedrock and OpenAI-compatible (including local open-weight) providers selectable via `LLM_PROVIDER` — see [Providers](#providers).

---

## High-level architecture

```
User request
     │
     ▼
run_flow.py          ← loads skills, runs agentic loop, manages MCP session
     │
     ├── skills/     ← Markdown files injected into Claude's system prompt
     │
     ├── LLM         ← Anthropic by default; Bedrock / OpenAI-compatible
     │                 selectable via LLM_PROVIDER (llm_client.py)
     │
     └── MCP session ← tools.py opens a session to mcp-server/server.py
                           │
                           └── database.py  ← SQLite (seed/users.db)
```

Three files do all the work:

| File | Responsibility |
|---|---|
| `flows/llm_client.py` | Provider toggle + capability declaration — the only file that knows which vendor is active |
| `flows/tools.py` | Tool schemas Claude sees, MCP session lifecycle, LLM judge helpers |
| `flows/run_flow.py` | Skill loading, agentic loop, three flow patterns |

---

## Skills

### What a skill is

A skill is a Markdown file (`SKILL.md`) that tells Claude **what steps to follow**, **what rules to apply**, and **what output format to use** for a specific task. Skills contain no Python — they are plain text loaded into Claude's system prompt.

Each skill file has a YAML frontmatter block followed by the skill body:

```markdown
---
name: lookup-user
description: >
  Look up a user by ID or email and return a clear summary...
---

# Lookup User Skill

## Steps
1. If given a user ID (starts with `usr_`): call `get_user(user_id)`
...
```

### How skills are loaded

`load_skill()` in `run_flow.py` reads one or more `SKILL.md` files, strips the YAML frontmatter (everything up to and including the closing `---`), and concatenates them into a single string separated by `---` dividers:

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

The resulting string is inserted into Claude's system prompt via `_build_system_prompt()`:

```python
def _build_system_prompt(skills_content: str) -> str:
    return (
        "You are a user intelligence assistant for an internal IT security team.\n"
        "You have access to user intelligence tools for all data operations.\n\n"
        "Follow the skills below precisely — they define your behavior for this task.\n\n"
        f"{skills_content}\n"
    )
```

### Skill dependency order

Skills are designed to compose. `_base` defines shared conventions (error handling, output format, safety rules) that all other skills depend on. Loading order matters — always load `_base` first:

```python
load_skill("_base", "lookup-user", "user-risk-profile", "offboard-user")
```

The dependency chain is:

```
_base
  └── lookup-user
        └── user-risk-profile
              └── offboard-user
```

Each skill's `SKILL.md` documents its dependencies explicitly so callers know what to load.

### The four skills

| Skill | What it instructs Claude to do |
|---|---|
| `_base` | Error handling rules, output format template, safety constraints (never deactivate without confirmation, always flag before deactivate, always include a reason on writes) |
| `lookup-user` | Resolve user ID or email → fetch user record, activity, and permissions → format a profile table |
| `user-risk-profile` | Run lookup steps + 30-day activity + audit log → score across 4 dimensions (auth, permissions, behaviour, account) → classify 0–15 → recommend action |
| `offboard-user` | Orchestrate the 5-step offboard flow: lookup → risk → flag → confirmation gate → deactivate. Never skips steps even if asked. |

### Skills in Claude Desktop

The MCP server also exposes the skills as **prompts** (`@mcp.prompt()` in `server.py`). These appear in Claude Desktop's prompt picker and load the same `SKILL.md` files:

```python
@mcp.prompt()
def risk_assessment() -> str:
    """Assess a user's risk on a 0–15 point scale."""
    return _load_skills("_base", "lookup-user", "user-risk-profile")
```

Select a prompt in Claude Desktop before asking a question to load the skill instructions into context.

---

## Agentic loops

### The core loop: `_run_tool_loop()`

All three flow patterns share the same inner loop in `run_flow.py`. It runs Claude in a message-passing loop, dispatching tool calls to the MCP server, until Claude signals it is done (`stop_reason == "end_turn"`):

```
while True:
    call Claude (Bedrock) with current messages
    for each block in response:
        if text → accumulate
        if tool_use → call MCP server → collect tool_result
    append assistant turn to messages
    if stop_reason == "end_turn" → break
    if tool_results → append user turn with results → continue
```

Claude drives the loop entirely — it decides which tools to call, in what order, and when it has enough information to stop. The loop never enforces a step sequence; that logic lives in the skills.

One MCP server session is opened per `run_flow*` call and shared across all loop iterations. This avoids the overhead of spawning a new subprocess (or HTTP connection) on every tool call.

### Flow 1: `run_flow()` — single shot

The simplest pattern. Opens an MCP session, runs one `_run_tool_loop()` call, returns the accumulated text.

```
user_request → [tool loop] → response_text
```

Use this when the task is well-defined and a single pass is expected to be sufficient.

### Flow 2: `run_flow_until_complete()` — convergence loop

After each round of the tool loop, a **second LLM call** (the completeness judge) checks whether the response fully addresses the original request. If not, the missing items are fed back into the conversation as a follow-up message and Claude runs another tool-use pass. The same MCP session and conversation thread continue across rounds.

```
round 1: user_request → [tool loop] → response
         → completeness judge → {complete: false, missing: [...]}
         → append "Your response is incomplete. Please also check: ..."
round 2: [tool loop continues same conversation] → response
         → completeness judge → {complete: true}
         → done
```

`max_rounds` acts as a hard ceiling. If the judge never returns `complete: true`, the loop exits after `max_rounds` iterations.

The completeness judge uses `tool_choice={"type": "any"}` to force structured output instead of free text:

```python
result = await client.messages.create(
    tools       = [_COMPLETENESS_TOOL],
    tool_choice = {"type": "any"},   # forces a tool_use block, not prose
    ...
)
return _first_tool_input(
    result, {"complete": True, "missing": [], "judge_unavailable": True}
)
# → {"complete": bool, "missing": [str, ...]}
```

This gives a parseable dict without fragile JSON extraction from free text.

`tool_choice` forces a tool call but cannot guarantee one — a `max_tokens`
truncation or refusal can end the turn with no readable `tool_use` block.
`_first_tool_input()` (in `flows/tools.py`) handles that by **failing open**:
it returns the supplied default so the flow keeps its answer instead of
crashing, and tags it `judge_unavailable` so callers can tell a degraded
verdict from a real one. Never parse a judge response with a bare
`next(...)` — that raises `StopIteration` and takes the whole flow down.
See `docs/improvements/llm-as-judge.md`.

### Flow 3: `run_flow_with_reflection()` — critic-revise

Runs the tool loop once, then a **second LLM call** (the critic) reviews the output for errors, unjustified claims, or gaps. If issues are found, Claude is asked to revise within the **same conversation thread** — it still has all prior tool results in context and can correct without re-fetching data.

```
phase 1: user_request → [tool loop] → initial_text
phase 2: critic LLM → {has_issues: true, issues: [...]}
phase 3: append "Your assessment has the following issues: ..."
         → [tool loop continues same conversation] → revised_text
```

If the critic finds no issues, the initial response is returned immediately — no revision pass. The same MCP session spans all three phases.

---

## Error handling

### Tool errors

MCP tool errors surface as JSON dicts, not exceptions. When a tool call fails (user not found, invalid state), the MCP server returns a dict with an `"error"` key:

```python
# server.py
def get_user(user_id: str) -> dict:
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}
    return user
```

`execute_tool()` in `tools.py` forwards this to Claude as the tool result content:

```python
async def execute_tool(session, name, inputs) -> str:
    result = await session.call_tool(name, inputs)
    if result.isError:
        error_text = result.content[0].text if result.content else "Unknown MCP error"
        return json.dumps({"error": error_text})
    return result.content[0].text
```

Claude receives the error dict as a tool result and the `_base` skill instructs it what to do:

> If any tool returns `{"error": "..."}`:
> 1. Surface the error clearly to the user
> 2. Do not proceed with downstream steps
> 3. Suggest corrective action

This means error handling is **skill-driven** — Claude decides how to respond to an error based on the rules in its system prompt, not hardcoded Python logic.

### State guard errors

Write tools (`flag_user`, `deactivate_user`) check current state before acting and return errors for illegal transitions:

```python
def flag_user(user_id, reason):
    if user["status"] == "inactive":
        return {"error": "Cannot flag an already inactive user"}

def deactivate_user(user_id, reason):
    if user["status"] == "inactive":
        return {"error": "User is already inactive"}
```

These prevent double-writes and make writes idempotent-safe.

### Partial failure in offboarding

The `offboard-user` skill explicitly handles the case where `flag_user` succeeds but `deactivate_user` fails:

> If Step 5 fails after Step 3 (flag succeeded but deactivate failed):
> - Inform the caller that the account is flagged but still active
> - Provide the user_id and ask them to retry deactivation manually
> - Do not attempt to undo the flag

The flag is left in place intentionally — it creates an audit trail and a security signal even if deactivation is retried later.

### No retries

There are no automatic retries in the tool loop or in the flow functions. The design relies on:
- MCP tools returning structured error dicts that Claude can reason about
- Skills instructing Claude to surface errors and stop rather than retry blindly
- The convergence loop (`run_flow_until_complete`) as the mechanism for re-attempting incomplete work — but driven by the completeness judge, not on error

---

## Security Guardrails

This codebase enforces **hard access boundaries** using two mechanisms:

### Per-Flow Tool Visibility

Each flow (a combination of loaded skills) restricts Claude to only the tools that those skills declare they need. This follows the principle of **minimal permissions**: Claude never sees a tool it shouldn't call.

**Implementation:** `tools_for_skills()` in `flows/tools.py` unions the tool sets from all loaded skills and filters `USER_TOOLS` to only those names.

**Example:** The offboard workflow has two phases:
- **offboard-prepare phase** loads `["_base", "lookup-user", "user-risk-profile", "offboard-prepare"]`
  - Visible tools: get_user, find_user_by_email, get_user_activity, get_user_permissions, get_audit_log, flag_user
  - NOT visible: `deactivate_user` (prevents premature account deactivation)
- **offboard-confirm phase** loads `["_base", "offboard-confirm"]`
  - Visible tools: `deactivate_user` only
  - NOT visible: lookup, risk, or flag tools (prevents re-running checks or re-flagging)

This is more than prose instruction — the tools Claude cannot see are literally not in the tool list it receives, making violations impossible instead of just discouraged.

### Order Guard

Some tools have dependencies: they should only be called after other tools have succeeded. The order guard enforces these at dispatch time (in `_run_tool_loop` before `execute_tool()`).

**Current requirements** (defined in `ORDER_REQUIREMENTS` in `flows/tools.py`):
- `flag_user` requires `get_user_activity` — prevents flagging without looking at activity data
- `deactivate_user` requires `flag_user` — prevents deactivating an account without flagging it first

**How it works:** If Claude attempts to call a tool before its dependencies are met:
1. The order guard detects this in `_run_tool_loop`
2. Instead of calling the MCP server, it returns an error dict: `{"error": "Cannot call deactivate_user before flag_user has succeeded..."}`
3. Claude receives this error as a tool result
4. The `_base` skill's error-handling rules instruct Claude to surface the error and retry in the right order
5. Claude naturally recovers and calls the dependencies first

This means the LLM can still reason about what went wrong and fix its behavior, but it cannot bypass the dependency at the dispatch layer.

---

## MCP server: tool setup and discoverability

### How tools are defined

Tools are defined in `mcp-server/server.py` using FastMCP's `@mcp.tool()` decorator. FastMCP reads the function signature and docstring to build the JSON schema that the MCP protocol exposes:

```python
mcp = FastMCP(name="user-intelligence")

@mcp.tool()
def get_user(user_id: str) -> dict:
    """
    Fetch a user record by ID.
    Returns name, email, department, role, status, last_login, mfa_enabled.
    Returns an error dict if user not found.
    """
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}
    return user
```

FastMCP derives the tool name from the function name, the input schema from the type-annotated parameters, and the description from the docstring. No separate schema file is needed.

### Two parallel schema definitions

The same tools are described **twice** — once for the MCP protocol (in `server.py`) and once for the Bedrock API (in `tools.py`):

| Location | Used by | Format |
|---|---|---|
| `server.py` `@mcp.tool()` | MCP protocol, Claude Desktop | FastMCP auto-generated JSON schema |
| `tools.py` `USER_TOOLS` | Bedrock `messages.create(tools=...)` | Hand-written Anthropic tool schema |

These must be kept in sync. When adding a tool, update both. The descriptions in `USER_TOOLS` can be more detailed than the docstring since they are Claude's primary signal for when and how to call the tool.

### Transport modes and discoverability

The MCP server supports two transports, selected at startup:

**stdio (default)** — used by Claude Desktop and by `tools.py` when `MCP_URL` is not set. Claude Desktop or the parent process spawns `server.py` as a subprocess and communicates over stdin/stdout. The client calls `session.initialize()` which triggers the MCP handshake — the server responds with its full list of tools, prompts, and capabilities. No URL needed; the process is the server.

**HTTP (streamable-http)** — used when services are deployed separately. `server.py` listens on a port; clients connect via `streamablehttp_client`. `tools.py` switches to this mode when `MCP_URL` is set:

```python
MCP_URL = os.environ.get("MCP_URL")

@asynccontextmanager
async def start_mcp_session():
    if MCP_URL:
        async with streamablehttp_client(f"{MCP_URL}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        server_params = StdioServerParameters(
            command=sys.executable,   # same Python binary as the caller
            args=[str(MCP_SERVER)],
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
```

`sys.executable` is used (not the string `"python"`) so the subprocess always uses the same virtualenv Python as the orchestrator, regardless of what is on `PATH`.

In both transports, `session.initialize()` performs the MCP handshake and the session object's interface is identical — callers never need to know which transport is active.

### How the orchestrator discovers tools

The orchestrator (`run_flow.py`) does not query the MCP session for its tool list at runtime. Instead, `USER_TOOLS` in `tools.py` is the **static declaration** that Claude receives at the start of every model call. The MCP session is only used to **execute** tool calls that Claude decides to make based on those schemas.

This means:
- Tool schemas are part of the Bedrock API call (`tools=USER_TOOLS` in every `client.messages.create(...)`)
- Claude chooses which tools to call based on the schemas + skill instructions
- `execute_tool()` routes Claude's `tool_use` blocks to the MCP server via `session.call_tool(name, inputs)`
- The MCP server's own schema (from `@mcp.tool()`) is used by Claude Desktop and for the MCP protocol handshake — not by the Bedrock flow

---

## Project structure

```
user-intelligence/
├── flows/
│   ├── llm_client.py       ← provider toggle + capability declaration
│   ├── anthropic_client.py ← direct Anthropic API client (the default)
│   ├── bedrock_client.py   ← AWS Bedrock client (boto3 + AsyncAnthropicBedrock)
│   ├── openai_compat_client.py ← OpenAI/vLLM adapter (only file importing openai)
│   ├── tools.py            ← USER_TOOLS schemas, execute_tool(), MCP session, judge helpers
│   └── run_flow.py         ← orchestration, skill loader, flow patterns, examples
│
├── orchestrator/
│   ├── app.py              ← FastAPI service wrapping the flow functions
│   └── requirements.txt
│
├── client/
│   └── cli.py              ← CLI that calls the orchestrator over HTTP
│
├── skills/
│   ├── _base/SKILL.md              ← shared: error handling, output format, safety rules
│   ├── lookup-user/SKILL.md        ← fetch + summarise a user record
│   ├── user-risk-profile/SKILL.md  ← 0–15 point risk scoring (auth/perms/behaviour/account)
│   └── offboard-user/SKILL.md      ← lookup → risk → flag → CONFIRM gate → deactivate
│
├── mcp-server/
│   ├── server.py       ← FastMCP server (tools + prompts)
│   ├── database.py     ← SQLite layer
│   └── requirements.txt
│
├── seed/
│   ├── seed.py         ← creates schema + 8 test users
│   └── users.db        ← SQLite database
│
└── tests/
    └── test_flows.py   ← eval-style tests: checks tools called + response content
```

## Setup

```bash
pip install -r mcp-server/requirements.txt   # anthropic[bedrock], boto3, fastmcp
python seed/seed.py                          # create users.db with test data
```

### Providers

`LLM_PROVIDER` selects the client. **Anthropic is the default**, and `run_flow.py`/`tools.py` are written for its API directly. The other providers are adapted to look like it and are allowed to support less — but adding one must never make the Anthropic code more generic. See `docs/improvements/multi-provider.md`.

| `LLM_PROVIDER` | Model env var | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_MODEL_ID` | direct API |
| `bedrock` | `BEDROCK_MODEL_ID` | AWS credentials in `~/.aws/credentials` under `default`, Bedrock access to `us.anthropic.claude-sonnet-4-6` in `us-west-2` |
| `local` | `LOCAL_MODEL_ID`, `LOCAL_BASE_URL` | open-weight model on LM Studio/vLLM/SGLang |
| `openai` | `OPENAI_MODEL_ID`, `OPENAI_API_KEY` | hosted OpenAI API |

`local` and `openai` need an extra install (`pip install -r flows/requirements-local.txt`) and runs through `flows/openai_compat_client.py`, the only file permitted to `import openai`. The local path is verified against Muse Glimmer 30B on LM Studio:

```bash
lms server start
export LLM_PROVIDER=local
export LOCAL_BASE_URL=http://127.0.0.1:1234/v1
export LOCAL_MODEL_ID=meta/muse-glimmer
```

Verify the wiring before spending minutes on a flow — `scripts/local_smoke.py` checks reachability, tool calling, forced `tool_choice`, reasoning traces and adapter refusals in ~60s, and names the fix for each failure:

```bash
LLM_PROVIDER=local python scripts/local_smoke.py
```

Then `python flows/run_flow.py` for a real flow. Expect ~4 minutes for a lookup that Claude finishes in seconds. See `docs/improvements/multi-provider.md` for the full test ladder, what was measured, and what still is not.

Adding a provider means editing `_CAPABILITIES` in `flows/llm_client.py` and nothing in the flows. `tests/test_provider_isolation.py` fails the build if provider handling leaks out of that layer.

All LLM calls default to `temperature=0` (deterministic) via `flows/llm_client.py` — `TEMPERATURE` for the main agentic loop, `JUDGE_TEMPERATURE` for the completeness-judge/critic calls, independently overridable with `LLM_TEMPERATURE` / `LLM_JUDGE_TEMPERATURE`. See `docs/improvements/temperature-determinism.md`.

## Test users

| ID | Name | Profile |
|---|---|---|
| usr_001 | Alice Chen | Normal senior engineer |
| usr_002 | Bob Martinez | Normal engineer |
| usr_005 | Eve Contractor | **High risk**: no MFA, broad perms, suspicious activity, external IPs |
| usr_006 | Frank Old | **Dormant**: no login in 180 days |
| usr_007 | Grace Flagged | Already flagged |
| usr_008 | Henry Inactive | Already deactivated |

Use `usr_005` to exercise risk/offboard flows. Use `usr_001` to verify low-risk paths.

Re-seed after any offboard test: `python seed/seed.py`

## Adding a new skill

**Step 1: Create the skill file**
1. Create `skills/<name>/SKILL.md` with YAML frontmatter:
   ```markdown
   ---
   name: my-skill
   description: >
     Short description of what this skill does...
   ---
   
   # My Skill
   
   ## Steps
   1. Call tool X
   2. ...
   ```
2. Write the skill body: steps, rules, output format, error handling

**Step 2: Declare tool visibility**

Add an entry to `SKILL_TOOLS` in `flows/tools.py`:
```python
SKILL_TOOLS: dict[str, set[str]] = {
    ...
    "my-skill": {"tool1", "tool2", ...},
}
```

List exactly which tools this skill calls. When skills are loaded together (e.g., `["_base", "lookup-user", "my-skill"]`), their tool sets are unioned and Claude only sees that union.

**Example:** offboard-prepare calls lookup, risk assessment, and flagging:
```python
"offboard-prepare": {"flag_user"},  # rest inherited from dependency skills
```
Even though lookup-user and user-risk-profile are also loaded, their tools are added via the SKILL_TOOLS union. offboard-prepare itself only adds `flag_user`; it cannot add `deactivate_user` (that tool is restricted to offboard-confirm).

**Step 3: Watch for tool ordering constraints**

If your skill calls a tool that has `ORDER_REQUIREMENTS`, ensure your steps follow that order. For example:
- If calling `flag_user`, you must call `get_user_activity` first (or Claude will receive an order-guard error)
- If calling `deactivate_user`, you must call `flag_user` first

The `_base` skill's error-handling rules will instruct Claude to retry in the right sequence if it attempts the wrong order.

**Step 4: Load and test**

Pass the skill name when calling a flow function:
```python
response = await run_flow(
    user_request = "...",
    skill_names  = ["_base", "lookup-user", "my-skill"],
)
```

Skills are loaded in dependency order (always `_base` first if needed). The tool visibility and order guards are applied automatically.

## Adding a new tool

A tool exists in three places and must be kept in sync:

### 1. Database layer: `mcp-server/database.py`

Add the implementation (if it needs a database):
```python
def my_tool(user_id: str, reason: str) -> dict:
    # Fetch, validate, and mutate as needed
    return {"status": "success", ...}
```

### 2. MCP server: `mcp-server/server.py`

Register the tool with FastMCP:
```python
@mcp.tool()
def my_tool(user_id: str, reason: str) -> dict:
    """
    What this tool does and when to use it.
    Returns a dict with results or {"error": "..."}
    """
    return database.my_tool(user_id, reason)
```

FastMCP auto-generates the JSON schema from the function signature and docstring.

### 3. Bedrock layer: `flows/tools.py`

Add the JSON schema to `USER_TOOLS` (so Claude knows about the tool):
```python
USER_TOOLS = [
    ...
    {
        "name": "my_tool",
        "description": (
            "Detailed description of what this tool does, when to call it, "
            "and what output to expect. This is Claude's primary signal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "..."},
                "reason":  {"type": "string", "description": "..."},
            },
            "required": ["user_id", "reason"],
        },
    },
]
```

The description in `USER_TOOLS` can be more detailed than the server.py docstring, since it's Claude's primary guide for when and how to call the tool.

### 4. Tool visibility: `flows/tools.py` — update `SKILL_TOOLS`

Decide which skills should call this tool, and add it to their sets:
```python
SKILL_TOOLS: dict[str, set[str]] = {
    ...
    "skill-that-uses-my-tool": {"my_tool", ...},
}
```

Only skills that declare this tool in `SKILL_TOOLS` will expose it to Claude. Other flows will not see it.

### 5. Tool ordering (if needed): `flows/tools.py` — update `ORDER_REQUIREMENTS`

If a tool should only be called after other tools have succeeded, add an entry to enforce this as a hard constraint (not just prose instruction):

```python
ORDER_REQUIREMENTS: dict[str, list[str]] = {
    "flag_user":       ["get_user_activity"],
    "deactivate_user": ["flag_user"],
}
```

The order guard in `_run_tool_loop` enforces these dependencies at dispatch time: if Claude attempts to call a tool before its prerequisites succeed, it receives an error dict instead of MCP dispatch.

**Real example from this codebase:**

The user deactivation flow has two critical dependencies:

1. **`flag_user` requires `get_user_activity`**
   - Security rationale: Never flag a user without examining recent activity
   - If Claude calls `flag_user` directly without checking activity, it receives:
     ```json
     {"error": "Cannot call flag_user before get_user_activity has succeeded in this conversation."}
     ```
   - Claude reads this error, calls `get_user_activity` first, then retries `flag_user`

2. **`deactivate_user` requires `flag_user`**
   - Security rationale: Account must be flagged (audit trail) before permanent deactivation
   - If Claude attempts to deactivate without flagging first, it receives:
     ```json
     {"error": "Cannot call deactivate_user before flag_user has succeeded in this conversation."}
     ```
   - Claude recovers by calling `flag_user` first (which itself requires activity lookup)

This creates a **chain of enforcement**: to deactivate, you must flag; to flag, you must look at activity. The order guard makes violations impossible, not just discouraged by prose.

### Duplicate guard

A repeated **read** with byte-identical arguments cannot return new information, so `_dispatch_tool_use` answers it with an error instead of re-dispatching, and escalates to a hard "stop calling tools, write your answer now" on the third attempt. `WRITE_TOOLS` (in `flows/tools.py`) are exempt — `database.py`'s state guards are the authority there — and a successful write clears the cached reads so re-reading after a state change still works.

This exists because a local open-weight model looped ten times on one call and returned nothing. It is dormant for Claude, which does not repeat an identical call three times. See `docs/improvements/multi-provider.md`.

### 6. Skills: Update `SKILL.md` files

Add or update skill files to instruct Claude when to call your tool:
- When should this tool be called?
- What preconditions must be met?
- What should Claude do with the result?

The skill prose and the code-level guardrails (visibility + order guard) work together: prose explains the intent, guardrails enforce the boundaries.
