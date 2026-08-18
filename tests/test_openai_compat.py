#!/usr/bin/env python3
"""
Translation unit tests for the OpenAI-compatible provider adapter.

Hermetic: no model, no server, no MCP, no credentials. Pure function calls
against flows/openai_compat_client.py.

Why this file exists
--------------------
The adapter's whole job is to be invisible. run_flow.py sends cache_control,
output_config, forced tool_choice and Anthropic-shaped messages, and expects
back objects with .type / .input / .stop_reason / .usage. Every one of those
translations fails *quietly* when it breaks:

  - a dropped cache_control looks like a working call that costs 10x more
  - a mistranslated tool_choice looks like a judge that "found no issues"
  - a mis-keyed tool_result id looks like a model that ignored the tool output
  - cached_tokens left in prompt_tokens looks like a 0% cache hit rate

None of those raise, and the model's prose output looks fine in all four cases.
So they get asserted here rather than noticed in production.

Skips cleanly when `openai` is not installed — the default Anthropic path must
not require it. Install with: pip install -r flows/requirements-local.txt

Run:  python tests/test_openai_compat.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "flows"))

try:
    from openai_compat_client import (  # noqa: E402
        Message,
        TextBlock,
        ToolUseBlock,
        UnsupportedFeature,
        Usage,
        _build_request,
        _convert_usage,
        _parse_arguments,
        _STOP_REASON,
        _to_message,
    )
    OPENAI_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "openai":
        raise
    OPENAI_AVAILABLE = False


# A request the way run_flow.py actually builds one: cache_control on the
# system block and on the last tool, Anthropic tool schemas, forced choice.
def _anthropic_request(**overrides):
    base = dict(
        model       = "muse-glimmer-30b",
        max_tokens  = 4096,
        temperature = 0,
        system      = [{"type": "text", "text": "SKILLS", "cache_control": {"type": "ephemeral"}}],
        tools       = [{
            "name": "get_user",
            "description": "Fetch a user",
            "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}},
            "cache_control": {"type": "ephemeral"},
        }],
        tool_choice = {"type": "any"},
        messages    = [{"role": "user", "content": "Look up usr_001"}],
    )
    base.update(overrides)
    return base


# ── Refusals: never silently drop an Anthropic feature ────────────

def test_thinking_is_accepted_and_sends_nothing():
    """
    Verified against Muse Glimmer on LM Studio: the model reasons natively and
    returns the trace in reasoning_content, so `thinking` needs no wire
    representation — only output_config's depth. It must not appear as a key,
    which no OpenAI-compatible server would accept.
    """
    req = _build_request(**_anthropic_request(
        thinking      = {"type": "adaptive", "display": "summarized"},
        output_config = {"effort": "high"},
    ))
    if "thinking" in req:
        return False, "thinking leaked onto the wire"
    if not req["messages"][0]["content"].rstrip().endswith("Reasoning strength: high"):
        return False, "the depth half of the request was lost"
    return True, ""


def test_unknown_thinking_type_is_refused():
    try:
        _build_request(**_anthropic_request(thinking={"type": "disabled"}))
    except UnsupportedFeature:
        return True, ""
    return False, "an unrecognised thinking type was accepted silently"


def test_reasoning_content_becomes_a_thinking_block():
    """
    run_flow.py prints `block.thinking` for `block.type == "thinking"`. That is
    the entire contract, and it is what makes the dimension agents auditable on
    a local model.
    """
    msg = _to_message(
        _FakeCompletion(_FakeMessage("Score: 3", reasoning="Weighing MFA absence..."), "stop"),
        "m",
    )
    kinds = [b.type for b in msg.content]
    if kinds != ["thinking", "text"]:
        return False, f"blocks are {kinds}, expected thinking before text"
    if msg.content[0].thinking != "Weighing MFA absence...":
        return False, f"trace mistranslated: {msg.content[0].thinking!r}"
    return True, ""


def test_reasoning_content_read_from_model_extra():
    """
    reasoning_content is not in the formal OpenAI schema, so the SDK parks it in
    model_extra rather than exposing an attribute. Reading only the attribute
    would silently lose every trace.
    """
    msg = _to_message(
        _FakeCompletion(_FakeMessage("done", extra={"reasoning_content": "hidden trace"}), "stop"),
        "m",
    )
    thinking = [b for b in msg.content if b.type == "thinking"]
    if not thinking:
        return False, "a trace in model_extra was dropped"
    if thinking[0].thinking != "hidden trace":
        return False, f"trace mistranslated: {thinking[0].thinking!r}"
    return True, ""


def test_unknown_anthropic_parameter_is_refused():
    """A new parameter added to a flow must be translated or refused, never dropped."""
    try:
        _build_request(**_anthropic_request(some_future_param={"x": 1}))
    except UnsupportedFeature:
        return True, ""
    return False, "an unrecognised parameter reached the wire silently"


def test_unknown_effort_level_is_refused():
    try:
        _build_request(**_anthropic_request(output_config={"effort": "maximum"}))
    except UnsupportedFeature:
        return True, ""
    return False, "a typo'd effort would silently mean 'provider default'"


# ── Request translation ───────────────────────────────────────────

def test_effort_becomes_a_reasoning_directive():
    """Muse Glimmer takes reasoning depth as a system-prompt line, same vocabulary."""
    req    = _build_request(**_anthropic_request(output_config={"effort": "high"}))
    system = req["messages"][0]
    if system["role"] != "system":
        return False, f"first message is {system['role']!r}, expected 'system'"
    if not system["content"].rstrip().endswith("Reasoning strength: high"):
        return False, f"no reasoning directive appended: {system['content']!r}"
    if "output_config" in req:
        return False, "output_config leaked onto the wire"
    return True, ""


def test_cache_control_never_reaches_the_wire():
    """
    vLLM has no breakpoint to place. The marker must vanish in translation —
    and usage.py must stop calling that DEAD, which is asserted separately in
    test_provider_isolation via the capability declaration.
    """
    req = _build_request(**_anthropic_request())
    if "cache_control" in json.dumps(req):
        return False, "a cache_control marker survived into the OpenAI request"
    return True, ""


def test_forced_tool_choice_maps_to_required():
    """The judge helpers in tools.py depend on this one being enforced."""
    req = _build_request(**_anthropic_request())
    if req.get("tool_choice") != "required":
        return False, f"tool_choice is {req.get('tool_choice')!r}, expected 'required'"
    return True, ""


def test_tool_schema_shape():
    req  = _build_request(**_anthropic_request())
    tool = req["tools"][0]
    if tool.get("type") != "function":
        return False, f"tool type is {tool.get('type')!r}"
    fn = tool["function"]
    if fn["name"] != "get_user" or "parameters" not in fn:
        return False, f"input_schema was not renamed to parameters: {fn}"
    return True, ""


def test_tool_results_become_tool_role_messages_in_order():
    """
    Anthropic packs all tool results into one user turn; OpenAI wants one
    role=tool message each, and every compliant server requires them to follow
    the assistant message that requested them.
    """
    messages = [
        {"role": "user", "content": "Look up usr_001"},
        {"role": "assistant", "content": [
            TextBlock(text="Looking that up."),
            ToolUseBlock(id="call_1", name="get_user", input={"user_id": "usr_001"}),
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": '{"name": "Alice Chen"}'},
        ]},
    ]
    req   = _build_request(**_anthropic_request(messages=messages))
    roles = [m["role"] for m in req["messages"]]

    if "tool" not in roles:
        return False, f"no tool-role message produced: {roles}"
    if roles.index("tool") != roles.index("assistant") + 1:
        return False, f"tool message does not directly follow the assistant turn: {roles}"

    assistant = req["messages"][roles.index("assistant")]
    if not assistant.get("tool_calls"):
        return False, "the assistant's tool_use block did not become tool_calls"
    call = assistant["tool_calls"][0]
    if call["id"] != "call_1":
        return False, f"tool_call id changed: {call['id']!r}"
    if json.loads(call["function"]["arguments"]) != {"user_id": "usr_001"}:
        return False, f"arguments were not JSON-encoded correctly: {call}"

    tool_msg = req["messages"][roles.index("tool")]
    if tool_msg["tool_call_id"] != "call_1":
        return False, f"tool_call_id does not match the request: {tool_msg}"
    return True, ""


# ── Response translation ──────────────────────────────────────────

class _FakeFunction:
    def __init__(self, name, arguments):
        self.name      = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id       = id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content, tool_calls=None, reasoning=None, extra=None):
        self.content    = content
        self.tool_calls = tool_calls
        if reasoning is not None:
            self.reasoning_content = reasoning
        # Mirrors how the OpenAI SDK parks undeclared fields.
        self.model_extra = extra or {}


class _FakeChoice:
    def __init__(self, message, finish_reason):
        self.message       = message
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self, message, finish_reason, usage=None):
        self.choices = [_FakeChoice(message, finish_reason)]
        self.usage   = usage


class _FakeDetails:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class _FakeUsage:
    def __init__(self, prompt, completion, cached=0):
        self.prompt_tokens         = prompt
        self.completion_tokens     = completion
        self.prompt_tokens_details = _FakeDetails(cached)


def test_stop_reason_maps_to_end_turn():
    """_run_tool_loop exits on end_turn and nothing else; a bad map is an infinite loop."""
    if _STOP_REASON.get("stop") != "end_turn":
        return False, "finish_reason 'stop' must become 'end_turn'"
    if _STOP_REASON.get("tool_calls") != "tool_use":
        return False, "finish_reason 'tool_calls' must become 'tool_use'"

    msg = _to_message(_FakeCompletion(_FakeMessage("done"), "stop"), "m")
    if msg.stop_reason != "end_turn":
        return False, f"stop_reason is {msg.stop_reason!r}"
    if [b.type for b in msg.content] != ["text"]:
        return False, f"blocks are {[b.type for b in msg.content]}"
    return True, ""


def test_tool_calls_become_tool_use_blocks():
    completion = _FakeCompletion(
        _FakeMessage(None, [_FakeToolCall("call_9", "get_user", '{"user_id": "usr_005"}')]),
        "tool_calls",
    )
    msg   = _to_message(completion, "m")
    block = msg.content[0]
    if block.type != "tool_use":
        return False, f"block type is {block.type!r}"
    if (block.id, block.name, block.input) != ("call_9", "get_user", {"user_id": "usr_005"}):
        return False, f"block mistranslated: {block.id} {block.name} {block.input}"
    if msg.stop_reason != "tool_use":
        return False, f"stop_reason is {msg.stop_reason!r}"
    return True, ""


def test_malformed_tool_arguments_do_not_raise():
    """
    A truncated argument string must reach the MCP server as a bad argument —
    which the model can recover from — rather than killing the flow.
    """
    parsed = _parse_arguments('{"user_id": "usr_0')
    if "_malformed_arguments" not in parsed:
        return False, f"expected a surfaced error, got {parsed}"
    if _parse_arguments("") != {}:
        return False, "an empty argument string should parse to {}"
    return True, ""


def test_cached_tokens_map_onto_cache_read():
    """
    vLLM includes prefix-cache hits in prompt_tokens; Anthropic excludes them
    from input_tokens. Without the subtraction usage.py double-counts and the
    reported hit rate is wrong in the flattering direction.
    """
    usage = _convert_usage(_FakeUsage(prompt=2500, completion=40, cached=2048))
    if usage.cache_read_input_tokens != 2048:
        return False, f"cache_read is {usage.cache_read_input_tokens}"
    if usage.input_tokens != 452:
        return False, f"input_tokens is {usage.input_tokens}, expected 2500-2048=452"
    if usage.cache_creation_input_tokens != 0:
        return False, "there is no write counter on this provider; it must stay 0"

    # A server that reports no cache details at all must not crash the logger.
    bare = _convert_usage(_FakeUsage(prompt=100, completion=10))
    if (bare.input_tokens, bare.cache_read_input_tokens) != (100, 0):
        return False, f"bare usage mishandled: {bare.input_tokens}/{bare.cache_read_input_tokens}"
    return True, ""


def test_message_is_duck_compatible_with_the_sdk():
    """run_flow only ever touches these attributes; they are the whole contract."""
    msg = Message(content=[TextBlock("hi")], stop_reason="end_turn", usage=Usage(), model="m")
    for attr in ("content", "stop_reason", "usage", "role", "type"):
        if not hasattr(msg, attr):
            return False, f"Message is missing .{attr}"
    if msg.role != "assistant":
        return False, f"role is {msg.role!r}"
    if not hasattr(msg.usage, "cache_read_input_tokens"):
        return False, "Usage is missing the cache fields usage.py reads defensively"
    return True, ""


# ── Streaming events ──────────────────────────────────────────────
#
# run_dimension_agent iterates `async for event in stream` and keys on
# event.type == "thinking" / "text". That loop is written once and runs on both
# providers, so these shapes are the contract with the Anthropic SDK — if they
# drift, options 8 and 9 stop streaming reasoning on the local provider with no
# error, just silence.

class _FakeSSEStream:
    """Minimal stand-in for openai's AsyncStream over chat.completions chunks."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed  = False

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()

    async def close(self):
        self.closed = True


class _FakeDelta:
    def __init__(self, content=None, reasoning=None):
        self.content    = content
        self.tool_calls = None
        if reasoning is not None:
            self.reasoning_content = reasoning
        self.model_extra = {}


class _FakeStreamChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta         = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, delta=None, finish_reason=None, usage=None):
        self.choices = [_FakeStreamChoice(delta, finish_reason)] if delta is not None else []
        self.usage   = usage


def _stream_over(chunks):
    from openai_compat_client import _MessageStream
    stream = _MessageStream(aclient=None, request={}, model="m")
    stream._stream = _FakeSSEStream(chunks)
    return stream


def _run(coro):
    import asyncio
    return asyncio.run(coro)


_CHUNKS = [
    _FakeChunk(_FakeDelta(reasoning="Checking MFA")),
    _FakeChunk(_FakeDelta(reasoning=" → +2\n")),
    _FakeChunk(_FakeDelta(content="Score")),
    _FakeChunk(_FakeDelta(content=": 4")),
    _FakeChunk(_FakeDelta(content=""), finish_reason="stop"),
]


def test_stream_emits_thinking_and_text_events():
    async def go():
        stream = _stream_over(_CHUNKS)
        return [(e.type, getattr(e, "thinking", None) or getattr(e, "text", None))
                async for e in stream]

    events = _run(go())
    kinds  = [t for t, _ in events]
    if kinds != ["thinking", "thinking", "text", "text"]:
        return False, f"event types are {kinds}"
    if "".join(v for t, v in events if t == "thinking") != "Checking MFA → +2\n":
        return False, "thinking deltas did not reassemble"
    if "".join(v for t, v in events if t == "text") != "Score: 4":
        return False, "text deltas did not reassemble"
    return True, ""


def test_event_snapshot_accumulates():
    """The SDK's events carry a running snapshot; mirroring it keeps callers portable."""
    async def go():
        stream = _stream_over(_CHUNKS)
        return [e.snapshot async for e in stream if e.type == "thinking"]

    snaps = _run(go())
    if snaps[-1] != "Checking MFA → +2\n":
        return False, f"final thinking snapshot is {snaps[-1]!r}"
    if len(snaps[0]) >= len(snaps[-1]):
        return False, "snapshot did not grow across deltas"
    return True, ""


def test_text_stream_excludes_thinking():
    """
    text_stream must keep meaning "the visible reply" on this provider exactly as
    it does on Anthropic. The five non-dimension call sites still use it, and
    leaking reasoning into it would print traces into flow output.
    """
    async def go():
        stream = _stream_over(_CHUNKS)
        return [t async for t in stream.text_stream]

    if "".join(_run(go())) != "Score: 4":
        return False, "reasoning leaked into text_stream"
    return True, ""


def test_stream_views_share_one_iterator():
    """
    Both views read one memoised generator, as in the SDK where text_stream is
    built once in __init__. Two independent generators would each consume half
    the feed and neither would see the whole response.
    """
    async def go():
        stream = _stream_over(_CHUNKS)
        first  = [t async for t in stream.text_stream]
        second = [t async for t in stream.text_stream]   # already exhausted
        return first, second

    first, second = _run(go())
    if "".join(first) != "Score: 4":
        return False, f"first pass got {first}"
    if second:
        return False, f"second pass re-consumed the feed: {second}"
    return True, ""


def test_get_final_message_after_streaming_has_both_blocks():
    async def go():
        stream = _stream_over(_CHUNKS)
        async for _ in stream:
            pass
        return await stream.get_final_message()

    msg   = _run(go())
    kinds = [b.type for b in msg.content]
    if kinds != ["thinking", "text"]:
        return False, f"final blocks are {kinds}, expected thinking then text"
    if msg.stop_reason != "end_turn":
        return False, f"stop_reason is {msg.stop_reason!r}"
    return True, ""


def test_drained_stream_is_closed_in_frame():
    """
    Closing when the SSE feed ends, rather than at GC, is what keeps httpcore
    from printing 'generator didn't stop after athrow()' after a correct answer.
    """
    async def go():
        stream = _stream_over(_CHUNKS)
        async for _ in stream:
            pass
        return stream._stream.closed

    if not _run(go()):
        return False, "stream was not closed when the feed ended"
    return True, ""


TESTS = [
    test_stream_emits_thinking_and_text_events,
    test_event_snapshot_accumulates,
    test_text_stream_excludes_thinking,
    test_stream_views_share_one_iterator,
    test_get_final_message_after_streaming_has_both_blocks,
    test_drained_stream_is_closed_in_frame,
    test_thinking_is_accepted_and_sends_nothing,
    test_unknown_thinking_type_is_refused,
    test_reasoning_content_becomes_a_thinking_block,
    test_reasoning_content_read_from_model_extra,
    test_unknown_anthropic_parameter_is_refused,
    test_unknown_effort_level_is_refused,
    test_effort_becomes_a_reasoning_directive,
    test_cache_control_never_reaches_the_wire,
    test_forced_tool_choice_maps_to_required,
    test_tool_schema_shape,
    test_tool_results_become_tool_role_messages_in_order,
    test_stop_reason_maps_to_end_turn,
    test_tool_calls_become_tool_use_blocks,
    test_malformed_tool_arguments_do_not_raise,
    test_cached_tokens_map_onto_cache_read,
    test_message_is_duck_compatible_with_the_sdk,
]


def main() -> int:
    print("\nOpenAI-compatible adapter — translation tests (hermetic, no credentials)")
    print("=" * 74)

    if not OPENAI_AVAILABLE:
        print("  SKIP  `openai` is not installed — local/openai providers are an optional extra.")
        print("        pip install -r flows/requirements-local.txt")
        print(f"\n{'='*74}\n🟡 Skipped")
        return 0

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
    print(f"\n{'='*74}\nResults: {passed}/{len(TESTS)} passed")
    print("🟢 All passed" if passed == len(TESTS) else "🔴 Failures above")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
