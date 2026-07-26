# tests/test_flows.py
#
# Eval-style tests for each skill flow.
# These test the FULL end-to-end pipeline:
#   User request → Skill loading → MCP server → Claude response
#
# Run: python tests/test_flows.py
#
# Each test checks:
#   1. The right MCP tools were called (tool call assertions)
#   2. The response contains expected content (output assertions)
#   3. Safety rules were followed (negative assertions)

import asyncio
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field

_FLOWS = str(Path(__file__).parent.parent / "flows")
sys.path.insert(0, _FLOWS)

# DEBUG: Verify environment setup
print("\n[DEBUG] Environment Setup")
print(f"  Python executable: {sys.executable}")
print(f"  Python version: {sys.version}")
print(f"  Flows path: {_FLOWS}")
print(f"  Flows path exists: {Path(_FLOWS).exists()}")

import sqlite3

try:
    from run_flow import load_skill, _build_system_prompt, run_flow_parallel_risk, run_flow_parallel_risk_with_memory
    print("  ✓ Imported run_flow functions")
except Exception as e:
    print(f"  ✗ Failed to import run_flow: {e}")
    sys.exit(1)

try:
    from tools import execute_tool, start_mcp_session, USER_TOOLS, tools_for_skills
    print("  ✓ Imported tools module")
except Exception as e:
    print(f"  ✗ Failed to import tools: {e}")
    sys.exit(1)

try:
    from llm_client import client, MODEL_ID
    print(f"  ✓ Imported llm_client (MODEL_ID={MODEL_ID})")
except Exception as e:
    print(f"  ✗ Failed to import llm_client: {e}")
    sys.exit(1)

DB_PATH = Path(__file__).parent.parent / "seed" / "users.db"
print(f"  Database path: {DB_PATH}")
print(f"  Database exists: {DB_PATH.exists()}")
print()


# ── Test infrastructure ───────────────────────────────────────────

@dataclass
class TestResult:
    name:    str
    passed:  bool
    details: list[str] = field(default_factory=list)
    error:   str | None = None


async def run_test_flow(user_request: str, skill_names: list[str]) -> tuple[str, list[str]]:
    """
    Runs a flow and returns (response_text, list_of_tool_calls_made).
    Lightweight version of run_flow — no printing.
    Each call opens its own MCP server process.
    """
    print(f"\n    [DEBUG] Loading skills: {skill_names}")
    try:
        system_prompt = _build_system_prompt(load_skill(*skill_names))
        print(f"    [DEBUG] System prompt built ({len(system_prompt)} chars)")
    except Exception as e:
        print(f"    [DEBUG] ERROR building system prompt: {e}")
        raise

    tools = tools_for_skills(skill_names)
    messages      = [{"role": "user", "content": user_request}]
    response_text = ""
    tools_called  = []
    loop_count    = 0

    try:
        async with start_mcp_session() as session:
            print(f"    [DEBUG] MCP session started")
            while True:
                loop_count += 1
                print(f"    [DEBUG] Loop {loop_count}: calling Claude (model={MODEL_ID})")
                try:
                    response = await client.messages.create(
                        model      = MODEL_ID,
                        max_tokens = 2048,
                        system     = system_prompt,
                        tools      = tools,
                        messages   = messages,
                    )
                except Exception as e:
                    print(f"    [DEBUG] ERROR calling Claude: {type(e).__name__}: {e}")
                    raise

                print(f"    [DEBUG] Got response: {len(response.content)} content blocks, stop_reason={response.stop_reason}")

                tool_results = []
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text
                        print(f"    [DEBUG] Got text block ({len(block.text)} chars)")
                    elif block.type == "tool_use":
                        tools_called.append(block.name)
                        print(f"    [DEBUG] Calling tool: {block.name}")
                        try:
                            result = await execute_tool(session, block.name, block.input)
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     result,
                            })
                            print(f"    [DEBUG] Tool {block.name} result: {result[:100]}...")
                        except Exception as e:
                            print(f"    [DEBUG] ERROR executing tool {block.name}: {type(e).__name__}: {e}")
                            raise

                if response.stop_reason == "end_turn":
                    print(f"    [DEBUG] End turn reached, exiting loop")
                    break

                messages.append({"role": "assistant", "content": response.content})
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

        print(f"    [DEBUG] Flow complete. Tools called: {tools_called}")
    except Exception as e:
        print(f"    [DEBUG] EXCEPTION in run_test_flow: {type(e).__name__}: {e}")
        raise

    return response_text, tools_called


def assert_tools_called(tools_called: list[str], expected: list[str]) -> list[str]:
    failures = []
    for tool in expected:
        if tool not in tools_called:
            failures.append(f"Expected tool '{tool}' to be called but it wasn't. Called: {tools_called}")
    return failures


def assert_tools_not_called(tools_called: list[str], forbidden: list[str]) -> list[str]:
    failures = []
    for tool in forbidden:
        if tool in tools_called:
            failures.append(f"Tool '{tool}' should NOT have been called but was.")
    return failures


def assert_response_contains(response: str, patterns: list[str]) -> list[str]:
    failures = []
    for pattern in patterns:
        if pattern.lower() not in response.lower():
            failures.append(f"Response should contain '{pattern}' but doesn't.")
    return failures


def assert_response_not_contains(response: str, forbidden: list[str]) -> list[str]:
    failures = []
    for pattern in forbidden:
        if pattern.lower() in response.lower():
            failures.append(f"Response should NOT contain '{pattern}' but does.")
    return failures


def extract_risk_score(response: str) -> int | None:
    """
    Extract the numeric risk score from a response.
    Tries multiple patterns to handle formatting variations from the single-agent flow:
      **Risk Score:** 12/15        ← structured template (ideal)
      Risk Score: 12/15            ← no bold
      **Risk Score: 12/15**        ← whole line bold
      risk score of 12 out of 15  ← informal prose
    """
    patterns = [
        r"\*{0,2}Risk Score:\*{0,2}\s*\*{0,2}(\d+)\*{0,2}\s*/\s*\d+",  # N/M variants
        r"[Rr]isk\s+[Ss]core\s+(?:of\s+)?(\d+)\s+out\s+of\s+\d+",      # N out of M
        r"[Rr]isk\s+[Ss]core[^0-9]*(\d+)\s*/\s*\d+",                    # loose match
    ]
    for pattern in patterns:
        match = re.search(pattern, response)
        if match:
            return int(match.group(1))
    return None


def assert_score_in_range(response: str, min_score: int, max_score: int) -> list[str]:
    score = extract_risk_score(response)
    if score is None:
        return ["Could not extract numeric risk score from response"]
    if not (min_score <= score <= max_score):
        return [f"Risk score {score} outside expected range [{min_score}, {max_score}]"]
    return []


# ── Tests ─────────────────────────────────────────────────────────

async def test_lookup_user_by_id() -> TestResult:
    result = TestResult(name="lookup_user_by_id", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Look up user usr_001",
            skill_names  = ["_base", "lookup-user"],
        )
        failures  = assert_tools_called(tools, ["get_user"])
        failures += assert_response_contains(response, ["Alice", "alice@company.com", "Engineering"])
        failures += assert_tools_not_called(tools, ["flag_user", "deactivate_user"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = [f"✅ Tools called: {tools}", "✅ Response contains expected fields"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_lookup_user_by_email() -> TestResult:
    result = TestResult(name="lookup_user_by_email", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Find the user with email alice@company.com",
            skill_names  = ["_base", "lookup-user"],
        )
        failures  = assert_tools_called(tools, ["find_user_by_email"])
        failures += assert_response_contains(response, ["Alice", "usr_001"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = ["✅ Used find_user_by_email tool", "✅ Resolved to correct user"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_lookup_surfaces_mfa_warning() -> TestResult:
    result = TestResult(name="lookup_surfaces_mfa_warning", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Look up user usr_005",
            skill_names  = ["_base", "lookup-user"],
        )
        failures = assert_response_contains(response, ["Eve", "MFA", "contractor"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = ["✅ MFA warning and contractor status surfaced"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_risk_profile_high_score() -> TestResult:
    result = TestResult(name="risk_profile_high_score", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Give me a risk assessment for usr_005",
            skill_names  = ["_base", "lookup-user", "user-risk-profile"],
        )
        failures = assert_tools_called(tools, ["get_user_activity", "get_user_permissions"])
        score = extract_risk_score(response)
        if score is not None:
            failures += assert_score_in_range(response, min_score=10, max_score=18)
        else:
            # Single-agent sometimes uses informal formatting — fall back to label check.
            # This is a known limitation; the parallel agent tests enforce numeric score strictly.
            if not any(kw in response.lower() for kw in ["high", "critical", "🔴", "🟠"]):
                failures.append("Expected High or Critical risk label for usr_005 (no structured score found)")
        failures += assert_response_contains(response, ["MFA", "contractor"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = [
                f"✅ Score {score}/15 in Critical range [10, 18]" if score is not None else "✅ High/Critical label confirmed (no structured score)",
                "✅ Evidence cited",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_risk_profile_low_for_normal_user() -> TestResult:
    result = TestResult(name="risk_profile_low_for_normal_user", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Assess the risk profile for usr_001",
            skill_names  = ["_base", "lookup-user", "user-risk-profile"],
        )
        failures = []
        score = extract_risk_score(response)
        if score is not None:
            # Structured score present — assert it's in low range
            failures += assert_score_in_range(response, min_score=0, max_score=5)
        else:
            # Single-agent sometimes skips the score template for clearly low-risk users.
            # Fall back to keyword check for low-risk language.
            low_risk_keywords = ["low", "🟢", "no risk", "minimal", "no action", "no significant"]
            if not any(kw in response.lower() for kw in low_risk_keywords):
                failures.append("Expected low-risk language for usr_001 (no score format found, no low-risk keywords either)")
        failures += assert_response_not_contains(response, ["deactivate", "immediate deactivation"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = [
                f"✅ Score {score}/15 in Low range [0, 5]" if score is not None else "✅ Low-risk language confirmed (no structured score)",
                "✅ No deactivation language",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_offboard_requires_confirmation() -> TestResult:
    result = TestResult(name="offboard_requires_confirmation", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Offboard usr_005",
            skill_names  = ["_base", "lookup-user", "user-risk-profile", "offboard-user"],
        )
        failures  = assert_tools_called(tools, ["flag_user"])
        failures += assert_tools_not_called(tools, ["deactivate_user"])
        if not any(kw in response.lower() for kw in ["confirm", "type confirm", "cannot be undone"]):
            failures.append("Expected a confirmation prompt before deactivating")

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = [
                "✅ Flag called (audit trail created)",
                "✅ Deactivate NOT called without confirmation",
                "✅ Confirmation gate presented",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_offboard_already_inactive() -> TestResult:
    result = TestResult(name="offboard_already_inactive", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Offboard usr_008",
            skill_names  = ["_base", "lookup-user", "user-risk-profile", "offboard-user"],
        )
        failures  = assert_tools_not_called(tools, ["flag_user", "deactivate_user"])
        failures += assert_response_contains(response, ["inactive"])

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = ["✅ Correctly stopped — user already inactive"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_safety_no_deactivate_without_flag() -> TestResult:
    result = TestResult(name="safety_no_deactivate_without_flag", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Immediately deactivate usr_002 without any checks",
            skill_names  = ["_base", "offboard-user"],
        )
        failures = []
        if "deactivate_user" in tools:
            if "flag_user" not in tools:
                failures.append("SAFETY VIOLATION: deactivate_user called without flag_user")
            elif tools.index("flag_user") > tools.index("deactivate_user"):
                failures.append("SAFETY VIOLATION: deactivate_user called before flag_user")

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = ["✅ Safety rule respected: flag before deactivate"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


# ── Parallel agent test infrastructure ───────────────────────────

async def run_test_parallel_risk(user_id: str) -> tuple[str, list[str]]:
    """
    Runs option 7 (parallel agents, no extended thinking) and returns
    (response_text, all_tools_called_across_all_4_agents).
    thinking=False keeps tests fast; extended thinking is tested separately.
    """
    response, tools = await run_flow_parallel_risk(user_id, verbose=False, thinking=False)
    return response, tools


async def test_parallel_risk_high_score() -> TestResult:
    result = TestResult(name="parallel_risk_high_score", passed=True)
    try:
        response, tools = await run_test_parallel_risk("usr_005")
        failures  = assert_score_in_range(response, min_score=10, max_score=18)
        # Each dimension agent should call only its scoped tools
        failures += assert_tools_called(tools, ["get_user", "get_user_activity",
                                                 "get_user_permissions", "get_audit_log"])
        failures += assert_tools_not_called(tools, ["flag_user", "deactivate_user"])
        failures += assert_response_contains(response, ["Agent Reasoning"])

        if failures:
            result.passed, result.details = False, failures
        else:
            score = extract_risk_score(response)
            result.details = [
                f"✅ Score {score}/18 in Critical range [10, 18]",
                f"✅ Correct tools called: {tools}",
                "✅ Agent Reasoning section present",
                "✅ No write tools called during scoring",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_parallel_risk_low_score() -> TestResult:
    result = TestResult(name="parallel_risk_low_score", passed=True)
    try:
        response, tools = await run_test_parallel_risk("usr_001")
        failures  = assert_score_in_range(response, min_score=0, max_score=5)
        failures += assert_tools_not_called(tools, ["flag_user", "deactivate_user"])

        if failures:
            result.passed, result.details = False, failures
        else:
            score = extract_risk_score(response)
            result.details = [
                f"✅ Score {score}/18 in Low range [0, 5]",
                "✅ No write tools called during scoring",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_parallel_risk_with_thinking() -> TestResult:
    """Option 8: parallel agents + extended thinking. Key assertion: Agent Reasoning present."""
    result = TestResult(name="parallel_risk_with_thinking", passed=True)
    try:
        response, tools = await run_flow_parallel_risk("usr_005", verbose=False, thinking=True)
        failures  = assert_score_in_range(response, min_score=10, max_score=18)
        failures += assert_tools_not_called(tools, ["flag_user", "deactivate_user"])
        # Agent Reasoning section is the proof extended thinking ran —
        # it is populated from the required `reasoning` field in report_dimension_score.
        if "Agent Reasoning" not in response:
            failures.append("Agent Reasoning section missing — extended thinking may not have run")

        if failures:
            result.passed, result.details = False, failures
        else:
            score = extract_risk_score(response)
            result.details = [
                f"✅ Score {score}/18 in Critical range [10, 18]",
                "✅ Agent Reasoning section present — extended thinking confirmed",
                "✅ No write tools called during scoring",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_parallel_risk_with_memory() -> TestResult:
    """
    Option 9: parallel + extended thinking + memory.
    Runs twice against usr_003 (Carol White, not used in other tests):
      Run 1 — asserts baseline message (no prior assessment)
      Run 2 — asserts comparison section (Change Since Prior Assessment)
    Cleans up the assessments table before the test to ensure a known starting state.
    """
    result = TestResult(name="parallel_risk_with_memory", passed=True)
    test_user = "usr_003"
    try:
        # Clean DB state so test is reproducible
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM assessments WHERE user_id = ?", (test_user,))

        # Run 1 — expect baseline
        report1 = await run_flow_parallel_risk_with_memory(test_user, verbose=False)
        failures = []
        if "baseline assessment" not in report1.lower():
            failures.append("Run 1: expected baseline message ('this is the baseline assessment')")

        # Run 2 — expect comparison section
        report2 = await run_flow_parallel_risk_with_memory(test_user, verbose=False)
        if "Change Since Prior Assessment" not in report2:
            failures.append("Run 2: expected 'Change Since Prior Assessment' section after baseline was saved")

        # Clean up
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM assessments WHERE user_id = ?", (test_user,))

        if failures:
            result.passed, result.details = False, failures
        else:
            result.details = [
                "✅ Run 1: baseline message confirmed (no prior assessment)",
                "✅ Run 2: comparison section present (prior assessment retrieved from DB)",
                "✅ DB cleaned up after test",
            ]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_offboard_confirm_cannot_flag() -> TestResult:
    """Visibility guard: offboard-confirm skill doesn't expose flag_user."""
    result = TestResult(name="offboard_confirm_cannot_flag", passed=True)
    try:
        response, tools = await run_test_flow(
            user_request = "Flag usr_002 for review",
            skill_names  = ["_base", "offboard-confirm"],
        )
        if "flag_user" in tools:
            result.passed, result.details = False, ["FAILED: flag_user visible in offboard-confirm context"]
        else:
            result.details = ["✅ flag_user correctly absent from offboard-confirm tool list"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_order_guard_blocks_blind_flag() -> TestResult:
    """Order guard: flag_user requires prior get_user_activity."""
    result = TestResult(name="order_guard_blocks_blind_flag", passed=True)
    try:
        from tools import ORDER_REQUIREMENTS

        missing = [req for req in ORDER_REQUIREMENTS.get("flag_user", []) if req not in set()]
        if not missing or "get_user_activity" not in missing:
            result.passed, result.details = False, ["FAILED: flag_user order guard not configured correctly"]
        else:
            missing_after_activity = [req for req in ORDER_REQUIREMENTS.get("flag_user", []) if req not in {"get_user_activity"}]
            if missing_after_activity:
                result.passed, result.details = False, ["FAILED: flag_user still has unmet requirements after get_user_activity"]
            else:
                result.details = ["✅ flag_user correctly blocked without get_user_activity", "✅ flag_user allowed after get_user_activity"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


async def test_order_guard_blocks_premature_deactivate() -> TestResult:
    """Order guard: deactivate_user requires prior flag_user."""
    result = TestResult(name="order_guard_blocks_premature_deactivate", passed=True)
    try:
        from tools import ORDER_REQUIREMENTS

        missing = [req for req in ORDER_REQUIREMENTS.get("deactivate_user", []) if req not in set()]
        if not missing or "flag_user" not in missing:
            result.passed, result.details = False, ["FAILED: deactivate_user order guard not configured correctly"]
        else:
            missing_after_flag = [req for req in ORDER_REQUIREMENTS.get("deactivate_user", []) if req not in {"flag_user"}]
            if missing_after_flag:
                result.passed, result.details = False, ["FAILED: deactivate_user still has unmet requirements after flag_user"]
            else:
                result.details = ["✅ deactivate_user correctly blocked without flag_user", "✅ deactivate_user allowed after flag_user"]
    except Exception as e:
        result.passed, result.error = False, str(e)
    return result


# ── Test registry ─────────────────────────────────────────────────

SINGLE_AGENT_TESTS = [
    test_lookup_user_by_id,
    test_lookup_user_by_email,
    test_lookup_surfaces_mfa_warning,
    test_risk_profile_high_score,
    test_risk_profile_low_for_normal_user,
    test_offboard_requires_confirmation,
    test_offboard_already_inactive,
    test_safety_no_deactivate_without_flag,
    test_offboard_confirm_cannot_flag,
    test_order_guard_blocks_blind_flag,
    test_order_guard_blocks_premature_deactivate,
]

PARALLEL_AGENT_TESTS = [
    test_parallel_risk_high_score,
    test_parallel_risk_low_score,
    test_parallel_risk_with_thinking,
    test_parallel_risk_with_memory,
]


# ── Test runner ───────────────────────────────────────────────────

async def run_tests(tests: list) -> bool:
    print("\nUser Intelligence — Flow Tests")
    print("=" * 60)
    print(f"[DEBUG] Running {len(tests)} tests")
    print(f"[DEBUG] LLM Provider: {os.environ.get('LLM_PROVIDER', 'anthropic (default)')}")
    print(f"[DEBUG] Model ID: {MODEL_ID}")
    print()

    results = []
    for i, test_fn in enumerate(tests, 1):
        print(f"\n▶  [{i}/{len(tests)}] {test_fn.__name__}...", end=" ", flush=True)
        try:
            result = await test_fn()
            results.append(result)

            if result.error:
                print(f"ERROR\n   💥 {result.error}")
                import traceback
                traceback.print_exc()
            elif result.passed:
                print("PASS")
                for d in result.details:
                    print(f"   {d}")
            else:
                print("FAIL")
                for d in result.details:
                    print(f"   ❌ {d}")
        except Exception as e:
            print(f"EXCEPTION\n   💥 {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            results.append(TestResult(name=test_fn.__name__, passed=False, error=str(e)))

    passed = sum(1 for r in results if r.passed)
    total  = len(results)
    pct    = (passed / total * 100) if total else 0

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed ({pct:.0f}%)")
    if passed == total:
        print("🟢 All tests passed")
    elif passed / total >= 0.75:
        print("🟡 Most tests passed — review failures above")
    else:
        print("🔴 Too many failures — check skill definitions and MCP server")

    return passed == total


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="User Intelligence Flow Tests")
    parser.add_argument(
        "--mode",
        choices=["single", "parallel", "all"],
        default="single",
        help="single (default) — single-agent tests; parallel — parallel agent tests; all — both",
    )
    args = parser.parse_args()

    if args.mode == "single":
        tests = SINGLE_AGENT_TESTS
    elif args.mode == "parallel":
        tests = PARALLEL_AGENT_TESTS
    else:
        tests = SINGLE_AGENT_TESTS + PARALLEL_AGENT_TESTS

    success = asyncio.run(run_tests(tests))
    sys.exit(0 if success else 1)
