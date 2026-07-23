# Tool Use (Function Calling)

## What it is

Tool use is the mechanism by which Claude calls real functions instead of answering from training data. Claude is given a list of tool schemas describing what functions exist and what arguments they take. When it decides a tool is needed, it returns a `tool_use` block naming the tool and providing arguments. The calling code executes the function and returns the result. Claude then reasons over the result and decides what to do next.

Claude never executes code directly — it only declares intent. The Python code dispatches that intent.

---

## How it works in this project

### The two-layer design

Tools are defined in two places that must stay in sync:

**Layer 1 — What Claude sees (`flows/tools.py`):**

`USER_TOOLS` is a list of JSON schemas passed to every `client.messages.create` call. Claude reads these descriptions to decide which tool to call and how to construct the arguments.

```python
USER_TOOLS = [
    {
        "name": "get_user",
        "description": (
            "Fetch a user record by ID. "
            "Returns name, email, department, role, status, last_login, mfa_enabled. "
            "Returns an error dict if user not found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "The user ID to look up"},
            },
            "required": ["user_id"],
        },
    },
    ...
]
```

**Layer 2 — What actually runs (`mcp-server/server.py`):**

FastMCP `@mcp.tool()` decorators register the real Python functions. FastMCP derives its own JSON schema from the function signature and docstring for the MCP protocol. Claude Desktop uses this schema; the Bedrock flow uses `USER_TOOLS`.

```python
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

The two schemas must be kept in sync manually — the name, parameters, and semantics must match. When adding a tool, both layers need updating.

---

### The 7 tools

**READ tools** — fetch data, no side effects:

| Tool | Arguments | Returns |
|---|---|---|
| `get_user` | `user_id` | Core user record: name, email, dept, role, status, last_login, mfa_enabled |
| `find_user_by_email` | `email` | Same as `get_user` — resolves email to user record |
| `get_user_activity` | `user_id`, `days=30` | Summary stats (total, failures, unique_ips) + first 20 events |
| `get_user_permissions` | `user_id` | All permissions + pre-computed high-risk list |
| `get_audit_log` | `user_id` | History of admin actions (flags, deactivations) on the account |

**WRITE tools** — modify state, include guard checks:

| Tool | Arguments | Side effect |
|---|---|---|
| `flag_user` | `user_id`, `reason` | Sets status to `flagged`. Does NOT block login. |
| `deactivate_user` | `user_id`, `reason` | Sets status to `inactive`. Blocks all future logins. |

---

### How Claude chooses which tool to call

Claude reads the `description` field of each schema. The descriptions are written to be unambiguous about when to use each tool and what it returns. Key patterns:

- **Distinguish similar tools:** `find_user_by_email` says *"Useful when you have an email but not a user ID"* — Claude uses it only when the identifier is an email, not a user ID.
- **Clarify write scope:** `flag_user` says *"Does NOT deactivate the account — user can still log in"* and `deactivate_user` says *"Use only after confirmation"* — preventing premature or wrong writes.
- **State what's returned:** listing what fields come back helps Claude know whether to call another tool or whether it already has what it needs.

The skill instructions (`SKILL.md` files) layer explicit step sequencing on top — e.g., `offboard-user` names the exact tools to call in order. Description quality + skill instructions together drive Claude's decisions.

---

### How tool calls flow through the system

```
Claude returns:
  ToolUseBlock(id="tu_abc", name="get_user", input={"user_id": "usr_005"})

_run_tool_loop dispatches:
  result = await execute_tool(session, "get_user", {"user_id": "usr_005"})

execute_tool calls MCP server:
  result = await session.call_tool("get_user", {"user_id": "usr_005"})

MCP server runs:
  server.py get_user("usr_005") → database.py fetch_user("usr_005") → SQLite

Returns JSON string back to the loop:
  '{"id": "usr_005", "name": "Eve Contractor", "status": "active", ...}'

Loop appends to messages:
  {"type": "tool_result", "tool_use_id": "tu_abc", "content": "{...}"}
```

`execute_tool` in `tools.py` is the single dispatch point — it routes all tool calls through the MCP session and normalises errors into JSON dicts so Claude always receives a parseable response.

---

### Error handling in tools

All tools return errors as dicts, not exceptions:

```python
def get_user(user_id: str) -> dict:
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}   # ← Claude reads this
    return user
```

Write tools add state-guard checks before acting:

```python
def flag_user(user_id: str, reason: str) -> dict:
    if user["status"] == "inactive":
        return {"error": "Cannot flag an already inactive user"}
```

Claude receives `{"error": "..."}` as the tool result. The `_base` skill instructs it to surface the error and stop — no downstream steps. This means error logic is in the skill (what to do) not the Python (what happened).

---

### Redundancy detection

`_run_tool_loop` tracks every tool call made within a flow using a `seen_calls` dict keyed by `tool_name:{sorted_args}`. If Claude calls the same tool with the same arguments a second time, it prints a warning:

```
[DUPLICATE TOOL CALL] get_user({"user_id": "usr_005"}) already called — redundant MCP call
```

This is a diagnostic, not a block — the call still executes. The intent is to surface cases where Claude re-fetches data it already has in context, particularly across rounds in the convergence loop.

---

## Planned improvements

### 1. Tool result caching

**Problem:** The duplicate detection prints a warning but still makes the redundant MCP call. In a convergence loop where Claude re-fetches the same user across rounds, this is wasted latency and database load.

**How it works:** Cache tool results in `_run_tool_loop` keyed by `tool_name:{sorted_args}`. Return the cached result on a duplicate call instead of hitting the MCP server.

```python
_tool_cache: dict[str, str] = {}

cache_key = f"{block.name}:{json.dumps(block.input, sort_keys=True)}"
if cache_key in _tool_cache:
    result = _tool_cache[cache_key]
    print(f"[CACHE HIT] {block.name}({block.input}) — served from cache")
else:
    result = await execute_tool(session, block.name, block.input)
    _tool_cache[cache_key] = result
```

The cache lives for the lifetime of the flow call — it is not persisted across requests.

**Trade-off:** READ tools are safe to cache (same input always returns same output for a given DB state). WRITE tools (`flag_user`, `deactivate_user`) should never be cached — calling them twice is probably a bug, but returning a stale result would be worse. Cache only READ tools.

---

### 2. Typed tool results

**Problem:** Tool results are returned as raw JSON strings. The loop, skills, and tests all treat them as opaque text. There's no validation that a tool returned the shape Claude expects — a database schema change would silently return different fields.

**How it works:** Define Pydantic models for each tool's return type. Validate the result in `execute_tool` before returning it to Claude.

```python
from pydantic import BaseModel

class UserRecord(BaseModel):
    id: str
    name: str
    email: str
    status: str
    mfa_enabled: bool
    employee_type: str

TOOL_RETURN_SCHEMAS = {
    "get_user": UserRecord,
    ...
}

async def execute_tool(session, name, inputs) -> str:
    result_str = await session.call_tool(name, inputs)
    if name in TOOL_RETURN_SCHEMAS:
        data = json.loads(result_str)
        if "error" not in data:
            TOOL_RETURN_SCHEMAS[name](**data)  # raises ValidationError if shape is wrong
    return result_str
```

Validation errors surface immediately at the tool boundary rather than causing silent misbehaviour downstream.

---

### 3. Tool descriptions as the authoritative source

**Problem:** Tool descriptions exist in two places (`USER_TOOLS` in `tools.py` and docstrings in `server.py`) and must be kept in sync manually. A description update in one place that's missed in the other means Claude Desktop and the Bedrock flow get different guidance.

**How it works:** Make `server.py` the single source of truth. At startup, query the MCP server for its tool list via `session.list_tools()` and build `USER_TOOLS` dynamically from what the server returns — no separate `USER_TOOLS` definition needed.

```python
async def get_user_tools(session: ClientSession) -> list[dict]:
    response = await session.list_tools()
    return [
        {
            "name":         tool.name,
            "description":  tool.description,
            "input_schema": tool.inputSchema,
        }
        for tool in response.tools
    ]
```

This eliminates the sync requirement entirely. The trade-off is that `USER_TOOLS` is no longer statically available — it must be fetched per session. The prompt caching approach (adding `cache_control` to the last tool) would need to happen after fetching.

---

## Adding a new tool — checklist

1. Add the database function to `mcp-server/database.py`
2. Add `@mcp.tool()` to `mcp-server/server.py` with a clear docstring
3. Add the matching JSON schema to `USER_TOOLS` in `flows/tools.py` — name, description, and `input_schema` must match the server definition
4. Update the relevant `SKILL.md` files to tell Claude when to call the new tool
5. Add an eval case in `tests/test_flows.py` that asserts the tool is called for an appropriate request