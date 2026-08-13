# User Intelligence

An IT-security agentic workflow: Claude reads **skills** (natural-language rules) and calls **MCP tools** (database operations) to look up users, assess risk, and offboard accounts. Model calls go through either the direct Anthropic API or AWS Bedrock — see [Model access](#3-model-access) below.

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

## Skills Overview

The project includes **4 main skills** + **6 sub-skills** for advanced modes:

**Main Skills:**
- `_base` — error handling, safety rules
- `lookup-user` — fetch user records
- `user-risk-profile` — risk assessment (single-agent or parallel-agent mode)
- `offboard-user` — offboarding flow (single-flow or two-phase mode)

**Two Risk Assessment Modes:**
1. **Single-agent** (faster) — Claude scores all 4 dimensions at once
2. **Parallel-agent** (thorough) — 4 agents score one dimension each in parallel, with extended thinking and memory comparison

**Two Offboarding Modes:**
1. **Single-flow** (interactive CLI) — full flow with inline confirmation prompt
2. **Two-phase** (APIs, web UIs) — separate assess/flag → human review → deactivate

See [Skills Architecture](#skills-architecture) below for details.

---

## Documentation

For detailed explanations of the AI/Claude concepts implemented in this project, see **[docs/ai-concepts.md](docs/ai-concepts.md)**. It covers:

- **Evals** — end-to-end testing methodology
- **Tool Use** — function calling and MCP
- **Agentic Loops** — how Claude iterates until it decides it's done
- **Skills** — system prompt engineering and composability
- **MCP (Model Context Protocol)** — tool schema definition and dispatch
- **LLM-as-Judge** — structured output via `tool_choice`
- **Multi-turn Conversations** — stateful context across rounds
- **Prompt Caching** — token-efficient API calls
- **Streaming** — real-time token delivery
- **Parallel Agents** — concurrent dimension scoring
- **Extended Thinking** — step-by-step reasoning
- **Memory & Persistence** — cross-session risk comparisons
- **Human-in-the-Loop** — two-phase offboarding with confirmation gates
- **Orchestration Patterns** — single-shot, convergence loop, critic-revise, parallel risk
- **Client Options 1–9** — mapping from user choice → skills → tools → flow pattern

---

## Setup

### 1. Install dependencies
#### For the project to be visible system-wide 
```bash
pip install -r mcp-server/requirements.txt
```
OR
#### To install in a local env 
```bash
  python -m venv path/to/venv
  source path/to/venv/bin/activate
  pip install -r mcp-server/requirements.txt
  pip install -r orchestrator/requirements.txt
```

### 2. Seed the database

```bash
python seed/seed.py
```

### 3. Model access

`flows/llm_client.py` picks the model backend based on the `LLM_PROVIDER` env var. Two options:

#### Option A — Direct Anthropic API (default)

Used when `LLM_PROVIDER` is unset or anything other than `bedrock`. Reads `flows/anthropic_client.py`, model default `claude-sonnet-4-6` (override with `ANTHROPIC_MODEL_ID`).

Authenticate with one of:

- **API key** — get one from `console.anthropic.com`, then set:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  ```
  To be safe, export the key in every window/console. (MCP server and client does not need it. But it does not hurt. Tests need it and Orchestrator too.)

- **Your Claude account (OAuth)** — install the `ant` CLI, then:
  ```bash
  ant auth login
  ```
  This opens a browser to sign in and stores a profile locally — no env var needed. Don't set `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` alongside this; either one takes precedence over the OAuth profile and will shadow it.

#### Option B — AWS Bedrock

Set `LLM_PROVIDER=bedrock` to route through `flows/bedrock_client.py` instead:

```bash
export LLM_PROVIDER=bedrock
```

Add your AWS credentials to `~/.aws/credentials` under the `default` profile with Bedrock access to `us.anthropic.claude-sonnet-4-6` in `us-west-2`. Override the model with `BEDROCK_MODEL_ID`.

### 4. Sampling temperature

`flows/llm_client.py` also sets two `temperature` values, both defaulting to `0` (deterministic — same input, same output):

| Env var | Default | Applies to |
|---|---|---|
| `LLM_TEMPERATURE` | `0` | Main agentic loop — tool selection, risk scoring, write-ups |
| `LLM_JUDGE_TEMPERATURE` | `0` | Completeness judge (`_check_completeness`) and critic (`_critique_response`) |

Raise these if you want more varied phrasing across runs; leave them at `0` for reproducible tool-call sequences and stable eval assertions. One exception: `run_dimension_agent` never sets `temperature` when extended thinking is on (option 8/9) — the API requires `temperature=1` whenever `thinking` is enabled. See `docs/improvements/temperature-determinism.md`.

---

## Running the code

There are three ways to run this project.

---

### Mode 1 — All-in-one command-line app

The simplest way. Starts the MCP server automatically as a subprocess and presents an interactive menu. No other services need to be running.

```bash
python -m venv path/to/venv
source path/to/venv/bin/activate
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

## Skills Architecture

### Main Skills (User-Facing)

| Skill | Purpose | Dependencies |
|-------|---------|--------------|
| `_base` | Shared error handling, output format, safety rules | — |
| `lookup-user` | Fetch and summarize user records by ID or email | `_base` |
| `user-risk-profile` | Assess user risk on 0–15 point scale (single-agent mode) | `_base`, `lookup-user` |
| `offboard-user` | Full offboarding flow: lookup → risk → flag → confirm → deactivate | `_base`, `lookup-user`, `user-risk-profile` |

### Sub-Skills (Internal, Used by Parallel Agents)

The framework supports **two modes** for risk assessment:

#### Mode 1: Single-Agent Risk (Default)

**Skills loaded:** `["_base", "lookup-user", "user-risk-profile"]`

Claude scores all 4 dimensions in one pass within the `user-risk-profile` skill.

**When to use:**
- CLI options 1–6 (single-shot, convergence, critic-revise)
- Interactive mode — fast response needed
- Simple assessments without extended reasoning

---

#### Mode 2: Parallel-Agent Risk (Options 7–9)

**Skills loaded:** `["_base"] + per-agent: [risk-auth, risk-permissions, risk-behaviour, risk-account]`

Runs 4 independent agents in parallel. Each agent:
1. Loads a focused sub-skill
2. Fetches data independently
3. Calls `report_dimension_score` to contribute its dimension score

| Agent | Sub-Skill | Scores | Max Points |
|-------|-----------|--------|-----------|
| Agent 1 | `risk-auth` | Authentication (MFA, failed logins, IP diversity, dormancy) | 6 |
| Agent 2 | `risk-permissions` | Permissions (admin access, sensitive resources, contractor status) | 5 |
| Agent 3 | `risk-behaviour` | Behaviour (failure rate, sensitive access, off-hours activity) | 4 |
| Agent 4 | `risk-account` | Account (flagged status, contractor, age) | 3 |

**Extended thinking & memory (Options 8–9):**
- Each agent can use extended thinking for deeper reasoning
- System compares against prior assessments from DB (`get_prior_assessment`)
- Outputs "Change Since Prior Assessment" section

**When to use:**
- CLI options 7, 8, 9 (parallel ± extended thinking ± memory)
- When you want thorough, independent analysis of each dimension
- When you want to track risk changes over time
- When you want agent reasoning shown in output

### Offboarding: Two Modes

Similar to risk assessment, offboarding supports two modes:

#### Mode 1: Single-Flow Offboarding (Interactive CLI)

**Skills loaded:** `["_base", "lookup-user", "user-risk-profile", "offboard-user"]`

**All 5 steps in one flow:**

1. **Lookup** — fetch user details, activity, permissions
2. **Risk Assessment** — score across 4 dimensions (0–15 scale)
3. **Pre-deactivation Flag** — create audit trail
4. **Confirmation Prompt** — present summary, **block until user types CONFIRM**
5. **Deactivate** — execute if confirmed, output completion summary

**When to use:**
- CLI options 1–6, Mode 1 flow runner
- Interactive mode — user is present and can type confirmation
- Simple, immediate offboarding (all-in-one transaction)

---

#### Mode 2: Two-Phase Human-in-the-Loop (API/Web/Orchestrator)

**Phase 1: Prepare (Assessment)**

**Skills loaded:** `["_base", "lookup-user", "user-risk-profile", "offboard-prepare"]`

**Steps:**
1. **Lookup** — fetch user details, activity, permissions
2. **Risk Assessment** — score across 4 dimensions (0–15 scale)
3. **Pre-deactivation Flag** — create audit trail
4. **Output Summary** — return structured report for human review
5. **STOP** — do NOT ask for confirmation, do NOT deactivate

**Output:** Risk assessment summary, permissions, last login, recommendation (human reviews this)

---

**Phase 2: Confirm (Execution)**

**Skills loaded:** `["_base", "offboard-confirm"]`

**Assumptions:**
- Phase 1 was already completed
- User lookup and risk assessment already done
- Account already flagged
- Human has already reviewed and approved deactivation

**Step:**
1. **Deactivate** — execute `deactivate_user(user_id, reason)` only
2. **Output completion summary**

**When to use:**
- REST APIs, web dashboards, orchestrator service
- Slack bots, email workflows
- Any flow where assessment and approval are separated in time
- Any flow where the confirmation decision happens outside of Claude's code

**Why separate Phase 1 and Phase 2?**
- **Safety**: Human reviews before anything destructive happens
- **Audit trail**: Assessment is recorded before execution
- **Flexibility**: Approval can happen hours/days later, in a different system
- **Retry safety**: If Phase 2 fails, you can retry without re-assessing

---

### Mode 3 — Separate services (MCP server + orchestrator + client)

Run each component independently. This is useful for debugging, IDE integration, or deploying components separately.

**Terminal 1 — MCP server (HTTP mode):**

```bash
python -m venv path/to/venv
source path/to/venv/bin/activate
python mcp-server/server.py --transport http --port 8001
```

Expected output:

```
MCP server starting on http://0.0.0.0:8001
```

**Terminal 2 — Orchestrator:**

```bash
python -m venv path/to/venv
source path/to/venv/bin/activate
MCP_URL=http://localhost:8001 python orchestrator/app.py --port 8000
```

**Terminal 3 — Client:**

```bash
python -m venv path/to/venv
source path/to/venv/bin/activate
python client/cli.py
```

The client presents the same interactive menu as Mode 1. To point at a different orchestrator host:

```bash
python -m venv path/to/venv
source path/to/venv/bin/activate
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
python -m venv path/to/venv
source path/to/venv/bin/activate
python tests/test_flows.py
```

Tests verify which tools were called, what the response contains, and that safety rules were followed (flag before deactivate, no writes without a reason).
