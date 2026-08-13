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
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from llm_client import client, MODEL_ID, JUDGE_TEMPERATURE
from usage import log_usage

MCP_SERVER = Path(__file__).parent.parent / "mcp-server" / "server.py"
MCP_URL    = os.environ.get("MCP_URL")   # set to use HTTP mode, e.g. http://localhost:8001

logger = logging.getLogger(__name__)


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
        "description": "Fetch a user record by ID (returns: name, email, department, role, status, last_login, mfa_enabled)",
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
        "description": "Look up a user by email address",
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
        "description": "Get recent activity log for a user (returns: action, resource, timestamp, ip_address, success)",
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
        "description": "Get all permissions assigned to a user (with level: read/write/admin)",
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
        "description": "Get audit log of administrative actions on a user account",
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
        "description": "Flag a user account for review (requires: reason). Account remains active.",
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
        "description": "Permanently deactivate a user account (requires: reason). Blocks all logins.",
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
        "description": "Get the most recent saved risk assessment for a user",
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
#
# No prompt caching on either judge — deliberately, and measured rather than
# assumed.
#
# THE BINDING REASON — the prefix is below the minimum. The only cacheable
# content in a judge call is the tool schema plus the system prompt: ~180 tokens,
# against a 1024-token minimum on Sonnet 4.6. Below that minimum the API silently
# declines to cache — no error, no warning, just full price. Measured with
# flows/usage.py on a critic-revise run:
#
#     [USAGE] _critique_response  in=1440  cache_w=0  cache_r=0  out=980
#
# The other ~1260 tokens are the assessment under review — per-call content that
# sits after any breakpoint, so no static text remains to push the prefix over
# the threshold. Note what is NOT the problem: that prefix is byte-identical on
# every call, forever, across iterations and flows and users. Structurally this
# is a textbook caching candidate. Size alone rules it out, and no breakpoint
# placement fixes that.
#
# A SECONDARY NOTE, not independently disqualifying. _critique_response runs
# exactly once per flow (phase 2 of run_flow_with_reflection), so within a single
# run nothing reads what that call would have written, and the 1.25x write
# premium would be pure loss. Across runs it differs: the cache lives 5 minutes
# and the prefix never changes, so two runs inside that window would hit —
# break-even is roughly two calls (1.25x + 0.1x = 1.35x, versus 2.0x uncached).
# _check_completeness is a stronger candidate still, running once per convergence
# round (0-2 times at max_rounds=3) and able to pay off inside a single flow.
#
# That ranks the two judges against each other; it is not why either is uncached.
# If the prefix were 2000 tokens instead of 180, both would get a breakpoint.
#
# What was removed, for reference — this is what the no-op looked like:
#
#     system = [{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]
#
# Well-formed, passes review, caches nothing. Contrast _run_tool_loop in
# run_flow.py, where the skills system prompt is ~2296 tokens and the same
# pattern produces real cache hits. See docs/improvements/prompt-caching.md.


def _first_tool_input(result, default: dict) -> dict:
    """
    Pull the judge's verdict out of its forced tool_use block.

    tool_choice={"type": "any"} makes a text-only reply unlikely but not
    impossible — a max_tokens truncation, a refusal, or a provider-side stop
    can all end a turn with no complete tool_use block. Bare next() would raise
    StopIteration there, which inside a coroutine surfaces as an opaque
    RuntimeError and takes down the whole flow.

    Fail open instead: a missing verdict means "no judge this round", never
    "throw away the response Claude already produced".

    The caller's `default` should carry "judge_unavailable": True so the
    degraded verdict stays distinguishable from a genuine one. Without that
    tag a fail-open result is byte-identical to a real pass, and callers end
    up reporting a verdict that was never given.
    """
    block = next((b for b in result.content if b.type == "tool_use"), None)
    if block is None:
        logger.warning(
            "Judge returned no tool_use block (stop_reason=%s) — failing open with %s",
            getattr(result, "stop_reason", "unknown"), default,
        )
        return default
    return block.input


async def _check_completeness(original_request: str, response: str) -> dict:
    """Ask the LLM whether a response fully covers the original request."""
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 1024,   # 512 could truncate a long `missing`/`issues` list mid-block
        temperature = JUDGE_TEMPERATURE,
        system      = "You are a quality checker for user intelligence assessments. Be precise and critical.",
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
    # Fail open as "complete" — a lost verdict must not force an extra round.
    # judge_unavailable marks this as "not actually checked", so callers don't
    # report it as a passed completeness check.
    log_usage(result, "_check_completeness", cached=False)
    return _first_tool_input(
        result, {"complete": True, "missing": [], "judge_unavailable": True}
    )


async def _critique_response(original_request: str, response: str) -> dict:
    """Ask the LLM to critique an assessment for errors and unjustified claims."""
    result = await client.messages.create(
        model       = MODEL_ID,
        max_tokens  = 1024,   # 512 could truncate a long `missing`/`issues` list mid-block
        temperature = JUDGE_TEMPERATURE,
        system      = "You are a critical reviewer of user intelligence risk assessments. Check that risk scores are justified by the evidence shown. Flag any score inflation, unsupported conclusions, or missing caveats.",
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
    # Fail open as "no issues" — a lost verdict must not trigger a revision pass.
    # judge_unavailable marks this as "not actually reviewed", so callers don't
    # report it as a clean critique.
    log_usage(result, "_critique_response", cached=False)
    return _first_tool_input(
        result, {"has_issues": False, "issues": [], "judge_unavailable": True}
    )
