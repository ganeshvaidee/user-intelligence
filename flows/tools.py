# flows/tools.py
#
# Two things live here:
#   1. Tool schemas  — what Claude sees (USER_TOOLS, plus judge schemas for
#                      the completeness/critique LLM calls)
#   2. Tool callers  — Python that executes those schemas
#                      (execute_tool routes calls through the MCP server;
#                       _check_completeness / _critique_response make
#                       structured LLM calls and return parsed dicts)

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from llm_client import client, MODEL_ID

MCP_SERVER = Path(__file__).parent.parent / "mcp-server" / "server.py"
MCP_URL    = os.environ.get("MCP_URL")   # set to use HTTP mode, e.g. http://localhost:8001


# ── MCP session lifecycle ─────────────────────────────────────────

@asynccontextmanager
async def start_mcp_session():
    """
    Yield an initialised MCP session using the right transport:
      - MCP_URL set  → HTTP (connect to a running server)
      - MCP_URL unset → stdio (spawn a subprocess)

    Either way the session interface is identical — callers never need to
    know which transport is in use.
    """
    if MCP_URL:
        async with streamablehttp_client(f"{MCP_URL}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER)],
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


# ── Per-skill tool visibility ────────────────────────────────────

SKILL_TOOLS: dict[str, set[str]] = {
    "_base":              set(),
    "lookup-user":        {"get_user", "find_user_by_email", "get_user_activity", "get_user_permissions"},
    "user-risk-profile":  {"get_user_activity", "get_audit_log"},
    "offboard-user":      {"flag_user", "deactivate_user"},
    "offboard-prepare":   {"flag_user"},
    "offboard-confirm":   {"deactivate_user"},
    "risk-auth":          {"get_user", "get_user_activity"},
    "risk-permissions":   {"get_user", "get_user_permissions"},
    "risk-behaviour":     {"get_user_activity"},
    "risk-account":       {"get_user", "get_audit_log"},
}


def tools_for_skills(skill_names: list[str]) -> list[dict]:
    """Union the tool sets of every loaded skill, filtered against USER_TOOLS.
    Unknown skill names contribute nothing extra (fail open to empty set)."""
    allowed = set()
    for name in skill_names:
        allowed |= SKILL_TOOLS.get(name, set())
    return [t for t in USER_TOOLS if t["name"] in allowed]


ORDER_REQUIREMENTS: dict[str, list[str]] = {
    "flag_user":       ["get_user_activity"],
    "deactivate_user": ["flag_user"],
}


# ── User intelligence tool schemas ────────────────────────────────────

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
    {
        "name": "find_user_by_email",
        "description": "Look up a user by their email address. Useful when you have an email but not a user ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "The email address to search for"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "get_user_activity",
        "description": (
            "Get recent activity log for a user. "
            "Returns up to 100 events from the last N days (default 30). "
            "Each event has: action, resource, timestamp, ip_address, success."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "days":    {"type": "integer", "description": "Number of days to look back", "default": 30},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "get_user_permissions",
        "description": (
            "Get all permissions assigned to a user. "
            "Returns list of permissions with name, resource, level (read/write/admin). "
            "High-risk permissions (admin/write to sensitive resources) are flagged."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "get_audit_log",
        "description": "Get the audit log of administrative actions taken on a user account (flags, deactivations, permission changes).",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "flag_user",
        "description": (
            "Flag a user account for review. Sets status to 'flagged'. "
            "Does NOT deactivate the account — user can still log in. "
            "Use when suspicious activity is detected but not confirmed. "
            "Always provide a clear reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reason":  {"type": "string", "description": "Clear reason for flagging"},
            },
            "required": ["user_id", "reason"],
        },
    },
    {
        "name": "deactivate_user",
        "description": (
            "Permanently deactivate a user account. Sets status to 'inactive'. "
            "This blocks all future logins. Use only after confirmation. "
            "Always provide a clear reason for the audit log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reason":  {"type": "string", "description": "Clear reason for deactivation"},
            },
            "required": ["user_id", "reason"],
        },
    },
    {
        "name": "get_prior_assessment",
        "description": (
            "Return the most recent saved risk assessment for a user. "
            "Returns {none: true} if no prior assessment exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "save_assessment",
        "description": "Persist a completed risk assessment for future comparison.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id":     {"type": "string"},
                "total_score": {"type": "integer"},
                "max_score":   {"type": "integer"},
                "risk_level":  {"type": "string"},
                "auth_score":  {"type": "integer"},
                "perms_score": {"type": "integer"},
                "behav_score": {"type": "integer"},
                "acct_score":  {"type": "integer"},
                "summary":     {"type": "string"},
            },
            "required": ["user_id", "total_score", "max_score", "risk_level",
                         "auth_score", "perms_score", "behav_score", "acct_score", "summary"],
        },
    },
]


# ── Judge tool schemas ────────────────────────────────────────────
# tool_choice="any" forces structured output from the LLM judge
# instead of free text, making the result safe to parse as a dict.

_COMPLETENESS_TOOL = {
    "name": "report_completeness",
    "description": "Report whether the response fully addresses the original request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "complete": {
                "type": "boolean",
                "description": "True if the response fully addresses the request with sufficient evidence.",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific data points or checks that are missing or insufficient.",
            },
        },
        "required": ["complete", "missing"],
    },
}

_CRITIQUE_TOOL = {
    "name": "report_critique",
    "description": "Report errors, unsupported claims, or gaps in the assessment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_issues": {
                "type": "boolean",
                "description": "True if there are substantive errors or gaps.",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific errors, unjustified claims, or gaps.",
            },
        },
        "required": ["has_issues", "issues"],
    },
}


# ── MCP tool executor ─────────────────────────────────────────────

async def execute_tool(session: ClientSession, name: str, inputs: dict) -> str:
    """
    Route a Claude tool_use block to the MCP server and return the result
    as a JSON string. The MCP server (server.py → database.py) is the single
    source of truth for tool logic.
    """
    result = await session.call_tool(name, inputs)
    if result.isError:
        error_text = result.content[0].text if result.content else "Unknown MCP error"
        return json.dumps({"error": error_text})
    if not result.content:
        return json.dumps({})
    # FastMCP serialises dict returns as JSON text in TextContent
    return result.content[0].text


# ── LLM judge callers ─────────────────────────────────────────────

async def _check_completeness(original_request: str, response: str) -> dict:
    """Ask the LLM whether a response fully covers the original request."""
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 512,
        system      = [{"type": "text", "text": "You are a quality checker for user intelligence assessments. Be precise and critical.", "cache_control": {"type": "ephemeral"}}],
        tools       = [_COMPLETENESS_TOOL],
        tool_choice = {"type": "any"},
        messages    = [{
            "role":    "user",
            "content": (
                f"Original request: {original_request}\n\n"
                f"Response produced:\n{response}\n\n"
                "Is this response complete? What specific data points were not checked?"
            ),
        }],
    )
    return next(b for b in result.content if b.type == "tool_use").input


async def _critique_response(original_request: str, response: str) -> dict:
    """Ask the LLM to critique an assessment for errors and unjustified claims."""
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 512,
        system      = [{"type": "text", "text": "You are a critical reviewer of user intelligence risk assessments. Check that risk scores are justified by the evidence shown. Flag any score inflation, unsupported conclusions, or missing caveats.", "cache_control": {"type": "ephemeral"}}],
        tools       = [_CRITIQUE_TOOL],
        tool_choice = {"type": "any"},
        messages    = [{
            "role":    "user",
            "content": (
                f"Original request: {original_request}\n\n"
                f"Assessment to review:\n{response}\n\n"
                "Are there errors, unjustified claims, or important gaps?"
            ),
        }],
    )
    return next(b for b in result.content if b.type == "tool_use").input
