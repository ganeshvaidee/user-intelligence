# User Intelligence

An IT-security agentic workflow: Claude reads **skills** (natural-language rules) and calls **MCP tools** (database operations) to look up users, assess risk, and offboard accounts. All model calls go through AWS Bedrock.

## How it works

```
Claude
  ├── reads SKILL.md files → knows what steps to follow and rules to apply
  └── calls MCP server tools → actually queries the database
        ├── get_user(user_id)
        ├── get_user_activity(user_id, days)
        ├── get_user_permissions(user_id)
        ├── flag_user(user_id, reason)
        └── deactivate_user(user_id, reason)
```

**Skills** = the recipe (steps, rules, output format)
**MCP server** = the hands (DB access)

---

## Setup

### 1. Install dependencies

```bash
pip install -r mcp-server/requirements.txt
```

### 2. Seed the database

```bash
python seed/seed.py
```

### 3. AWS credentials

Add your credentials to `~/.aws/credentials` under the `default` profile with Bedrock access to `us.anthropic.claude-sonnet-4-6` in `us-west-2`.

---

## Running the code

There are three ways to run this project.

---

### Mode 1 — All-in-one command-line app

The simplest way. Starts the MCP server automatically as a subprocess and presents an interactive menu. No other services need to be running.

```bash
cd flows
python run_flow.py
```


User Intelligence — Flow Runner
================================
  1. Lookup user
  2. Risk assessment
  3. Full offboarding
  4. Find by email + risk
  5. Risk assessment (convergence loop)
  6. Risk assessment (critic-revise)
  7. Risk assessment (parallel agents)
  8. Risk assessment (parallel agents + extended thinking)
  9. Risk assessment (parallel + extended thinking + memory)

Choose a flow (1-9):

---

### Mode 2 — Claude Desktop

Use the MCP server directly from the Claude Desktop UI. Claude Desktop manages the MCP server process; you load the skills as project instructions.

**Step 1: Configure the MCP server**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "user-intelligence": {
      "command": "/path/to/your/python",
      "args": ["/Users/GaneshVaideeswaran/projects/veevabc/user-intelligence/mcp-server/server.py"]
    }
  }
}
```

Replace `/path/to/your/python` with the full path from `which python` in your activated virtualenv (e.g. `/Users/GaneshVaideeswaran/virtualenv/nitro/user-intelligence/bin/python`).

**Step 2: Load the skills**

Create a **Project** in Claude Desktop and paste the contents of these files into the project instructions, in this order:

1. `skills/_base/SKILL.md`
2. `skills/lookup-user/SKILL.md`
3. `skills/user-risk-profile/SKILL.md`
4. `skills/offboard-user/SKILL.md` *(only if you want the offboard flow)*

**Step 3: Restart Claude Desktop**, then ask questions in the project:

> "Give me a risk assessment for usr_005"
> "Look up eve@vendor.com"
> "Offboard usr_005. Reason: contract ended."

Claude Desktop will call the MCP server tools automatically.

---

### Mode 3 — Separate services (MCP server + orchestrator + client)

Run each component independently. This is useful for debugging, IDE integration, or deploying components separately.

**Terminal 1 — MCP server (HTTP mode):**

```bash
python mcp-server/server.py --transport http --port 8001
```

Expected output:

```
MCP server starting on http://0.0.0.0:8001
```

**Terminal 2 — Orchestrator:**

```bash
MCP_URL=http://localhost:8001 python orchestrator/app.py --port 8000
```

**Terminal 3 — Client:**

```bash
python client/cli.py
```

The client presents the same interactive menu as Mode 1. To point at a different orchestrator host:

```bash
ORCHESTRATOR_URL=http://localhost:8000 python client/cli.py
```

#### IDE Run/Debug configuration (IntelliJ)

Create two Python run configurations. Start **MCP Server first**, then **Orchestrator**.

**MCP Server:**


| Field             | Value                          |
| ----------------- | ------------------------------ |
| Script path       | `.../mcp-server/server.py`     |
| Script parameters | `--transport http --port 8001` |
| Working directory | `.../mcp-server/`              |
| Interpreter       | your virtualenv Python         |

**Orchestrator:**


| Field                 | Value                                              |
| --------------------- | -------------------------------------------------- |
| Script path           | `.../orchestrator/app.py`                          |
| Script parameters     | `--port 8000`                                      |
| Working directory     | `.../orchestrator/`                                |
| Environment variables | `MCP_URL=http://localhost:8001;PYTHONUNBUFFERED=1` |
| Interpreter           | your virtualenv Python                             |

Once both are running, start the client from a terminal with `python client/cli.py`.

---

## Test users


| ID      | Name           | Profile                                                                       |
| ------- | -------------- | ----------------------------------------------------------------------------- |
| usr_001 | Alice Chen     | Normal senior engineer — use for low-risk paths                              |
| usr_002 | Bob Martinez   | Normal engineer                                                               |
| usr_005 | Eve Contractor | **High risk**: no MFA, broad permissions, suspicious logins from external IPs |
| usr_006 | Frank Old      | **Dormant**: no login in 180 days                                             |
| usr_007 | Grace Flagged  | Already flagged                                                               |
| usr_008 | Henry Inactive | Already deactivated                                                           |

After any offboard test that calls `deactivate_user`, re-seed the database:

```bash
python seed/seed.py
```

---

## Running tests

```bash
cd flows
python ../tests/test_flows.py
```

Tests verify which tools were called, what the response contains, and that safety rules were followed (flag before deactivate, no writes without a reason).
