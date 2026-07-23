# MCP (Model Context Protocol)

## What MCP is

MCP is a protocol that lets Claude call functions on a running server rather than executing code directly. The MCP server exposes a set of tools — Claude calls them by name with arguments, the server executes the real logic, and returns a result. Claude never touches the database directly.

In this project MCP serves as the boundary between Claude's reasoning and the SQLite database. All reads and writes go through the MCP server, which means tool logic lives in one place and can be used by both the Bedrock flows and Claude Desktop without duplication.

---

## Architecture

```
Claude (via Bedrock)
    │
    │  tool_use block: {name: "get_user", input: {user_id: "usr_005"}}
    ▼
execute_tool()  [flows/tools.py]
    │
    │  session.call_tool("get_user", {user_id: "usr_005"})
    ▼
MCP session  [MCP protocol — stdio or HTTP]
    │
    ▼
server.py  [mcp-server/server.py]
    │
    │  fetch_user("usr_005")
    ▼
database.py  [mcp-server/database.py]
    │
    ▼
SQLite  [seed/users.db]
```

---

## The MCP server — `mcp-server/server.py`

### Setup

FastMCP creates the server and names it. The name appears in Claude Desktop's tool list and in MCP logs:

```python
from fastmcp import FastMCP
mcp = FastMCP(name="user-intelligence")
```

### Tool registration

Tools are registered with `@mcp.tool()`. FastMCP derives the tool name, argument schema, and description automatically from the function signature and docstring:

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

FastMCP uses:
- **Function name** → tool name (`get_user`)
- **Type-annotated parameters** → input schema (`user_id: str` → `{type: string}`)
- **Docstring** → tool description (shown in Claude Desktop)

### Prompt registration

Skills are also exposed as prompts via `@mcp.prompt()`. These appear in Claude Desktop's prompt picker and load the same `SKILL.md` files that the Bedrock flows use:

```python
@mcp.prompt()
def risk_assessment() -> str:
    """Assess a user's risk on a 0–15 point scale."""
    return _load_skills("_base", "lookup-user", "user-risk-profile")
```

Selecting a prompt before asking a question in Claude Desktop loads the skill instructions into context — no separate configuration needed.

### READ vs WRITE tools

Tools are split into two groups with different safety properties:

**READ tools** — no side effects, safe to call multiple times:

| Tool | Key logic |
|---|---|
| `get_user` | Returns error dict if not found |
| `find_user_by_email` | Returns error dict if not found |
| `get_user_activity` | Pre-computes summary stats (total, failures, unique_ips); returns first 20 events |
| `get_user_permissions` | Pre-computes high-risk list (admin/write to sensitive resources) |
| `get_audit_log` | Returns raw audit entries |

**WRITE tools** — modify state; include guard checks before acting:

| Tool | Guards |
|---|---|
| `flag_user` | Checks user exists; rejects if already inactive |
| `deactivate_user` | Checks user exists; rejects if already inactive |

Both write tools return errors as dicts, not exceptions:

```python
def flag_user(user_id: str, reason: str) -> dict:
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}
    if user["status"] == "inactive":
        return {"error": "Cannot flag an already inactive user"}
    flag_user_record(user_id, reason)
    return {"success": True, "user_id": user_id, "new_status": "flagged", "reason": reason}
```

---

## Two transport modes

The MCP server supports two transports, selected at startup. The calling code (`tools.py`) switches automatically based on the `MCP_URL` environment variable.

### stdio (default)

Claude Desktop or `tools.py` (when `MCP_URL` is unset) spawns `server.py` as a subprocess and communicates over stdin/stdout. No port, no network — the process is the server.

```python
server_params = StdioServerParameters(
    command=sys.executable,   # same Python binary as the orchestrator
    args=[str(MCP_SERVER)],
)
async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        yield session
```

`sys.executable` is used instead of the string `"python"` so the subprocess always uses the same virtualenv Python as the orchestrator, regardless of what is on `PATH`.

Started with:
```bash
python mcp-server/server.py          # defaults to stdio
```

### HTTP (streamable-http)

Used when services run separately. The server listens on a port; clients connect via HTTP.

```python
if MCP_URL:
    async with streamablehttp_client(f"{MCP_URL}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
```

Started with:
```bash
python mcp-server/server.py --transport http --port 8001
# MCP server starting on http://0.0.0.0:8001
```

Orchestrator connects by setting:
```bash
MCP_URL=http://localhost:8001 python orchestrator/app.py
```

### Transport comparison

| | stdio | HTTP |
|---|---|---|
| Use case | Claude Desktop, all-in-one CLI | Three-service deployment, IDE debugging |
| Session lifetime | Lives as long as the parent process | One session per `run_flow` call (fresh per round in convergence loop) |
| MCP session teardown | Process exit | HTTP DELETE /mcp |
| Configuration | None — subprocess auto-started | `MCP_URL` env var |

---

## Session lifecycle — `start_mcp_session()`

`start_mcp_session()` in `tools.py` is an async context manager that opens and closes the MCP session:

```python
@asynccontextmanager
async def start_mcp_session():
    if MCP_URL:
        async with streamablehttp_client(f"{MCP_URL}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        server_params = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER)])
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
```

`session.initialize()` performs the MCP protocol handshake — the server responds with its tool list, prompt list, and capabilities. After `initialize()`, the session is ready to execute tool calls.

In the convergence loop (`run_flow_until_complete`), a **fresh session is opened per round** (not shared across rounds). This is because holding the HTTP session open during the completeness judge call (which can take 10–30s) caused the underlying SSE connection to fail, producing a TaskGroup crash. Opening a new session per round eliminates the window for that failure.

In the reflection flow (`run_flow_with_reflection`), **one session spans all three phases** — the critique call is fast (no tool calls), so the connection stays live.

---

## How the orchestrator discovers tools

The Bedrock flows do **not** query the MCP session for its tool list at runtime. Instead, `USER_TOOLS` in `tools.py` is a static declaration that Claude receives in every `client.messages.create` call. The MCP session is used only to **execute** tool calls that Claude decides to make based on those schemas.

The flow is:
1. Claude receives `USER_TOOLS` schemas → decides which tools to call
2. `execute_tool(session, name, inputs)` routes the call to the MCP server
3. MCP server executes the real Python function
4. Result returned as JSON string → appended to `msgs` as a tool result

The MCP server's own schema (from `@mcp.tool()` docstrings) is used by Claude Desktop and for the MCP protocol handshake — not by the Bedrock flow. This means the two schema definitions must be kept in sync manually.

---

## Limitations

**Two schema definitions must stay in sync.** `USER_TOOLS` in `tools.py` and `@mcp.tool()` docstrings in `server.py` describe the same tools in two different formats. A description update or parameter change in one place that's missed in the other means Claude Desktop and the Bedrock flow get different guidance.

**Session-per-round overhead.** The convergence loop opens and closes an MCP session for every round to avoid the HTTP connection timeout issue. Each session open involves a subprocess spawn (stdio) or HTTP handshake (HTTP mode), adding latency.

**No MCP server health check.** If the MCP server is down when a flow starts, `session.initialize()` will fail with an unhelpful error. There's no pre-flight check or retry.

---

## Planned improvements

### 1. Dynamic tool discovery

**Problem:** `USER_TOOLS` and `@mcp.tool()` must be kept in sync manually. A mismatch silently gives Claude Desktop and the Bedrock flow different tool descriptions.

**How it works:** Query the MCP session for its tool list after `initialize()` and build the Anthropic schema dynamically:

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

`USER_TOOLS` in `tools.py` is removed. The tool list is fetched once per flow from the MCP server — the single source of truth is `server.py` docstrings. Prompt caching of the tool list would still apply (cache after fetching, before the first Bedrock call).

---

### 2. MCP server health check on startup

**Problem:** If the MCP server isn't running when the orchestrator starts (HTTP mode), tool calls will fail at runtime with an unhelpful connection error.

**How it works:** Add a startup check in the orchestrator that verifies the MCP server is reachable before accepting requests:

```python
# orchestrator/app.py
@app.on_event("startup")
async def check_mcp_server():
    if MCP_URL:
        async with start_mcp_session() as session:
            tools = await session.list_tools()
            print(f"MCP server connected — {len(tools.tools)} tools available")
```

Fails fast at orchestrator startup rather than on the first user request.

---

### 3. Session pooling for the convergence loop

**Problem:** Opening a new MCP session per convergence round adds latency — each session open requires a subprocess spawn or HTTP handshake.

**How it works:** Keep a small pool of pre-initialised MCP sessions and check one out for each round rather than creating a new one:

```python
class MCPSessionPool:
    def __init__(self, size: int = 3):
        self._sessions = asyncio.Queue()
        self._size = size

    async def initialise(self):
        for _ in range(self._size):
            session = await _open_session()
            await self._sessions.put(session)

    @asynccontextmanager
    async def acquire(self):
        session = await self._sessions.get()
        try:
            yield session
        finally:
            await self._sessions.put(session)
```

Each round checks out a session, uses it for `_run_tool_loop`, and returns it to the pool. No session is held open during the completeness judge call between rounds — the pool just holds sessions that aren't in use, avoiding the HTTP timeout issue while eliminating per-round setup cost.

**Trade-off:** More complexity, and pool sessions need to be kept alive (heartbeat or reconnect logic). Only worth implementing if round latency from session setup becomes measurable.