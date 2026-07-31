#!/usr/bin/env python3
"""
Characterization tests for _dispatch_tool_use() — the shared tool-dispatch path.

These are hermetic: no model calls, no MCP server, no database, no credentials.
`execute_tool` is stubbed, so the whole file runs in milliseconds and is safe in
CI. That matters, because everything it covers previously had *zero* coverage.

Why this file exists
--------------------
Duplicate detection, the ORDER_REQUIREMENTS guard, and MCP dispatch used to be
copy-pasted at five call sites (_run_tool_loop plus four streaming flows). The
copies drifted: only the blocking one warned on duplicates, so the streaming
flows — the ones /flow/stream actually runs — recorded seen_calls and never read
it. That went unnoticed because tests/test_flows.py drives run_test_flow(), which
reimplements the agentic loop *without* the order guard; tests written against it
pass whether or not dispatch works at all.

The five copies are now one helper. These tests pin its behaviour, and
test_all_sites_use_the_helper() fails if anyone re-inlines a copy.

Run:  python tests/test_tool_dispatch.py
"""

import asyncio
import contextlib
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

import run_flow  # noqa: E402
from tools import ORDER_REQUIREMENTS  # noqa: E402


# ── Stubs ─────────────────────────────────────────────────────────

class FakeBlock:
    """Stands in for an Anthropic tool_use content block."""
    def __init__(self, name: str, inputs: dict | None = None, id: str = "tu_1"):
        self.type = "tool_use"
        self.name = name
        self.input = inputs or {}
        self.id = id


mcp_calls: list[str] = []


async def fake_execute_tool(session, name: str, inputs: dict) -> str:
    """Stub for execute_tool — records the call, never touches MCP."""
    mcp_calls.append(name)
    if name == "always_errors":
        return json.dumps({"error": "tool failed"})
    return json.dumps({"status": "ok", "tool": name})


run_flow.execute_tool = fake_execute_tool
dispatch = run_flow._dispatch_tool_use


def call(block, seen=None, completed=None, verbose=False) -> tuple[dict, str]:
    """Run one dispatch, returning (tool_result, captured_stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = asyncio.run(dispatch(None, block, seen if seen is not None else {},
                                      completed if completed is not None else set(), verbose))
    return result, buf.getvalue()


def body(result: dict) -> dict:
    return json.loads(result["content"])


# ── Order guard ───────────────────────────────────────────────────

def test_guard_blocks_when_prerequisite_unmet():
    mcp_calls.clear()
    result, _ = call(FakeBlock("flag_user", {"user_id": "u1"}))
    err = body(result).get("error", "")
    if "get_user_activity" not in err:
        return False, f"expected a guard error naming get_user_activity, got {body(result)}"
    if mcp_calls:
        return False, f"blocked call still reached MCP: {mcp_calls}"
    return True, "blocked, and never dispatched to MCP"


def test_guard_clears_when_prerequisite_met():
    mcp_calls.clear()
    result, _ = call(FakeBlock("flag_user", {"user_id": "u1"}),
                     completed={"get_user_activity"})
    if "error" in body(result):
        return False, f"guard should have cleared, got {body(result)}"
    if mcp_calls != ["flag_user"]:
        return False, f"expected one MCP call to flag_user, got {mcp_calls}"
    return True, "dispatched once the prerequisite was satisfied"


def test_guard_enforces_the_full_chain():
    """deactivate_user -> flag_user -> get_user_activity, two levels deep."""
    completed = set()
    err = body(call(FakeBlock("deactivate_user"), completed=completed)[0]).get("error", "")
    if "flag_user" not in err:
        return False, f"deactivate should require flag_user, got {err!r}"

    completed = {"get_user_activity", "flag_user"}
    if "error" in body(call(FakeBlock("deactivate_user"), completed=completed)[0]):
        return False, "deactivate should be allowed once flag_user has succeeded"
    return True, "two-level chain enforced"


def test_unguarded_tools_pass_straight_through():
    unguarded = FakeBlock("get_user", {"user_id": "u1"})
    if "error" in body(call(unguarded)[0]):
        return False, "get_user has no ORDER_REQUIREMENTS and must not be blocked"
    return True, f"tools outside {sorted(ORDER_REQUIREMENTS)} dispatch freely"


# ── completed-set bookkeeping ─────────────────────────────────────

def test_success_marks_completed():
    completed = set()
    call(FakeBlock("get_user_activity", {"user_id": "u1"}), completed=completed)
    if "get_user_activity" not in completed:
        return False, f"expected get_user_activity in completed, got {completed}"
    return True, "successful tool recorded"


def test_error_result_does_not_mark_completed():
    """The guard must not be satisfiable by a tool that failed."""
    completed = set()
    call(FakeBlock("always_errors"), completed=completed)
    if "always_errors" in completed:
        return False, "a tool returning {'error': ...} must not count as completed"
    return True, "failed tool not recorded"


def test_blocked_call_does_not_mark_completed():
    completed = set()
    call(FakeBlock("flag_user", {"user_id": "u1"}), completed=completed)
    if completed:
        return False, f"a guard-blocked call must record nothing, got {completed}"
    return True, "guard-blocked call recorded nothing"


# ── Duplicate detection (was dead code in all streaming flows) ────

def test_first_call_is_silent():
    seen, completed = {}, {"get_user_activity"}
    _, out = call(FakeBlock("flag_user", {"user_id": "u1"}), seen, completed)
    if "DUPLICATE" in out:
        return False, f"first call should not warn, printed: {out.strip()!r}"
    return True, "no warning on first call"


def test_repeat_call_warns():
    seen, completed = {}, {"get_user_activity"}
    block = FakeBlock("flag_user", {"user_id": "u1"})
    call(block, seen, completed)
    _, out = call(block, seen, completed)
    if "DUPLICATE TOOL CALL" not in out:
        return False, f"expected a duplicate warning, printed: {out.strip()!r}"
    return True, "duplicate warned on second identical call"


def test_different_args_are_not_duplicates():
    seen, completed = {}, {"get_user_activity"}
    call(FakeBlock("flag_user", {"user_id": "u1"}), seen, completed)
    _, out = call(FakeBlock("flag_user", {"user_id": "u2"}), seen, completed)
    if "DUPLICATE" in out:
        return False, "same tool with different args is not a duplicate"
    return True, "keyed on name+args, not name alone"


# ── Result shape and logging ──────────────────────────────────────

def test_result_shape_round_trips():
    result, _ = call(FakeBlock("get_user", {}, id="tu_xyz"))
    if result.get("type") != "tool_result":
        return False, f"expected type=tool_result, got {result.get('type')}"
    if result.get("tool_use_id") != "tu_xyz":
        return False, f"tool_use_id must match the block, got {result.get('tool_use_id')}"
    if not isinstance(result.get("content"), str):
        return False, "content must be a JSON string for the API"
    return True, "type / tool_use_id / content correct"


def test_verbose_logs_and_quiet_is_quiet():
    _, loud = call(FakeBlock("get_user", {}), verbose=True)
    if "[TOOL CALL]" not in loud or "[TOOL RESULT]" not in loud:
        return False, f"verbose should log call and result, got {loud.strip()!r}"
    _, quiet = call(FakeBlock("get_user", {}), verbose=False)
    if quiet.strip():
        return False, f"non-verbose should print nothing, got {quiet.strip()!r}"
    return True, "verbose logs, quiet stays quiet"


# ── Structural: nobody re-inlines a copy ──────────────────────────

def test_all_sites_use_the_helper():
    """
    Guards against the drift this helper was extracted to fix. If a future edit
    inlines the dispatch logic again, the copies can diverge silently — which is
    how the streaming flows lost duplicate detection in the first place.
    """
    src = (ROOT / "flows" / "run_flow.py").read_text()
    inline = len(re.findall(r"ORDER_REQUIREMENTS\.get\(block\.name", src))
    sites = len(re.findall(r"await _dispatch_tool_use\(", src))
    if inline != 1:
        return False, (f"order-guard logic appears {inline}x — expected exactly 1 "
                       f"(inside _dispatch_tool_use). A copy has been re-inlined.")
    if sites != 5:
        return False, (f"expected 5 call sites (_run_tool_loop + 4 streaming flows), "
                       f"found {sites}. A flow may be bypassing the guard.")
    return True, f"1 implementation, {sites} call sites"


TESTS = [
    test_guard_blocks_when_prerequisite_unmet,
    test_guard_clears_when_prerequisite_met,
    test_guard_enforces_the_full_chain,
    test_unguarded_tools_pass_straight_through,
    test_success_marks_completed,
    test_error_result_does_not_mark_completed,
    test_blocked_call_does_not_mark_completed,
    test_first_call_is_silent,
    test_repeat_call_warns,
    test_different_args_are_not_duplicates,
    test_result_shape_round_trips,
    test_verbose_logs_and_quiet_is_quiet,
    test_all_sites_use_the_helper,
]


def main() -> int:
    print("\nTool dispatch — characterization tests (hermetic, no credentials)")
    print("=" * 66)
    passed = 0
    for i, fn in enumerate(TESTS, 1):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        print(f"  [{i:2}/{len(TESTS)}] {'PASS' if ok else 'FAIL'}  {fn.__name__}")
        if not ok:
            print(f"           {detail}")
        passed += ok

    print(f"\n{'='*66}\nResults: {passed}/{len(TESTS)} passed")
    print("🟢 All passed" if passed == len(TESTS) else "🔴 Failures above")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
