#!/usr/bin/env python3
"""
Wiring check for the local open-weight provider.

Six checks, ~20 seconds, before you commit four minutes to a real flow. Each one
fails with the specific thing to fix rather than a stack trace, because every
failure here has a different cause: server down, wrong model id, no tool-calling
support, no forced tool choice, no reasoning traces.

Defaults target LM Studio. Override with LOCAL_BASE_URL / LOCAL_MODEL_ID for
vLLM or SGLang.

Run:  LLM_PROVIDER=local python scripts/local_smoke.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

os.environ.setdefault("LLM_PROVIDER",   "local")
os.environ.setdefault("LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
os.environ.setdefault("LOCAL_MODEL_ID", "meta/muse-glimmer")
# This script is about reachability and capability, not token accounting.
os.environ.setdefault("LLM_LOG_USAGE", "0")

if os.environ["LLM_PROVIDER"] not in ("local", "openai"):
    sys.exit(
        f"LLM_PROVIDER is {os.environ['LLM_PROVIDER']!r}. This script checks the "
        "the local/openai providers; set LLM_PROVIDER=local."
    )

try:
    from llm_client import CAPABILITIES, MODEL_ID, client  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name == "openai":
        sys.exit("The openai SDK is missing. pip install -r flows/requirements-local.txt")
    raise

from openai_compat_client import UnsupportedFeature  # noqa: E402

BASE_URL = os.environ["LOCAL_BASE_URL"]

TOOL = {
    "name": "get_user",
    "description": "Fetch a user record by ID",
    "input_schema": {
        "type": "object",
        "properties": {"user_id": {"type": "string"}},
        "required": ["user_id"],
    },
}


async def check_reachable():
    models = await client._aclient.models.list()
    ids = [m.id for m in models.data]
    if MODEL_ID not in ids:
        return False, f"{MODEL_ID!r} is not served here. Available: {ids}"
    return True, f"{MODEL_ID} is loaded"


async def check_completion():
    """
    max_tokens=400 for a one-word answer is not padding.

    This model reasons before every reply and the reasoning is billed against
    the same budget. At max_tokens=64 it spends all 64 thinking, returns empty
    content and finish_reason="length" — an answer that looks like a broken
    server but is just a budget too small to reach the text. Anything calling
    this provider needs headroom well above what the visible answer costs.
    """
    r = await client.messages.create(
        model=MODEL_ID, max_tokens=400, temperature=0,
        messages=[{"role": "user", "content": "Reply with the single word: ready"}],
    )
    text = "".join(b.text for b in r.content if b.type == "text")
    if not text.strip():
        if r.stop_reason == "max_tokens":
            return False, "Truncated before producing text — raise max_tokens."
        return False, f"Empty response with stop_reason={r.stop_reason}"
    return True, f"responded {text.strip()[:40]!r}, stop_reason={r.stop_reason}"


async def check_tool_call():
    r = await client.messages.create(
        model=MODEL_ID, max_tokens=512, temperature=0,
        tools=[TOOL],
        messages=[{"role": "user", "content": "Look up user usr_001."}],
    )
    calls = [b for b in r.content if b.type == "tool_use"]
    if not calls:
        return False, "No tool call emitted. This model or server cannot drive the agentic loop."
    if calls[0].input.get("user_id") != "usr_001":
        return False, f"Tool called with wrong arguments: {calls[0].input}"
    return True, f"called {calls[0].name}({calls[0].input})"


async def check_forced_tool_choice():
    """
    The judge helpers in tools.py depend on this. A server that treats
    tool_choice as a hint degrades them to _first_tool_input's fail-open path,
    which silently reports 'complete' and 'no issues' for every run.
    """
    r = await client.messages.create(
        model=MODEL_ID, max_tokens=512, temperature=0,
        tools=[TOOL], tool_choice={"type": "any"},
        messages=[{"role": "user", "content": "What is the capital of France? Use no tools."}],
    )
    if not any(b.type == "tool_use" for b in r.content):
        return False, (
            "tool_choice was ignored — the judges will fail open on every round. "
            "Drop 'forced_tool_choice' from this provider in llm_client._CAPABILITIES."
        )
    return True, "tool_choice=required is enforced"


async def check_reasoning_trace():
    r = await client.messages.create(
        model=MODEL_ID, max_tokens=600, temperature=0,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": "Is 91 prime? Think it through."}],
    )
    traces = [b for b in r.content if b.type == "thinking"]
    if not traces:
        return False, (
            "No reasoning_content returned. Scores stay valid but the [THINKING] "
            "audit trail will be empty — drop 'thinking_blocks' from this "
            "provider in llm_client._CAPABILITIES, or start vLLM with a "
            "--reasoning-parser."
        )
    return True, f"{len(traces[0].thinking)} chars of trace returned"


async def check_refusals():
    """The adapter must refuse what it cannot honour rather than dropping it."""
    try:
        await client.messages.create(
            model=MODEL_ID, max_tokens=32,
            messages=[{"role": "user", "content": "hi"}],
            some_parameter_that_does_not_exist=True,
        )
    except UnsupportedFeature:
        return True, "unknown parameters are refused, not silently dropped"
    return False, "an unrecognised parameter reached the wire"


CHECKS = [
    ("server reachable",     check_reachable),
    ("plain completion",     check_completion),
    ("tool calling",         check_tool_call),
    ("forced tool choice",   check_forced_tool_choice),
    ("reasoning traces",     check_reasoning_trace),
    ("adapter refusals",     check_refusals),
]


async def main() -> int:
    print(f"\nLocal provider wiring check — {BASE_URL}")
    print(f"model: {MODEL_ID}   capabilities: {sorted(CAPABILITIES)}")
    print("=" * 70)

    passed = 0
    for name, check in CHECKS:
        t0 = time.time()
        try:
            ok, detail = await check()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
            if "Connection" in type(e).__name__ or "connect" in str(e).lower():
                detail += "\n           Is the server up? LM Studio: `lms server start`"
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} ({time.time()-t0:5.1f}s)  {detail}")
        passed += ok
        if not ok and name == "server reachable":
            print("\n  Stopping — nothing else can pass while the server is unreachable.")
            break

    print("=" * 70)
    print(f"{passed}/{len(CHECKS)} passed")
    if passed == len(CHECKS):
        print("\n🟢 Ready. Next:  ./scripts/local.sh")
        print("   Expect ~4 minutes per flow on a 30B running locally.")
    return 0 if passed == len(CHECKS) else 1


sys.exit(asyncio.run(main()))
