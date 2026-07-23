# mcp-server/server.py
#
# This is the MCP server. It exposes database operations as tools
# that Claude can call. Claude never talks to the DB directly —
# it always goes through these tool definitions.
#
# Run with: python mcp-server/server.py
# Claude Desktop config (claude_desktop_config.json):
#
# {
#   "mcpServers": {
#     "user-intelligence": {
#       "command": "python",
#       "args": ["/path/to/user-intelligence/mcp-server/server.py"]
#     }
#   }
# }

from pathlib import Path

from fastmcp import FastMCP
from database import (
    fetch_user,
    fetch_user_by_email,
    fetch_user_activity,
    fetch_user_permissions,
    flag_user_record,
    deactivate_user_record,
    fetch_audit_log,
    fetch_prior_assessment,
    save_assessment_record,
)

# ── Create the MCP server ─────────────────────────────────────────

mcp = FastMCP(name="user-intelligence")

SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _load_skills(*skill_names: str) -> str:
    """Read SKILL.md files from the project skills/ directory and combine them."""
    parts = []
    for name in skill_names:
        content = (SKILLS_DIR / name / "SKILL.md").read_text()
        if content.startswith("---"):
            end = content.index("---", 3) + 3
            content = content[end:].strip()
        parts.append(f"# SKILL: {name}\n\n{content}")
    return "\n\n---\n\n".join(parts)


# ── Prompts ───────────────────────────────────────────────────────
# These appear in Claude Desktop's prompt picker.
# Each one loads the relevant SKILL.md files from skills/ — no duplication.
# Select a prompt before asking a question to load the skill instructions.

@mcp.prompt()
def lookup_user() -> str:
    """Fetch and summarise a user record."""
    return _load_skills("_base", "lookup-user")


@mcp.prompt()
def risk_assessment() -> str:
    """Assess a user's risk on a 0–15 point scale across auth, permissions, and behaviour."""
    return _load_skills("_base", "lookup-user", "user-risk-profile")


@mcp.prompt()
def offboard_user() -> str:
    """Full offboarding flow: lookup → risk assessment → flag → confirm → deactivate."""
    return _load_skills("_base", "lookup-user", "user-risk-profile", "offboard-user")


# ── READ tools ────────────────────────────────────────────────────

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


@mcp.tool()
def find_user_by_email(email: str) -> dict:
    """
    Look up a user by their email address.
    Useful when you have an email but not a user ID.
    """
    user = fetch_user_by_email(email)
    if not user:
        return {"error": f"No user found with email '{email}'"}
    return user


@mcp.tool()
def get_user_activity(user_id: str, days: int = 30) -> dict:
    """
    Get recent activity log for a user.
    Returns up to 100 events from the last N days (default 30).
    Each event has: action, resource, timestamp, ip_address, success.
    """
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}

    activity = fetch_user_activity(user_id, days)
    
    # Compute summary stats for the skill to use
    total      = len(activity)
    failures   = sum(1 for a in activity if not a["success"])
    unique_ips = len(set(a["ip_address"] for a in activity))
    
    return {
        "user_id":    user_id,
        "days":       days,
        "total":      total,
        "failures":   failures,
        "unique_ips": unique_ips,
        "events":     activity[:20],   # return first 20 for context
        "truncated":  total > 20
    }


@mcp.tool()
def get_user_permissions(user_id: str) -> dict:
    """
    Get all permissions assigned to a user.
    Returns list of permissions with name, resource, level (read/write/admin).
    High-level permissions (admin/write to sensitive resources) are flagged.
    """
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}

    permissions = fetch_user_permissions(user_id)

    sensitive   = ["prod-database", "user-data", "billing", "secrets", "admin-panel"]
    high_risk   = [
        p for p in permissions
        if p["level"] in ("admin", "write") and p["resource"] in sensitive
    ]

    return {
        "user_id":          user_id,
        "total":            len(permissions),
        "high_risk_count":  len(high_risk),
        "high_risk":        high_risk,
        "all_permissions":  permissions
    }


@mcp.tool()
def get_audit_log(user_id: str) -> dict:
    """
    Get the audit log of administrative actions taken on a user account
    (flags, deactivations, permission changes).
    """
    log = fetch_audit_log(user_id)
    return {"user_id": user_id, "entries": log}


# ── WRITE tools ───────────────────────────────────────────────────

@mcp.tool()
def flag_user(user_id: str, reason: str) -> dict:
    """
    Flag a user account for review. Sets status to 'flagged'.
    Does NOT deactivate the account — user can still log in.
    Use this when suspicious activity is detected but not confirmed.
    Always provide a clear reason.
    """
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}
    if user["status"] == "inactive":
        return {"error": "Cannot flag an already inactive user"}

    flag_user_record(user_id, reason)
    return {
        "success":   True,
        "user_id":   user_id,
        "new_status": "flagged",
        "reason":    reason
    }


@mcp.tool()
def deactivate_user(user_id: str, reason: str) -> dict:
    """
    Permanently deactivate a user account. Sets status to 'inactive'.
    This blocks all future logins. Use only after confirmation.
    Always provide a clear reason for the audit log.
    """
    user = fetch_user(user_id)
    if not user:
        return {"error": f"User '{user_id}' not found"}
    if user["status"] == "inactive":
        return {"error": "User is already inactive"}

    deactivate_user_record(user_id, reason)
    return {
        "success":    True,
        "user_id":    user_id,
        "new_status": "inactive",
        "reason":     reason
    }


# ── Assessment memory tools ───────────────────────────────────────

@mcp.tool()
def get_prior_assessment(user_id: str) -> dict:
    """
    Return the most recent saved risk assessment for a user.
    Returns the assessment dict (with scores, level, summary, date) if found,
    or {"none": true} if no prior assessment exists for this user.
    """
    prior = fetch_prior_assessment(user_id)
    if not prior:
        return {"none": True}
    return prior


@mcp.tool()
def save_assessment(
    user_id: str, total_score: int, max_score: int, risk_level: str,
    auth_score: int, perms_score: int, behav_score: int, acct_score: int,
    summary: str,
) -> dict:
    """
    Save a completed risk assessment for future comparison.
    Stores the total score, per-dimension scores, risk level, and a one-sentence summary.
    """
    save_assessment_record(
        user_id, total_score, max_score, risk_level,
        auth_score, perms_score, behav_score, acct_score, summary,
    )
    return {"success": True, "user_id": user_id}


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="User Intelligence MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="Transport mode (default: stdio for Claude Desktop / subprocess use)"
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001, help="HTTP port (default: 8001)")
    args = parser.parse_args()

    if args.transport == "http":
        print(f"MCP server starting on http://{args.host}:{args.port}", flush=True)
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
