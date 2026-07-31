#!/usr/bin/env python3
"""
Invariant and contract tests across the tool/skill wiring.

Hermetic: parses source and inspects module-level data. No model, no MCP
server, no database, no credentials.

Why invariants rather than examples
-----------------------------------
An example test says "usr_005 scores high". An invariant says "every tool a
skill declares must exist" — it catches a whole class of mistakes, and it
fires at the moment someone adds a tool, which is exactly when this project's
wiring breaks. Adding a tool means editing four places (database.py,
server.py, USER_TOOLS, SKILL_TOOLS) and CLAUDE.md warns they must be kept in
sync by hand. Nothing checked that until now.

These also encode the security guarantees as assertions rather than prose:
the offboard phase split and the order-guard chain are only real if the data
backing them is well-formed.

Run:  python tests/test_invariants.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

from run_flow import _cache_tools  # noqa: E402
from tools import (  # noqa: E402
    ORDER_REQUIREMENTS,
    SKILL_TOOLS,
    USER_TOOLS,
    tools_for_skills,
)

SKILLS_DIR = ROOT / "skills"
SERVER_PY = ROOT / "mcp-server" / "server.py"

TOOL_NAMES = {t["name"] for t in USER_TOOLS}
MAX_CACHE_BREAKPOINTS = 4


def mcp_server_tools() -> dict[str, list[str]]:
    """{tool_name: [required params]} for every @mcp.tool() in server.py."""
    tree = ast.parse(SERVER_PY.read_text())
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any("mcp.tool" in ast.unparse(d) for d in node.decorator_list):
            continue
        args = [a.arg for a in node.args.args]
        required = args[: len(args) - len(node.args.defaults)]
        found[node.name] = required
    return found


# ── USER_TOOLS is well-formed ─────────────────────────────────────

def test_user_tools_schemas_are_wellformed():
    for tool in USER_TOOLS:
        for field in ("name", "description", "input_schema"):
            if field not in tool:
                return False, f"{tool.get('name', '?')} is missing '{field}'"
        schema = tool["input_schema"]
        props = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        missing = required - props
        if missing:
            return False, (f"{tool['name']}: required names a property that does not "
                           f"exist: {sorted(missing)} — Claude cannot satisfy it")
        if not tool["description"].strip():
            return False, f"{tool['name']} has an empty description — Claude's only signal"
    return True, f"{len(USER_TOOLS)} schemas valid"


def test_tool_names_are_unique():
    names = [t["name"] for t in USER_TOOLS]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        return False, f"duplicate tool names: {sorted(dupes)} — later entries shadow earlier"
    return True, f"{len(names)} unique names"


# ── USER_TOOLS <-> MCP server, the documented sync hazard ─────────

def test_every_declared_tool_exists_on_the_mcp_server():
    server = mcp_server_tools()
    orphans = TOOL_NAMES - set(server)
    if orphans:
        return False, (f"declared to Claude but not implemented on the server: "
                       f"{sorted(orphans)} — calling one fails at dispatch")
    return True, f"all {len(TOOL_NAMES)} tools exist on the server"


def test_required_params_match_the_server_signature():
    server = mcp_server_tools()
    for tool in USER_TOOLS:
        if tool["name"] not in server:
            continue
        declared = set(tool["input_schema"].get("required", []))
        actual = set(server[tool["name"]])
        if declared != actual:
            return False, (f"{tool['name']}: schema requires {sorted(declared)}, "
                           f"server signature requires {sorted(actual)}")
    return True, "required params agree for every tool"


def test_server_tools_not_exposed_are_deliberate():
    """
    Server tools absent from USER_TOOLS are Python-driven (called by
    execute_tool directly, never offered to Claude). Flag any new ones so the
    omission is a decision rather than an oversight.
    """
    known_python_driven = set()   # currently none; memory tools ARE in USER_TOOLS
    unexposed = set(mcp_server_tools()) - TOOL_NAMES - known_python_driven
    if unexposed:
        return False, (f"on the server but not in USER_TOOLS: {sorted(unexposed)}. "
                       f"If intentional (Python-driven), add it to known_python_driven.")
    return True, "no unexplained server tools"


# ── SKILL_TOOLS wiring ────────────────────────────────────────────

def test_skill_tools_reference_real_tools():
    for skill, tools in SKILL_TOOLS.items():
        unknown = set(tools) - TOOL_NAMES
        if unknown:
            return False, (f"skill '{skill}' declares {sorted(unknown)}, which are not "
                           f"in USER_TOOLS — they would be silently dropped")
    return True, f"{len(SKILL_TOOLS)} skills reference only real tools"


def test_every_skill_in_skill_tools_exists_on_disk():
    for skill in SKILL_TOOLS:
        if not (SKILLS_DIR / skill / "SKILL.md").exists():
            return False, f"SKILL_TOOLS names '{skill}' but skills/{skill}/SKILL.md is missing"
    return True, f"all {len(SKILL_TOOLS)} declared skills have a SKILL.md"


def test_every_skill_on_disk_is_declared():
    """An undeclared skill loads its prose but gets no tools — silently useless."""
    on_disk = {p.name for p in SKILLS_DIR.iterdir()
               if p.is_dir() and (p / "SKILL.md").exists()}
    undeclared = on_disk - set(SKILL_TOOLS)
    if undeclared:
        return False, (f"skills with no SKILL_TOOLS entry: {sorted(undeclared)} — "
                       f"they would load with zero tools visible")
    return True, f"all {len(on_disk)} skills on disk are declared"


# ── ORDER_REQUIREMENTS wiring ─────────────────────────────────────

def test_order_requirements_reference_real_tools():
    for tool, prereqs in ORDER_REQUIREMENTS.items():
        if tool not in TOOL_NAMES:
            return False, f"ORDER_REQUIREMENTS guards '{tool}', which is not a real tool"
        unknown = set(prereqs) - TOOL_NAMES
        if unknown:
            return False, (f"'{tool}' requires {sorted(unknown)}, which are not real tools — "
                           f"the guard could never be satisfied")
    return True, f"{len(ORDER_REQUIREMENTS)} guards reference real tools"


def test_order_requirements_have_no_cycles():
    """A cycle makes the guard permanently unsatisfiable."""
    def reaches(start, target, seen=None):
        seen = seen or set()
        for p in ORDER_REQUIREMENTS.get(start, []):
            if p == target or (p not in seen and reaches(p, target, seen | {p})):
                return True
        return False
    for tool in ORDER_REQUIREMENTS:
        if reaches(tool, tool):
            return False, f"'{tool}' transitively requires itself — unsatisfiable"
    return True, "dependency graph is acyclic"


def test_guarded_tool_is_reachable_wherever_it_is_visible():
    """
    The HITL bug in one assertion. If a skill set can see a guarded tool, it
    must also be able to see that tool's prerequisites — otherwise Claude is
    handed a tool it can never successfully call. offboard-confirm is the known
    exception: it is reached only via run_flow_offboard_confirm(), which seeds
    the guard from verified DB state.
    """
    seeded_externally = {"offboard-confirm"}
    for skill, tools in SKILL_TOOLS.items():
        if skill in seeded_externally:
            continue
        visible = set(tools)
        for tool in visible:
            for prereq in ORDER_REQUIREMENTS.get(tool, []):
                # prereq may come from a skill loaded alongside this one
                available = visible | {t for s, ts in SKILL_TOOLS.items()
                                       for t in ts if s != skill}
                if prereq not in available:
                    return False, (f"skill '{skill}' exposes '{tool}' but '{prereq}' is "
                                   f"unreachable — the guard can never be cleared")
    return True, "every guarded tool has a reachable prerequisite"


# ── Security guarantees, as assertions ────────────────────────────

def test_prepare_phase_cannot_deactivate():
    visible = {t["name"] for t in tools_for_skills(
        ["_base", "lookup-user", "user-risk-profile", "offboard-prepare"])}
    if "deactivate_user" in visible:
        return False, "prepare phase can see deactivate_user — the HITL gate is bypassable"
    return True, "deactivate_user hidden from the prepare phase"


def test_confirm_phase_sees_only_deactivate():
    visible = {t["name"] for t in tools_for_skills(["_base", "offboard-confirm"])}
    if visible != {"deactivate_user"}:
        return False, (f"confirm phase sees {sorted(visible)}; it must see exactly "
                       f"{{deactivate_user}} so it cannot re-run checks or re-flag")
    return True, "confirm phase scoped to deactivate_user alone"


def test_unlisted_skill_gets_no_tools():
    """tools_for_skills must fail closed, not fall back to every tool."""
    visible = tools_for_skills(["_base"])
    if visible:
        return False, f"_base declares no tools but {len(visible)} were exposed"
    return True, "a skill declaring no tools exposes none"


def test_no_skill_combination_exceeds_the_cache_limit():
    """Ties the #3 regression to the real skill wiring rather than a fixed list."""
    import itertools
    names = list(SKILL_TOOLS)
    worst, worst_combo = 0, None
    for r in (1, 2, 3, 4):
        for combo in itertools.combinations(names, r):
            n = sum(1 for t in _cache_tools(tools_for_skills(list(combo)))
                    if "cache_control" in t) + 1
            if n > worst:
                worst, worst_combo = n, combo
    if worst > MAX_CACHE_BREAKPOINTS:
        return False, f"{worst_combo} needs {worst} breakpoints (limit {MAX_CACHE_BREAKPOINTS})"
    return True, f"worst case {worst}/{MAX_CACHE_BREAKPOINTS} over all 1-4 skill combinations"


# ── Client <-> orchestrator flow_type contract ────────────────────

CLI_PY = ROOT / "client" / "cli.py"
APP_PY = ROOT / "orchestrator" / "app.py"

# Endpoints the client can reach that dispatch on flow_type.
FLOW_ENDPOINTS = ("run_flow_endpoint", "run_flow_stream_endpoint")


def _client_flow_types() -> set[str]:
    """flow_type strings the client menu can send (3rd element of each entry)."""
    tree = ast.parse(CLI_PY.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for value in node.values:
                if isinstance(value, ast.Tuple) and len(value.elts) >= 3:
                    third = value.elts[2]
                    if isinstance(third, ast.Constant) and isinstance(third.value, str):
                        found.add(third.value)
    return found


def _handled_flow_types(endpoint: str) -> set[str]:
    """flow_type literals compared against req.flow_type inside one endpoint."""
    tree = ast.parse(APP_PY.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == endpoint), None)
    if fn is None:
        raise AssertionError(f"endpoint {endpoint}() not found in app.py")
    handled = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and "req.flow_type" in ast.unparse(node.left):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    handled.add(comp.value)
                elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    handled |= {e.value for e in comp.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return handled


def test_every_client_flow_type_is_handled_by_every_endpoint():
    """
    The client offers nine options; each maps to a flow_type sent to the
    orchestrator. Both /flow and /flow/stream dispatch on it independently, so
    a flow_type added to one and not the other fails only at runtime, for
    whichever endpoint the client happens to use.

    Regression: option 9 sent "risk-parallel-memory", which /flow handled and
    /flow/stream did not. The client streams, so option 9 returned
    "Unknown flow_type: risk-parallel-memory" while the non-streaming endpoint
    worked fine.
    """
    client = _client_flow_types()
    if not client:
        return False, "parsed no flow_types from the client menu — parser is out of date"
    for endpoint in FLOW_ENDPOINTS:
        unhandled = client - _handled_flow_types(endpoint)
        if unhandled:
            return False, (f"{endpoint}() does not handle {sorted(unhandled)} — the client "
                           f"can send these and will get 'Unknown flow_type'")
    return True, f"{len(client)} client flow_types handled by both endpoints"


def test_both_endpoints_handle_the_same_flow_types():
    """Neither endpoint should silently support more than the other."""
    a, b = (_handled_flow_types(e) for e in FLOW_ENDPOINTS)
    if a != b:
        only_a, only_b = sorted(a - b), sorted(b - a)
        return False, (f"{FLOW_ENDPOINTS[0]} only: {only_a}; "
                       f"{FLOW_ENDPOINTS[1]} only: {only_b}")
    return True, f"both endpoints handle the same {len(a)} flow_types"


TESTS = [
    test_every_client_flow_type_is_handled_by_every_endpoint,
    test_both_endpoints_handle_the_same_flow_types,
    test_user_tools_schemas_are_wellformed,
    test_tool_names_are_unique,
    test_every_declared_tool_exists_on_the_mcp_server,
    test_required_params_match_the_server_signature,
    test_server_tools_not_exposed_are_deliberate,
    test_skill_tools_reference_real_tools,
    test_every_skill_in_skill_tools_exists_on_disk,
    test_every_skill_on_disk_is_declared,
    test_order_requirements_reference_real_tools,
    test_order_requirements_have_no_cycles,
    test_guarded_tool_is_reachable_wherever_it_is_visible,
    test_prepare_phase_cannot_deactivate,
    test_confirm_phase_sees_only_deactivate,
    test_unlisted_skill_gets_no_tools,
    test_no_skill_combination_exceeds_the_cache_limit,
]


def main() -> int:
    print("\nInvariants — tool/skill wiring contracts (hermetic)")
    print("=" * 66)
    passed = 0
    for i, fn in enumerate(TESTS, 1):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        print(f"  [{i:2}/{len(TESTS)}] {'PASS' if ok else 'FAIL'}  {fn.__name__}")
        print(f"           {detail}" if not ok else "")
        passed += ok
    print(f"{'='*66}\nResults: {passed}/{len(TESTS)} passed")
    print("🟢 All passed" if passed == len(TESTS) else "🔴 Failures above")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
