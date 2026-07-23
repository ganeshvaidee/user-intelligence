# seed/seed.py
# Creates and populates the SQLite database with realistic test data

import sqlite3
import os
from datetime import datetime, timedelta
import random
from pathlib import Path

DB_PATH = Path(__file__).parent / "users.db"


def create_schema(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS user_permissions;
        DROP TABLE IF EXISTS permissions;
        DROP TABLE IF EXISTS activity_log;
        DROP TABLE IF EXISTS audit_log;
        DROP TABLE IF EXISTS users;

        CREATE TABLE IF NOT EXISTS assessments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            total_score INTEGER,
            max_score   INTEGER,
            risk_level  TEXT,
            auth_score  INTEGER,
            perms_score INTEGER,
            behav_score INTEGER,
            acct_score  INTEGER,
            summary     TEXT
        );

        CREATE TABLE users (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            department    TEXT,
            role          TEXT,
            status        TEXT DEFAULT 'active',   -- active | flagged | inactive
            created_at    TEXT,
            last_login    TEXT,
            mfa_enabled   INTEGER DEFAULT 0,
            employee_type TEXT DEFAULT 'full-time' -- full-time | contractor | vendor
        );

        CREATE TABLE permissions (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            resource TEXT NOT NULL,
            level    TEXT NOT NULL   -- read | write | admin
        );

        CREATE TABLE user_permissions (
            user_id       TEXT,
            permission_id INTEGER,
            granted_at    TEXT,
            granted_by    TEXT,
            PRIMARY KEY (user_id, permission_id)
        );

        CREATE TABLE activity_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT,
            action     TEXT,
            resource   TEXT,
            timestamp  TEXT,
            ip_address TEXT,
            success    INTEGER DEFAULT 1
        );

        CREATE TABLE audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT,
            action       TEXT,
            reason       TEXT,
            performed_by TEXT,
            timestamp    TEXT
        );
    """)


def seed_permissions(conn):
    permissions = [
        ("read-users",         "user-data",    "read"),
        ("write-users",        "user-data",    "write"),
        ("admin-users",        "user-data",    "admin"),
        ("read-billing",       "billing",      "read"),
        ("write-billing",      "billing",      "write"),
        ("read-prod-db",       "prod-database","read"),
        ("write-prod-db",      "prod-database","write"),
        ("admin-prod-db",      "prod-database","admin"),
        ("read-secrets",       "secrets",      "read"),
        ("write-secrets",      "secrets",      "write"),
        ("read-reports",       "reports",      "read"),
        ("write-reports",      "reports",      "write"),
        ("access-admin-panel", "admin-panel",  "admin"),
        ("read-logs",          "logs",         "read"),
        ("deploy-prod",        "prod-infra",   "write"),
    ]
    conn.executemany(
        "INSERT INTO permissions (name, resource, level) VALUES (?, ?, ?)",
        permissions
    )


def seed_users(conn):
    users = [
        # Normal active users
        ("usr_001", "Alice Chen",      "alice@company.com",    "Engineering",  "Senior Engineer",  "active",   1, "full-time"),
        ("usr_002", "Bob Martinez",    "bob@company.com",      "Engineering",  "Engineer",         "active",   1, "full-time"),
        ("usr_003", "Carol White",     "carol@company.com",    "Finance",      "Analyst",          "active",   1, "full-time"),
        ("usr_004", "David Kim",       "david@company.com",    "HR",           "Manager",          "active",   0, "full-time"),
        # Suspicious: contractor with broad access, no MFA, unusual activity
        ("usr_005", "Eve Contractor",  "eve@vendor.com",       "Engineering",  "Contractor",       "active",   0, "contractor"),
        # Dormant account
        ("usr_006", "Frank Old",       "frank@company.com",    "Engineering",  "Engineer",         "active",   0, "full-time"),
        # Already flagged
        ("usr_007", "Grace Flagged",   "grace@company.com",    "Sales",        "Rep",              "flagged",  1, "full-time"),
        # Inactive
        ("usr_008", "Henry Inactive",  "henry@company.com",    "Engineering",  "Engineer",         "inactive", 1, "full-time"),
    ]

    now = datetime.now()

    for uid, name, email, dept, role, status, mfa, emp_type in users:
        # Frank hasn't logged in for 180 days (dormant)
        if uid == "usr_006":
            last_login = (now - timedelta(days=180)).isoformat()
        # Henry inactive - old login
        elif uid == "usr_008":
            last_login = (now - timedelta(days=400)).isoformat()
        else:
            last_login = (now - timedelta(days=random.randint(0, 7))).isoformat()

        conn.execute(
            """INSERT INTO users
               (id, name, email, department, role, status, created_at, last_login, mfa_enabled, employee_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, name, email, dept, role, status,
             (now - timedelta(days=random.randint(30, 730))).isoformat(),
             last_login, mfa, emp_type)
        )


def seed_user_permissions(conn):
    # Alice — normal senior engineer permissions
    for perm_id in [1, 7, 11, 14]:   # read-users, write-prod-db, read-reports, read-logs
        conn.execute(
            "INSERT INTO user_permissions VALUES (?, ?, ?, ?)",
            ("usr_001", perm_id, datetime.now().isoformat(), "admin")
        )

    # Bob — normal engineer
    for perm_id in [1, 6, 11]:       # read-users, read-prod-db, read-reports
        conn.execute(
            "INSERT INTO user_permissions VALUES (?, ?, ?, ?)",
            ("usr_002", perm_id, datetime.now().isoformat(), "admin")
        )

    # Carol — finance read access
    for perm_id in [4, 11]:          # read-billing, read-reports
        conn.execute(
            "INSERT INTO user_permissions VALUES (?, ?, ?, ?)",
            ("usr_003", perm_id, datetime.now().isoformat(), "admin")
        )

    # Eve (contractor) — suspiciously broad permissions, no MFA
    for perm_id in [2, 5, 8, 9, 13, 15]:  # write-users, write-billing, admin-prod-db, read-secrets, admin-panel, deploy-prod
        conn.execute(
            "INSERT INTO user_permissions VALUES (?, ?, ?, ?)",
            ("usr_005", perm_id, datetime.now().isoformat(), "usr_004")
        )

    # Frank — normal but dormant
    for perm_id in [1, 6]:
        conn.execute(
            "INSERT INTO user_permissions VALUES (?, ?, ?, ?)",
            ("usr_006", perm_id, datetime.now().isoformat(), "admin")
        )


def seed_activity(conn):
    now = datetime.now()
    normal_actions = ["login", "view-report", "read-record", "export-data", "update-record"]
    resources      = ["user-data", "reports", "billing", "logs"]

    # Normal activity for most users
    for uid in ["usr_001", "usr_002", "usr_003", "usr_004"]:
        for i in range(random.randint(15, 40)):
            conn.execute(
                "INSERT INTO activity_log (user_id, action, resource, timestamp, ip_address, success) VALUES (?,?,?,?,?,?)",
                (uid,
                 random.choice(normal_actions),
                 random.choice(resources),
                 (now - timedelta(hours=random.randint(1, 720))).isoformat(),
                 f"10.0.0.{random.randint(1,50)}",
                 1)
            )

    # Eve — suspicious: many failures, many different IPs, accessing sensitive resources at odd hours
    suspicious_actions   = ["login", "read-record", "export-data", "read-secrets", "access-admin"]
    suspicious_resources = ["prod-database", "secrets", "user-data", "admin-panel", "billing"]
    for i in range(60):
        success = 0 if i % 4 == 0 else 1   # high failure rate
        conn.execute(
            "INSERT INTO activity_log (user_id, action, resource, timestamp, ip_address, success) VALUES (?,?,?,?,?,?)",
            ("usr_005",
             random.choice(suspicious_actions),
             random.choice(suspicious_resources),
             (now - timedelta(hours=random.randint(1, 168))).isoformat(),
             f"185.{random.randint(100,200)}.{random.randint(0,255)}.{random.randint(0,255)}",  # external IPs
             success)
        )

    # Frank — no activity for 180 days (dormant)
    # (intentionally no recent rows)


def seed_audit_log(conn):
    conn.execute(
        """INSERT INTO audit_log (user_id, action, reason, performed_by, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        ("usr_007", "flagged", "Reported by manager for data export anomaly",
         "admin", (datetime.now() - timedelta(days=2)).isoformat())
    )
    conn.execute(
        """INSERT INTO audit_log (user_id, action, reason, performed_by, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        ("usr_008", "deactivated", "Employee offboarded 2024-01-15",
         "admin", (datetime.now() - timedelta(days=160)).isoformat())
    )


if __name__ == "__main__":
    print(f"Seeding database at {DB_PATH}...")
    with sqlite3.connect(DB_PATH) as conn:
        create_schema(conn)
        seed_permissions(conn)
        seed_users(conn)
        seed_user_permissions(conn)
        seed_activity(conn)
        seed_audit_log(conn)
    print("Done. Users seeded:")
    print("  usr_001  Alice Chen       — normal senior engineer")
    print("  usr_002  Bob Martinez     — normal engineer")
    print("  usr_003  Carol White      — normal finance")
    print("  usr_004  David Kim        — normal HR manager")
    print("  usr_005  Eve Contractor   — ⚠️  suspicious: broad perms, no MFA, odd activity")
    print("  usr_006  Frank Old        — ⚠️  dormant: no login in 180 days")
    print("  usr_007  Grace Flagged    — already flagged")
    print("  usr_008  Henry Inactive   — already inactive")
