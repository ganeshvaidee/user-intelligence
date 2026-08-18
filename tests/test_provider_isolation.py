#!/usr/bin/env python3
"""
Provider isolation — the guardrail that keeps the flows Anthropic-shaped.

Hermetic: no model, no MCP server, no credentials. Pure source inspection.

Why this file exists
--------------------
The Anthropic path is privileged in this repo: the flows are written
unconditionally in its shape — cache_control on the system prompt and tools,
adaptive thinking, output_config effort, forced tool_choice, messages.stream.
The other providers are adapted to that shape inside
flows/openai_compat_client.py and are allowed to support less.

That arrangement decays in exactly two ways, both of which look like small,
reasonable commits at review time:

  1. `import openai` appears in a second file, and provider-specific handling
     starts spreading through the flows.
  2. `if LLM_PROVIDER == ...` appears outside llm_client.py, and the flows turn
     into a lowest-common-denominator abstraction where the Anthropic path is no
     longer the readable one.

Prose in CLAUDE.md does not stop either. These tests do.

Run:  python tests/test_provider_isolation.py
"""

import ast
import re
import sys
from pathlib import Path

ROOT  = Path(__file__).parent.parent
FLOWS = ROOT / "flows"

# The one file permitted to speak OpenAI.
OPENAI_QUARANTINE = {"openai_compat_client.py"}

# The client modules: files whose whole job is knowing which vendor is in play.
# Everything else in flows/ is orchestration and must read the same regardless.
# The three client modules are included because they legitimately name
# LLM_PROVIDER in their error messages ("unset LLM_PROVIDER to use ...").
CLIENT_MODULES = {
    "llm_client.py",
    "anthropic_client.py",
    "bedrock_client.py",
    "openai_compat_client.py",
}

_OPENAI_IMPORT = re.compile(r"^\s*(?:import\s+openai|from\s+openai(?:\.\S+)?\s+import)", re.M)
_PROVIDER_REF  = re.compile(r"\bLLM_PROVIDER\b")


def _flow_files():
    return sorted(p for p in FLOWS.glob("*.py") if p.name != "__init__.py")


def _declared_capabilities() -> dict[str, set[str]]:
    """
    Read _CAPABILITIES out of llm_client.py without importing it.

    Importing would construct a real client — which needs credentials, and on
    the default path needs the anthropic SDK installed. This test has to stay
    runnable on a machine set up for the local provider only, so it parses.
    """
    tree = ast.parse((FLOWS / "llm_client.py").read_text())
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "_CAPABILITIES":
            return {k: set(v) for k, v in ast.literal_eval(node.value).items()}
    raise AssertionError("_CAPABILITIES not found in llm_client.py")


def _strip_comments(source: str) -> str:
    """
    Drop whole-line comments so the module headers, which necessarily discuss
    `openai` and LLM_PROVIDER by name, do not trip the scanners. Docstrings are
    left alone deliberately — the regexes only match import statements and a
    bare identifier, neither of which appears in prose here.
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def test_openai_import_is_quarantined():
    offenders = [
        path.name
        for path in _flow_files()
        if path.name not in OPENAI_QUARANTINE and _OPENAI_IMPORT.search(_strip_comments(path.read_text()))
    ]
    if offenders:
        return False, (
            f"`openai` imported outside the quarantine by: {offenders}. "
            f"Non-Anthropic providers must be adapted inside "
            f"{sorted(OPENAI_QUARANTINE)[0]}, not leak into the flows."
        )
    return True, ""


def test_flows_do_not_branch_on_provider():
    offenders = [
        path.name
        for path in _flow_files()
        if path.name not in CLIENT_MODULES and _PROVIDER_REF.search(_strip_comments(path.read_text()))
    ]
    if offenders:
        return False, (
            f"LLM_PROVIDER referenced outside the client modules by: {offenders}. "
            f"Flows stay Anthropic-shaped; where a capability gap genuinely "
            f"matters use llm_client.supports(<feature>), which names the "
            f"capability rather than the vendor."
        )
    return True, ""


def test_capabilities_cover_every_provider():
    """
    Every branch of the client resolution needs a capability entry, or
    supports() silently answers False for a provider that can in fact do the
    thing — which would downgrade Anthropic itself if a key were ever renamed.
    """
    source   = (FLOWS / "llm_client.py").read_text()
    declared = set(_declared_capabilities())
    resolved = set(re.findall(r'LLM_PROVIDER\s*(?:==|in)\s*\(?\s*["\'](\w+)["\']', source))
    # `in ("local", "openai")` — pick up the tail of the tuple too.
    resolved |= {
        name
        for name in re.findall(r'["\'](\w+)["\']', source)
        if name in declared
    }

    missing = resolved - declared
    if missing:
        return False, f"Providers resolved but not declared in _CAPABILITIES: {sorted(missing)}"
    if "anthropic" not in declared:
        return False, "The default provider 'anthropic' has no capability entry."
    return True, ""


def test_anthropic_keeps_every_capability():
    """
    The anthropic entry is the reference. If a capability is ever dropped from it
    to make some other provider's life easier, the arrangement has been inverted.
    """
    capabilities = _declared_capabilities()
    everything   = set().union(*capabilities.values())
    missing      = everything - capabilities["anthropic"]
    if missing:
        return False, (
            f"Capabilities declared for other providers but not for anthropic: "
            f"{sorted(missing)}. The anthropic set must be a superset."
        )
    return True, ""


TESTS = [
    test_openai_import_is_quarantined,
    test_flows_do_not_branch_on_provider,
    test_capabilities_cover_every_provider,
    test_anthropic_keeps_every_capability,
]


def main() -> int:
    print("\nProvider isolation — source guardrails (hermetic, no credentials)")
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
