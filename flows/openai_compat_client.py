# flows/openai_compat_client.py
#
# An OpenAI-compatible /v1/chat/completions endpoint presented
# as the Anthropic Messages API. Covers both a local open-weight model served by
# vLLM/SGLang (LLM_PROVIDER=local, the Muse Glimmer path) and the hosted OpenAI
# API (LLM_PROVIDER=openai).
#
# THIS IS THE ONLY FILE IN THE REPO ALLOWED TO IMPORT `openai`.
# run_flow.py and tools.py stay unconditionally Anthropic-shaped — they send
# cache_control, thinking, output_config and tool_choice={"type": "any"} and
# read block.type / response.stop_reason / response.usage.cache_read_input_tokens
# without ever knowing which provider is underneath. The quarantine is enforced
# by tests/test_provider_isolation.py.
#
# Adaptation happens here and only here: this file makes the provider look like
# the Anthropic API. Where it cannot, it raises UnsupportedFeature. It never
# accepts an Anthropic-only parameter and quietly drops it — a silent no-op
# looks identical to a working feature, which is precisely the failure this
# repo is meant to avoid demonstrating.
#
# What maps, and how:
#
#   output_config={"effort": "high"}     → "Reasoning strength: high" appended to
#                                          the system prompt. Muse Glimmer uses
#                                          the same low/medium/high/xhigh
#                                          vocabulary, so this is a rename, not
#                                          an approximation.
#   tool_choice={"type": "any"}          → tool_choice="required". Real on
#                                          vLLM/SGLang, which enforce it with
#                                          guided decoding; that is what keeps
#                                          the judge helpers in tools.py honest.
#   cache_control breakpoints            → dropped. vLLM prefix-caches
#                                          automatically with no breakpoint to
#                                          place. Hits come back as
#                                          prompt_tokens_details.cached_tokens
#                                          and are mapped onto
#                                          cache_read_input_tokens so usage.py
#                                          still reports a hit rate.
#   thinking={"type": "adaptive", ...}   → nothing on the wire. The model
#                                          reasons natively and returns the
#                                          trace in reasoning_content, which
#                                          becomes a ThinkingBlock. Only the
#                                          depth needs sending, and
#                                          output_config already carries it.

import json
import logging
import os

import openai

logger = logging.getLogger(__name__)


class UnsupportedFeature(RuntimeError):
    """An Anthropic-only parameter reached a provider that cannot honour it."""


# ── Anthropic-shaped response objects ─────────────────────────────
#
# Hand-rolled rather than reused from anthropic.types. The flows only ever touch
# .type / .text / .input / .id / .name / .stop_reason / .usage.*, and owning
# these four classes keeps the adapter clear of the SDK's pydantic
# required-field churn across versions. They are duck-compatible with the real
# blocks, which is the whole contract.
#
# These are also fed straight back in via msgs.append({"role": "assistant",
# "content": response.content}), so _convert_messages below has to read them
# again on the next turn — hence _block_attr handling both dicts and objects.


class TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class ThinkingBlock:
    """
    Mirrors Anthropic's thinking block: run_flow.py reads `.type == "thinking"`
    and prints `.thinking`. Populated from the OpenAI-compat
    `message.reasoning_content` field, which LM Studio returns natively and
    vLLM returns when started with a --reasoning-parser.
    """
    type = "thinking"

    def __init__(self, thinking: str):
        self.thinking = thinking


class ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):
        self.id    = id
        self.name  = name
        self.input = input


class Usage:
    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ):
        self.input_tokens                = input_tokens
        self.output_tokens               = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens     = cache_read_input_tokens


class TextEvent:
    """
    Mirrors anthropic.lib.streaming.TextEvent: `.type == "text"`, `.text` is the
    incremental delta, `.snapshot` the accumulated text so far.
    """
    type = "text"

    def __init__(self, text: str, snapshot: str):
        self.text     = text
        self.snapshot = snapshot


class ThinkingEvent:
    """
    Mirrors anthropic.lib.streaming.ThinkingEvent: `.type == "thinking"`,
    `.thinking` is the incremental delta, `.snapshot` the trace so far.

    This is the surface that lets one loop work on both providers:

        async for event in stream:
            if   event.type == "thinking": event.thinking
            elif event.type == "text":     event.text
    """
    type = "thinking"

    def __init__(self, thinking: str, snapshot: str):
        self.thinking = thinking
        self.snapshot = snapshot


class Message:
    type = "message"
    role = "assistant"

    def __init__(self, content: list, stop_reason: str | None, usage: Usage, model: str = ""):
        self.content     = content
        self.stop_reason = stop_reason
        self.usage       = usage
        self.model       = model


# ── Request translation: Anthropic → OpenAI ───────────────────────

# run_flow.py breaks its loop on stop_reason == "end_turn"; everything else here
# is mapped for parity with what the Anthropic SDK would have returned.
_STOP_REASON = {
    "stop":           "end_turn",
    "tool_calls":     "tool_use",
    "function_call":  "tool_use",
    "length":         "max_tokens",
    "content_filter": "refusal",
}

_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}


def _system_text(system) -> str:
    """
    Flatten Anthropic's `system` (a string, or a list of text blocks) to a string.

    cache_control markers on those blocks are simply not copied — this provider
    has no breakpoint to place. That is a documented capability gap declared in
    llm_client.CAPABILITIES, not a silent drop: usage.py consults the same
    declaration and stops reporting DEAD breakpoints rather than flagging every
    call as a caching bug.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(
        block.get("text", "")
        for block in system
        if block.get("type", "text") == "text"
    )


def _apply_effort(system_text: str, output_config: dict | None) -> str:
    """
    Fold output_config effort into the system prompt as a reasoning directive.

    Muse Glimmer reads `Reasoning strength: low|medium|high|xhigh` from the
    system prompt and uses the same four levels as Anthropic's effort control,
    so nothing is lost in translation. An unrecognised level is refused rather
    than passed through — a typo'd effort that silently means "default" is the
    exact class of bug this adapter exists to prevent.
    """
    if not output_config:
        return system_text

    effort = output_config.get("effort")
    if effort is None:
        return system_text
    if effort not in _EFFORT_LEVELS:
        raise UnsupportedFeature(
            f"Unknown effort {effort!r}; expected one of {sorted(_EFFORT_LEVELS)}"
        )
    return f"{system_text}\n\nReasoning strength: {effort}".strip()


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    """
    Anthropic {name, description, input_schema} → OpenAI function-tool shape.

    _cache_tools() in run_flow.py puts a cache_control key on the last tool;
    only the three keys below are read, so the marker falls away here.
    """
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name":        tool["name"],
                "description": tool.get("description", ""),
                "parameters":  tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _convert_tool_choice(tool_choice: dict | None):
    """
    {"type": "any"} → "required" is the load-bearing one: it is what makes the
    forced-structured-output trick in tools.py (_check_completeness,
    _critique_response) work off-Anthropic. vLLM and SGLang enforce it with
    guided decoding. On a backend that only treats it as a hint the judges
    degrade through _first_tool_input()'s fail-open path rather than crashing.
    """
    if tool_choice is None:
        return None

    kind = tool_choice.get("type")
    if kind == "any":
        return "required"
    if kind in ("auto", "none"):
        return kind
    if kind == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    raise UnsupportedFeature(f"Unsupported tool_choice: {tool_choice!r}")


def _block_attr(block, name: str, default=None):
    """
    Read a content block that may be either a dict (built inline by run_flow) or
    one of the classes above (echoed back from a previous assistant turn).
    """
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _convert_messages(messages: list[dict]) -> list[dict]:
    """
    Anthropic turns → OpenAI turns.

    The one non-obvious part is tool results. Anthropic packs every tool_result
    for a turn into a single user message; OpenAI wants one {"role": "tool"}
    message per result. Order is preserved so each tool message still follows
    the assistant message that requested it, which OpenAI-compatible servers
    require.
    """
    out: list[dict] = []

    for msg in messages:
        role    = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "".join(
                _block_attr(b, "text", "")
                for b in content
                if _block_attr(b, "type") == "text"
            )
            tool_calls = [
                {
                    "id":   _block_attr(b, "id"),
                    "type": "function",
                    "function": {
                        "name":      _block_attr(b, "name"),
                        "arguments": json.dumps(_block_attr(b, "input", {})),
                    },
                }
                for b in content
                if _block_attr(b, "type") == "tool_use"
            ]
            entry = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        # User turn carrying tool results, and possibly trailing text.
        trailing_text = []
        for block in content:
            btype = _block_attr(block, "type")
            if btype == "tool_result":
                out.append({
                    "role":         "tool",
                    "tool_call_id": _block_attr(block, "tool_use_id"),
                    "content":      _block_attr(block, "content", ""),
                })
            elif btype == "text":
                trailing_text.append(_block_attr(block, "text", ""))

        if trailing_text:
            out.append({"role": "user", "content": "\n".join(trailing_text)})

    return out


def _build_request(
    *,
    model,
    max_tokens,
    messages,
    max_tokens_field   = "max_tokens",
    system             = None,
    tools              = None,
    tool_choice        = None,
    temperature        = None,
    thinking           = None,
    output_config      = None,
    **unknown,
) -> dict:
    if thinking is not None:
        # Nothing goes on the wire for this one. Muse Glimmer reasons natively
        # and returns the trace in `reasoning_content`, which _to_message maps
        # back to a ThinkingBlock — so the request needs no flag, only the depth
        # that output_config already carries. There is no equivalent of
        # display="omitted", so a caller asking for thinking always gets it.
        if thinking.get("type") not in ("adaptive", "enabled"):
            raise UnsupportedFeature(
                f"Unsupported thinking type {thinking.get('type')!r}; this "
                "provider reasons natively and only understands adaptive/enabled."
            )
    if unknown:
        # Loud by design. A new Anthropic parameter added to a flow must be
        # either translated here or explicitly refused; it must never reach the
        # wire silently discarded.
        raise UnsupportedFeature(
            f"Unhandled Anthropic parameter(s): {sorted(unknown)}. Add a "
            "translation in _build_request or refuse them explicitly."
        )

    conversation = _convert_messages(messages)
    system_text  = _apply_effort(_system_text(system), output_config)
    if system_text:
        conversation = [{"role": "system", "content": system_text}, *conversation]

    request = {
        "model":          model,
        "messages":       conversation,
        max_tokens_field: max_tokens,
    }
    if temperature is not None:
        request["temperature"] = temperature

    converted_tools = _convert_tools(tools)
    if converted_tools:
        request["tools"] = converted_tools
        choice = _convert_tool_choice(tool_choice)
        if choice is not None:
            request["tool_choice"] = choice

    return request


# ── Response translation: OpenAI → Anthropic ──────────────────────

def _parse_arguments(raw: str) -> dict:
    """
    Tool arguments arrive as a JSON string. Malformed JSON is surfaced as a
    readable input rather than raised: the dispatcher then sends it to the MCP
    server, which rejects it as a bad argument and hands the model an error dict
    it can recover from. Raising here would take down the whole flow over one
    bad token.
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_malformed_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"_malformed_arguments": raw}


def _convert_usage(raw) -> Usage:
    """
    OpenAI usage → Anthropic usage.

    vLLM reports automatic-prefix-cache hits as
    prompt_tokens_details.cached_tokens, and includes them in prompt_tokens.
    Anthropic excludes cache reads from input_tokens, so the cached count is
    subtracted out — otherwise usage.py's hit rate would double-count them.

    cache_creation stays 0 permanently: there is no breakpoint to write and no
    write counter to read. That is why usage.py must not call these calls DEAD.
    """
    if raw is None:
        return Usage()

    details = getattr(raw, "prompt_tokens_details", None)
    cached  = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    prompt  = getattr(raw, "prompt_tokens", 0) or 0

    return Usage(
        input_tokens            = max(prompt - cached, 0),
        output_tokens           = getattr(raw, "completion_tokens", 0) or 0,
        cache_read_input_tokens = cached,
    )


def _reasoning_text(message) -> str:
    """
    Pull the reasoning trace off a response message.

    `reasoning_content` is the field LM Studio and vLLM both use. It is not part
    of the formal OpenAI schema, so the SDK exposes it via model_extra rather
    than as a declared attribute — check both.
    """
    direct = getattr(message, "reasoning_content", None)
    if direct:
        return direct
    extra = getattr(message, "model_extra", None) or {}
    return extra.get("reasoning_content") or ""


def _to_message(completion, model: str) -> Message:
    choice  = completion.choices[0]
    message = choice.message
    blocks: list = []

    # Thinking first, matching Anthropic's block order — run_flow prints the
    # trace before acting on the text or tool calls that follow it.
    reasoning = _reasoning_text(message)
    if reasoning:
        blocks.append(ThinkingBlock(thinking=reasoning))

    if message.content:
        blocks.append(TextBlock(text=message.content))

    for call in (message.tool_calls or []):
        blocks.append(ToolUseBlock(
            id    = call.id,
            name  = call.function.name,
            input = _parse_arguments(call.function.arguments),
        ))

    return Message(
        content     = blocks,
        stop_reason = _STOP_REASON.get(choice.finish_reason, choice.finish_reason),
        usage       = _convert_usage(getattr(completion, "usage", None)),
        model       = model,
    )


# ── Streaming ─────────────────────────────────────────────────────

class _MessageStream:
    """
    Mirrors anthropic's `async with client.messages.stream(...) as stream`.

    Two views over the same feed, and callers pick one:

        async for text in stream.text_stream:   # text deltas only
        async for event in stream:              # TextEvent / ThinkingEvent

    then `await get_final_message()` for the assembled Message.

    Both views are backed by ONE underlying generator, created lazily and
    memoised. That matches the real SDK, where `text_stream` is built once in
    __init__ and is therefore single-use — if this returned a fresh generator
    per access, two loops over it would each consume half the feed and neither
    would see the whole response.
    """

    def __init__(self, aclient, request: dict, model: str, requested_thinking: bool = False):
        self._aclient = aclient
        self._request = request
        self._model   = model
        self._requested_thinking = requested_thinking
        self._stream  = None
        self._text: list[str]      = []
        self._reasoning: list[str] = []
        self._calls: dict[int, dict] = {}
        self._finish: str | None  = None
        self._usage               = None
        self._drained             = False
        self._events_iter         = None
        self._text_stream         = None

    async def __aenter__(self):
        self._stream = await self._aclient.chat.completions.create(
            **self._request,
            stream         = True,
            stream_options = {"include_usage": True},
        )
        return self

    async def __aexit__(self, *exc_info):
        """
        Close only a stream that was abandoned part-way.

        A fully drained stream has already released its connection, and closing
        it a second time makes httpcore finalise an async generator that is
        already unwinding — which surfaces as

            RuntimeError: generator didn't stop after athrow()

        printed after a complete, correct answer. Harmless to the response,
        alarming in the logs, and it obscures real stream failures. Errors from
        closing an abandoned stream are swallowed for the same reason: by then
        the caller has what it needs and a teardown failure is not actionable.
        """
        if self._drained:
            return False
        close = getattr(self._stream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.debug("Ignoring error closing an abandoned stream", exc_info=True)
        return False

    async def _consume(self):
        """
        Yield TextEvent / ThinkingEvent as deltas arrive; accumulate tool calls
        and usage along the way.

        Reasoning is emitted as its own event type rather than folded into the
        text stream — it is process, not answer, and `text_stream` must keep
        meaning "the visible reply" on this provider exactly as it does on
        Anthropic.
        """
        async for chunk in self._stream:
            if getattr(chunk, "usage", None):
                self._usage = chunk.usage
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                self._finish = choice.finish_reason

            delta = choice.delta
            if delta is None:
                continue

            reasoning = _reasoning_text(delta)
            if reasoning:
                self._reasoning.append(reasoning)
                yield ThinkingEvent(reasoning, "".join(self._reasoning))

            if delta.content:
                self._text.append(delta.content)
                yield TextEvent(delta.content, "".join(self._text))

            for call in (delta.tool_calls or []):
                # Fragments are keyed by index, not id: the id and name usually
                # arrive only on the first fragment while `arguments` dribbles
                # in across many. Keying on id would lose everything after the
                # first chunk.
                slot = self._calls.setdefault(call.index, {"id": None, "name": None, "args": ""})
                if call.id:
                    slot["id"] = call.id
                if call.function is not None:
                    if call.function.name:
                        slot["name"] = call.function.name
                    if call.function.arguments:
                        slot["args"] += call.function.arguments

        self._drained = True

        # Close here, inside the generator frame, the moment the SSE feed ends.
        # Leaving it to __aexit__ or to garbage collection means httpcore
        # finalises its byte-stream generator during interpreter shutdown, which
        # prints
        #     RuntimeError: generator didn't stop after athrow()
        # after a complete and correct answer. Closing in-frame is deterministic
        # and keeps that noise out of every one-shot run.
        close = getattr(self._stream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.debug("Ignoring error closing a drained stream", exc_info=True)

    def _events(self):
        """The single memoised event generator both views read from."""
        if self._events_iter is None:
            self._events_iter = self._consume()
        return self._events_iter

    def __aiter__(self):
        return self._events()

    async def __stream_text__(self):
        async for event in self._events():
            if event.type == "text":
                yield event.text

    @property
    def text_stream(self):
        if self._text_stream is None:
            self._text_stream = self.__stream_text__()
        return self._text_stream

    async def get_final_message(self) -> Message:
        if not self._drained:
            # Caller skipped the stream (or bailed out early); drain the rest so
            # tool calls and usage are complete before assembling.
            async for _ in self._events():
                pass

        blocks: list = []
        reasoning = "".join(self._reasoning)
        if reasoning:
            blocks.append(ThinkingBlock(thinking=reasoning))

        text = "".join(self._text)
        if text:
            blocks.append(TextBlock(text=text))

        for index, slot in sorted(self._calls.items()):
            blocks.append(ToolUseBlock(
                id    = slot["id"] or f"call_{index}",
                name  = slot["name"] or "",
                input = _parse_arguments(slot["args"]),
            ))

        message = Message(
            content     = blocks,
            stop_reason = _STOP_REASON.get(self._finish, self._finish),
            usage       = _convert_usage(self._usage),
            model       = self._model,
        )
        _warn_if_trace_missing(self._requested_thinking, message)
        return message


# ── Client ────────────────────────────────────────────────────────

_warned_no_reasoning = False


def _warn_if_trace_missing(requested_thinking: bool, message: Message) -> None:
    """
    The one way this adapter can still no-op silently.

    reasoning_content is a de-facto convention, not part of the OpenAI schema.
    LM Studio and vLLM-with-a-reasoning-parser both send it; a plain llama.cpp
    server will not. When that happens the caller asked for an audit trail and
    got a perfectly valid answer with no trace and no error — exactly the
    failure mode this file exists to prevent. Say so, once.
    """
    global _warned_no_reasoning
    if not requested_thinking or _warned_no_reasoning:
        return
    if any(block.type == "thinking" for block in message.content):
        return
    _warned_no_reasoning = True
    logger.warning(
        "Thinking was requested but the server returned no reasoning_content. "
        "Reasoning traces are unavailable on this backend, so the audit trail "
        "will be empty; scores are unaffected. Start vLLM with a "
        "--reasoning-parser, or use LM Studio, to get traces back."
    )


class _Messages:
    def __init__(self, aclient, max_tokens_field: str):
        self._aclient          = aclient
        self._max_tokens_field = max_tokens_field

    async def create(self, **kwargs) -> Message:
        request = _build_request(max_tokens_field=self._max_tokens_field, **kwargs)
        completion = await self._aclient.chat.completions.create(**request)
        message = _to_message(completion, kwargs.get("model", ""))
        _warn_if_trace_missing(kwargs.get("thinking") is not None, message)
        return message

    def stream(self, **kwargs) -> _MessageStream:
        request = _build_request(max_tokens_field=self._max_tokens_field, **kwargs)
        return _MessageStream(
            self._aclient, request, kwargs.get("model", ""),
            requested_thinking = kwargs.get("thinking") is not None,
        )


class OpenAICompatClient:
    """The Anthropic Messages surface over an OpenAI-compatible endpoint."""

    def __init__(self, base_url: str | None, api_key: str, max_tokens_field: str = "max_tokens"):
        self._aclient = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.messages = _Messages(self._aclient, max_tokens_field)


# Muse Glimmer 30B — Meta's Apache-2.0 agentic model, distilled from Muse Spark.
# 131K context, native tool calling, reasoning-strength directive. Serve it with
# `vllm serve meta-models/Muse-Glimmer-30B --enable-auto-tool-choice`; the value
# here must match vLLM's --served-model-name.
DEFAULT_LOCAL_MODEL_ID = "meta-models/Muse-Glimmer-30B"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8000/v1"


def make_client(profile: str) -> tuple[OpenAICompatClient, str]:
    """Build the client and resolve the model id for a local/openai profile."""
    if profile == "local":
        return (
            OpenAICompatClient(
                base_url = os.environ.get("LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL),
                # vLLM ignores the key but the SDK refuses to construct without one.
                api_key  = os.environ.get("LOCAL_API_KEY", "EMPTY"),
            ),
            os.environ.get("LOCAL_MODEL_ID", DEFAULT_LOCAL_MODEL_ID),
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it, or use LLM_PROVIDER=local for "
            "a self-hosted model, or unset LLM_PROVIDER to use the Anthropic API."
        )
    return (
        OpenAICompatClient(
            base_url         = os.environ.get("OPENAI_BASE_URL"),
            api_key          = api_key,
            # The hosted API moved reasoning-capable models to this field;
            # vLLM and SGLang still take the classic max_tokens.
            max_tokens_field = "max_completion_tokens",
        ),
        os.environ.get("OPENAI_MODEL_ID", "gpt-5"),
    )
