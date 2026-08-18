#!/usr/bin/env python3
"""
Unit tests for the pure and near-pure helpers in flows/.

Hermetic: no model, no MCP server, no database, no credentials. Runs in ~2s.

Why this file exists
--------------------
Every function here shapes a request or a report before anything leaves the
process, and none of them had a test. _cache_tools() is the pointed example:
it put a cache_control breakpoint on *every* tool, the API caps them at 4, and
every flow exposing more than three tools returned

    400 - A maximum of 4 blocks with cache_control may be provided. Found 6.

CLI options 1-6 and every /flow call were dead. The suite stayed at 15/15
green throughout, because nothing exercised the function. test_cache_tools_*
below is five lines and would have caught it on the first run.

Run:  python tests/test_pure_functions.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

from run_flow import (  # noqa: E402
    MAX_TOOL_ITERATIONS,
    ThinkingPrinter,
    _build_system_prompt,
    _cache_tools,
    _is_error_result,
    _iteration_guard,
    _synthesize_risk_report,
    load_skill,
)
from tools import USER_TOOLS, tools_for_skills  # noqa: E402

MAX_CACHE_BREAKPOINTS = 4      # hard API limit
SYSTEM_PROMPT_BREAKPOINTS = 1  # every flow spends one on the system prompt


def breakpoints(tools: list[dict]) -> int:
    return sum(1 for t in tools if "cache_control" in t)


# ── _cache_tools ──────────────────────────────────────────────────

def test_cache_tools_marks_only_the_last():
    tools = [{"name": f"t{i}"} for i in range(5)]
    out = _cache_tools(tools)
    marked = [t["name"] for t in out if "cache_control" in t]
    if marked != ["t4"]:
        return False, f"expected only the last tool marked, got {marked}"
    return True, "one breakpoint, on the final tool"


def test_cache_tools_stays_under_the_api_limit():
    """The regression. Must hold for every skill combination the app can build."""
    combos = [
        ["_base", "lookup-user"],
        ["_base", "lookup-user", "user-risk-profile"],
        ["_base", "lookup-user", "user-risk-profile", "offboard-prepare"],
        ["_base", "lookup-user", "user-risk-profile", "offboard-user"],
        ["_base", "offboard-confirm"],
        ["risk-auth"], ["risk-permissions"], ["risk-behaviour"], ["risk-account"],
    ]
    worst = 0
    for combo in combos:
        total = breakpoints(_cache_tools(tools_for_skills(combo))) + SYSTEM_PROMPT_BREAKPOINTS
        worst = max(worst, total)
        if total > MAX_CACHE_BREAKPOINTS:
            return False, (f"{combo} needs {total} breakpoints, API allows "
                           f"{MAX_CACHE_BREAKPOINTS} — this is a hard 400")
    return True, f"worst case {worst}/{MAX_CACHE_BREAKPOINTS} across {len(combos)} combos"


def test_cache_tools_handles_every_tool_at_once():
    """A future skill could expose all of them; the count must not scale."""
    total = breakpoints(_cache_tools(USER_TOOLS)) + SYSTEM_PROMPT_BREAKPOINTS
    if total > MAX_CACHE_BREAKPOINTS:
        return False, f"all {len(USER_TOOLS)} tools -> {total} breakpoints"
    return True, f"all {len(USER_TOOLS)} tools -> {total} breakpoints (constant)"


def test_cache_tools_handles_empty_and_none():
    if _cache_tools([]) != [] or _cache_tools(None) != []:
        return False, "empty/None must return [] — a flow may expose no tools"
    return True, "[] and None -> []"


def test_cache_tools_does_not_mutate_input():
    tools = [{"name": "a"}, {"name": "b"}]
    _cache_tools(tools)
    if any("cache_control" in t for t in tools):
        return False, "mutated the caller's list — USER_TOOLS is module-level shared state"
    return True, "input left untouched"


def test_cache_tools_preserves_order_and_content():
    tools = [{"name": "a", "x": 1}, {"name": "b", "x": 2}, {"name": "c", "x": 3}]
    out = _cache_tools(tools)
    if [t["name"] for t in out] != ["a", "b", "c"]:
        return False, "order changed — cache keys are prefix-sensitive"
    if [t["x"] for t in out] != [1, 2, 3]:
        return False, "tool fields altered"
    return True, "order and fields preserved"


# ── _is_error_result ──────────────────────────────────────────────

def test_is_error_result():
    cases = [
        ('{"status": "ok"}',        False, "success dict"),
        ('{}',                      False, "empty dict"),
        ('{"error": "not found"}',  True,  "error dict"),
        ('[1, 2, 3]',               True,  "JSON list — 'error' in list is meaningless"),
        ('"a bare string"',         True,  "bare JSON string"),
        ('not json at all',         True,  "unparseable — must not raise"),
        ('',                        True,  "empty string"),
    ]
    for raw, want, label in cases:
        got = _is_error_result(raw)
        if got != want:
            return False, f"{label}: expected error={want}, got {got}"
    return True, f"{len(cases)} cases, including inputs that used to raise JSONDecodeError"


def test_unparseable_result_does_not_satisfy_the_order_guard():
    """Treating junk as success would let it clear ORDER_REQUIREMENTS."""
    if not _is_error_result("<html>gateway timeout</html>"):
        return False, "non-JSON must count as failure, not success"
    return True, "junk counts as failure"


# ── _iteration_guard ──────────────────────────────────────────────

def test_iteration_guard_boundary():
    if _iteration_guard(1, "x") is not False:
        return False, "iteration 1 must not trip"
    if _iteration_guard(MAX_TOOL_ITERATIONS, "x") is not False:
        return False, f"iteration {MAX_TOOL_ITERATIONS} is the last allowed; must not trip"
    if _iteration_guard(MAX_TOOL_ITERATIONS + 1, "x") is not True:
        return False, f"iteration {MAX_TOOL_ITERATIONS + 1} must trip"
    return True, f"trips above {MAX_TOOL_ITERATIONS}, not at it"


# ── _synthesize_risk_report ───────────────────────────────────────

def _dims(a, p, b, c):
    mk = lambda s, m: {"score": s, "max_score": m, "factors": [], "evidence": [], "reasoning": "r"}
    return mk(a, 6), mk(p, 5), mk(b, 4), mk(c, 3)


def test_risk_level_thresholds():
    """Boundary values for each band — off-by-one here misreports severity."""
    cases = [
        (0, 0, 0, 0, "Low"),        # 0
        (2, 0, 0, 0, "Low"),        # 2  — last Low
        (3, 0, 0, 0, "Medium"),     # 3  — first Medium
        (5, 0, 0, 0, "Medium"),     # 5  — last Medium
        (6, 0, 0, 0, "High"),       # 6  — first High
        (6, 3, 0, 0, "High"),       # 9  — last High
        (6, 4, 0, 0, "Critical"),   # 10 — first Critical
        (6, 5, 4, 3, "Critical"),   # 18 — max
    ]
    for a, p, b, c, want in cases:
        report = _synthesize_risk_report("usr_x", *_dims(a, p, b, c))
        if want not in report:
            return False, f"total {a+p+b+c} should be {want}; report said otherwise"
    return True, f"{len(cases)} boundary totals map to the right band"


def test_report_without_prior_says_baseline():
    report = _synthesize_risk_report("usr_x", *_dims(1, 1, 1, 1), prior={"none": True})
    if "baseline" not in report.lower():
        return False, "first run should be labelled a baseline"
    return True, "baseline noted when no prior exists"


def test_report_with_prior_shows_a_delta():
    prior = {"none": False, "total_score": 5, "max_score": 18,
             "risk_level": "🟡 Medium", "assessed_at": "2026-06-24T00:00:00",
             "auth_score": 1, "perms_score": 1, "behav_score": 1, "acct_score": 2}
    report = _synthesize_risk_report("usr_x", *_dims(6, 5, 4, 3), prior=prior)
    if "5" not in report or "18" not in report:
        return False, "delta section should cite the prior score and the max"
    return True, "prior score surfaced for comparison"


# ── load_skill / _build_system_prompt ─────────────────────────────

def test_load_skill_strips_frontmatter():
    content = load_skill("_base")
    if content.lstrip().startswith("---") and content.count("---") > 2:
        return False, "YAML frontmatter leaked into the system prompt"
    if "# SKILL: _base" not in content:
        return False, "skill header missing"
    return True, "frontmatter stripped, header present"


def test_load_skill_concatenates_in_order():
    combined = load_skill("_base", "lookup-user")
    if combined.index("# SKILL: _base") > combined.index("# SKILL: lookup-user"):
        return False, "skills must appear in the order given — _base defines shared conventions"
    return True, "order preserved"


def test_load_skill_rejects_a_missing_skill():
    try:
        load_skill("no-such-skill")
    except FileNotFoundError:
        return True, "raises FileNotFoundError rather than silently loading nothing"
    return False, "a typo'd skill name must not silently produce an empty prompt"


def test_build_system_prompt_embeds_the_skills():
    prompt = _build_system_prompt("MARKER-CONTENT")
    if "MARKER-CONTENT" not in prompt:
        return False, "skills content missing from the system prompt"
    return True, "skills embedded"


def test_thinking_printer_reassembles_interleaved_fragments():
    """
    Four dimension agents stream concurrently under asyncio.gather, so deltas
    from different dimensions arrive interleaved and split mid-word. Printing
    them straight through is unreadable; each dimension must buffer to a newline
    and emit one labelled line.
    """
    import io
    buf = io.StringIO()
    printer = ThinkingPrinter(stream=buf)
    for dimension, fragment in [
        ("auth",  "MFA dis"),
        ("perms", "admin "),
        ("auth",  "abled -> +2\n"),
        ("perms", "found -> +2\n"),
    ]:
        printer.emit(dimension, fragment)

    lines = buf.getvalue().splitlines()
    if lines != ["[THINKING — AUTH] MFA disabled -> +2",
                 "[THINKING — PERMS] admin found -> +2"]:
        return False, f"fragments did not reassemble per dimension: {lines}"
    return True, "interleaved mid-word fragments reassembled"


def test_thinking_printer_flush_emits_the_tail():
    """
    Without a flush the final partial line is silently dropped — and that line is
    usually the agent's conclusion, the part worth reading.
    """
    import io
    buf = io.StringIO()
    printer = ThinkingPrinter(stream=buf)
    printer.emit("auth", "no trailing newline here")
    if buf.getvalue():
        return False, "emitted an incomplete line before flush"

    printer.flush()
    if "no trailing newline here" not in buf.getvalue():
        return False, "flush lost the trailing partial line"

    printer.flush()
    if buf.getvalue().count("no trailing newline here") != 1:
        return False, "flush is not idempotent — buffers must clear"
    return True, "tail flushed once"


TESTS = [
    test_thinking_printer_reassembles_interleaved_fragments,
    test_thinking_printer_flush_emits_the_tail,
    test_cache_tools_marks_only_the_last,
    test_cache_tools_stays_under_the_api_limit,
    test_cache_tools_handles_every_tool_at_once,
    test_cache_tools_handles_empty_and_none,
    test_cache_tools_does_not_mutate_input,
    test_cache_tools_preserves_order_and_content,
    test_is_error_result,
    test_unparseable_result_does_not_satisfy_the_order_guard,
    test_iteration_guard_boundary,
    test_risk_level_thresholds,
    test_report_without_prior_says_baseline,
    test_report_with_prior_shows_a_delta,
    test_load_skill_strips_frontmatter,
    test_load_skill_concatenates_in_order,
    test_load_skill_rejects_a_missing_skill,
    test_build_system_prompt_embeds_the_skills,
]


def main() -> int:
    print("\nPure functions — unit tests (hermetic, no credentials)")
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
