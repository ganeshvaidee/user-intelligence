#!/usr/bin/env python3
"""
Regression tests for the two-phase (human-in-the-loop) offboarding flow.

Why these live outside tests/test_flows.py
------------------------------------------
`run_test_flow()` in test_flows.py reimplements the agentic loop and does NOT
apply ORDER_REQUIREMENTS. Any test written against it passes whether or not the
order guard works, which is exactly how the phase-2 deadlock shipped unnoticed:
deactivate_user required flag_user, the guard tracked only the *current*
conversation, and phase 2 is a new one — so the confirm phase could never
deactivate anything, and the suite stayed green.

These tests drive the real run_flow_offboard_* functions so the guard is
actually exercised.

Run:  python tests/test_offboard_hitl.py
Needs live model credentials and the seeded DB (python seed/seed.py).
"""

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

from run_flow import run_flow_offboard_prepare, run_flow_offboard_confirm  # noqa: E402

DB = ROOT / "seed" / "users.db"


def status(user_id: str) -> str:
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else "<missing>"


def audit_actions(user_id: str) -> list[str]:
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    return [r[0] for r in rows]


def reseed() -> None:
    subprocess.run([sys.executable, str(ROOT / "seed" / "seed.py")],
                   capture_output=True, check=True)


# ── Tests ─────────────────────────────────────────────────────────

async def test_confirm_deactivates_a_flagged_user() -> tuple[bool, str]:
    """The regression itself: phase 2 must actually deactivate."""
    reseed()
    before = status("usr_007")          # seeded as already flagged
    if before != "flagged":
        return False, f"fixture drift: usr_007 should start 'flagged', got '{before}'"

    await run_flow_offboard_confirm("usr_007", "contract ended", verbose=False)

    after = status("usr_007")
    if after != "inactive":
        return False, (
            f"phase 2 did not deactivate — status '{after}'. If this says 'flagged', "
            f"the order guard blocked deactivate_user because flag_user never ran in "
            f"this conversation; run_flow_offboard_confirm must seed completed from DB state."
        )
    return True, "flagged -> inactive"


async def test_confirm_refuses_an_unflagged_user() -> tuple[bool, str]:
    """The guard must still hold: no flag, no deactivation."""
    reseed()
    out = await run_flow_offboard_confirm("usr_002", "test", verbose=False)

    after = status("usr_002")
    if after != "active":
        return False, f"unflagged user was modified — status '{after}'"
    if "refus" not in out.lower():
        return False, f"expected an explicit refusal, got: {out[:120]!r}"
    return True, "refused, DB untouched"


async def test_confirm_on_inactive_user_is_a_noop() -> tuple[bool, str]:
    reseed()
    out = await run_flow_offboard_confirm("usr_008", "test", verbose=False)
    if "already inactive" not in out.lower():
        return False, f"expected an already-inactive message, got: {out[:120]!r}"
    return True, "clean no-op"


async def test_full_two_phase_flow() -> tuple[bool, str]:
    """prepare flags but does not deactivate; confirm then deactivates."""
    reseed()
    await run_flow_offboard_prepare("usr_005", "contractor contract ended", verbose=False)
    mid = status("usr_005")
    if mid != "flagged":
        return False, f"after phase 1 expected 'flagged', got '{mid}'"

    await run_flow_offboard_confirm("usr_005", "contractor contract ended", verbose=False)
    end = status("usr_005")
    if end != "inactive":
        return False, f"after phase 2 expected 'inactive', got '{end}'"

    actions = audit_actions("usr_005")
    if actions != ["flagged", "deactivated"]:
        return False, f"audit trail wrong: {actions}"
    return True, "active -> flagged -> inactive, audit trail intact"


TESTS = [
    test_confirm_deactivates_a_flagged_user,
    test_confirm_refuses_an_unflagged_user,
    test_confirm_on_inactive_user_is_a_noop,
    test_full_two_phase_flow,
]


async def main() -> int:
    print("\nHITL Offboarding — regression tests")
    print("=" * 60)
    passed = 0
    for i, fn in enumerate(TESTS, 1):
        print(f"\n[{i}/{len(TESTS)}] {fn.__name__} ...", end=" ", flush=True)
        try:
            ok, detail = await fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        print("PASS" if ok else "FAIL")
        print(f"    {detail}")
        passed += ok

    reseed()
    print(f"\n{'='*60}\nResults: {passed}/{len(TESTS)} passed")
    print("🟢 All passed" if passed == len(TESTS) else "🔴 Failures above")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
