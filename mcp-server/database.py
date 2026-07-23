# mcp-server/database.py
# SQLite database layer — the actual data store the MCP server queries

import sqlite3
import os
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "seed" / "users.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows as dicts
    return conn


# ── User queries ──────────────────────────────────────────────────

def fetch_user(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, email, department, role, status,
                   created_at, last_login, mfa_enabled, employee_type
            FROM users
            WHERE id = ?
            """,
            (user_id,)
        ).fetchone()
        return dict(row) if row else None


def fetch_user_by_email(email: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return dict(row) if row else None


def fetch_user_activity(user_id: str, days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT action, resource, timestamp, ip_address, success
            FROM activity_log
            WHERE user_id = ?
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 100
            """,
            (user_id, since)
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_user_permissions(user_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.name, p.resource, p.level, up.granted_at, up.granted_by
            FROM permissions p
            JOIN user_permissions up ON p.id = up.permission_id
            WHERE up.user_id = ?
            ORDER BY p.level DESC
            """,
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Mutation queries ──────────────────────────────────────────────

def flag_user_record(user_id: str, reason: str, flagged_by: str = "system") -> bool:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users SET status = 'flagged' WHERE id = ?
            """,
            (user_id,)
        )
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, reason, performed_by, timestamp)
            VALUES (?, 'flagged', ?, ?, ?)
            """,
            (user_id, reason, flagged_by, datetime.now().isoformat())
        )
        return True


def deactivate_user_record(user_id: str, reason: str, performed_by: str = "system") -> bool:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET status = 'inactive' WHERE id = ?",
            (user_id,)
        )
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, reason, performed_by, timestamp)
            VALUES (?, 'deactivated', ?, ?, ?)
            """,
            (user_id, reason, performed_by, datetime.now().isoformat())
        )
        return True


def fetch_audit_log(user_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT action, reason, performed_by, timestamp
            FROM audit_log
            WHERE user_id = ?
            ORDER BY timestamp DESC
            """,
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Assessment memory ─────────────────────────────────────────────

def fetch_prior_assessment(user_id: str) -> dict | None:
    """Return the most recent saved assessment for a user, or None."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, assessed_at, total_score, max_score, risk_level,
                   auth_score, perms_score, behav_score, acct_score, summary
            FROM assessments
            WHERE user_id = ?
            ORDER BY assessed_at DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
        return dict(row) if row else None


def save_assessment_record(
    user_id: str, total_score: int, max_score: int, risk_level: str,
    auth_score: int, perms_score: int, behav_score: int, acct_score: int,
    summary: str,
) -> bool:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO assessments
                (user_id, assessed_at, total_score, max_score, risk_level,
                 auth_score, perms_score, behav_score, acct_score, summary)
            VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, total_score, max_score, risk_level,
             auth_score, perms_score, behav_score, acct_score, summary)
        )
        return True
