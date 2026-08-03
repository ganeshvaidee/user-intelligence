# flows/run_flow.py
#
# Pure orchestration — skill loading, the agentic loop, and three flow patterns.
# All I/O (Bedrock API calls + MCP tool calls) is async.
# Each flow opens one MCP server process for its lifetime and closes it on exit.
#
#   run_flow()                — single shot
#   run_flow_until_complete() — convergence loop (LLM checks completeness)
#   run_flow_with_reflection()— critic-revise (LLM critiques, then revises)
#
# Run: python run_flow.py

import asyncio
import json
from pathlib import Path

from llm_client import client, MODEL_ID, TEMPERATURE
from tools import (
    USER_TOOLS,
    execute_tool,
    start_mcp_session,
    _check_completeness,
    _critique_response,
    tools_for_skills,
    ORDER_REQUIREMENTS,
)

SKILLS_DIR = Path(__file__).parent.parent / "skills"


# ── Skill loader ──────────────────────────────────────────────────

def load_skill(*skill_names: str) -> str:
    """Concatenate SKILL.md files into a single system-prompt block."""
    parts = []
    for name in skill_names:
        path = SKILLS_DIR / name / "SKILL.md"
        if not path.exists():
            raise FileNotFoundError(f"Skill not found: {path}")
        content = path.read_text()
        if content.startswith("---"):
            end = content.index("---", 3) + 3
            content = content[end:].strip()
        parts.append(f"# SKILL: {name}\n\n{content}")
    return "\n\n---\n\n".join(parts)


# ── Private helpers ───────────────────────────────────────────────

def _build_system_prompt(skills_content: str) -> str:
    return (
        "You are a user intelligence assistant for an internal IT security team.\n"
        "You have access to user intelligence tools for all data operations.\n\n"
        "Follow the skills below precisely — they define your behavior for this task.\n\n"
        f"{skills_content}\n"
    )


def _cache_tools(tools: list[dict] | None) -> list[dict]:
    """
    Put a single cache breakpoint on the LAST tool.

    The API allows at most 4 cache_control blocks per request and caches the
    entire prefix up to the last breakpoint — so one marker on the final tool
    already caches every tool before it. Marking every tool spends a breakpoint
    per tool and returns
        400 "A maximum of 4 blocks with cache_control may be provided"
    as soon as a flow exposes more than three tools (the system prompt uses the
    fourth). Caching behaviour is identical; only the breakpoint count differs.
    """
    if not tools:
        return []
    return [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]


def _print_header(user_request: str, skill_names: list[str]) -> None:
    print(f"\n{'='*60}")
    print(f"REQUEST: {user_request}")
    print(f"SKILLS:  {', '.join(skill_names)}")
    print(f"{'='*60}\n")


# Circuit breaker for the agentic loops. The only real exit condition is
# Claude returning stop_reason == "end_turn"; if it keeps emitting tool calls
# the loop has nothing else to stop it. A typical risk assessment uses 3–5
# iterations, so 20 is generous — hitting it means something is wrong, not that
# the work was large.
MAX_TOOL_ITERATIONS = 20


def _iteration_guard(iteration: int, where: str) -> bool:
    """Return True when the loop has exceeded MAX_TOOL_ITERATIONS."""
    if iteration <= MAX_TOOL_ITERATIONS:
        return False
    print(f"[WARNING] {where}: hit MAX_TOOL_ITERATIONS ({MAX_TOOL_ITERATIONS}) "
          f"— forcing stop. The response may be incomplete.")
    return True


def _is_error_result(result: str) -> bool:
    """
    Did an MCP tool report failure?

    Tools return a JSON object and signal failure with an "error" key. Anything
    that isn't a parseable JSON object — a bare string, a list, plain text from
    a misbehaving tool — is treated as an error rather than allowed to raise:
    a json.JSONDecodeError here would kill the whole flow mid-loop, and marking
    an unrecognisable result as "succeeded" would let it satisfy the order guard.
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return True
    return not isinstance(parsed, dict) or "error" in parsed


async def _dispatch_tool_use(
    session,
    block,
    seen_calls: dict,
    completed: set[str],
    verbose: bool = False,
) -> dict:
    """
    Execute one tool_use block and return the tool_result dict to append.

    The single implementation of duplicate detection, the ORDER_REQUIREMENTS
    order guard, and MCP dispatch. Shared by _run_tool_loop and all four
    streaming flows.

    This logic used to be copied five times, and the copies had drifted: only
    the blocking one warned on a duplicate call, so the streaming flows —
    which are what /flow/stream actually runs — recorded seen_calls and never
    read it. Adding a guardrail here now covers every flow at once.
    """
    cache_key = f"{block.name}:{json.dumps(block.input, sort_keys=True)}"
    if cache_key in seen_calls:
        print(f"[DUPLICATE TOOL CALL] {block.name}({json.dumps(block.input)}) already called — redundant MCP call")
    else:
        seen_calls[cache_key] = True

    if verbose:
        print(f"\n[TOOL CALL] {block.name}({json.dumps(block.input, indent=2)})")

    missing = [req for req in ORDER_REQUIREMENTS.get(block.name, []) if req not in completed]
    if missing:
        result = json.dumps({
            "error": f"Cannot call {block.name} before {', '.join(missing)} has succeeded in this conversation."
        })
    else:
        result = await execute_tool(session, block.name, block.input)
        if not _is_error_result(result):
            completed.add(block.name)

    if verbose:
        display = result[:300] + "..." if len(result) > 300 else result
        print(f"[TOOL RESULT] {display}\n")

    return {"type": "tool_result", "tool_use_id": block.id, "content": result}


async def _run_tool_loop(
    system_prompt: str,
    messages: list[dict],
    session,
    verbose: bool = False,
    seen_calls: dict | None = None,
    tools: list[dict] | None = None,
    completed: set[str] | None = None,
) -> tuple[list[dict], str]:
    """
    Run the agentic tool-use loop until stop_reason == 'end_turn'.
    Tool calls are routed to the MCP server via session.
    Returns (updated_messages, accumulated_text) so the caller can
    continue the same conversation across multiple rounds.
    """
    msgs             = list(messages)
    accumulated_text = ""
    if seen_calls is None:
        seen_calls = {}
    if completed is None:
        completed = set()
    active_tools = tools if tools is not None else USER_TOOLS

    cached_tools = _cache_tools(active_tools)
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    iteration = 0
    while True:
        iteration += 1
        if _iteration_guard(iteration, "_run_tool_loop"):
            break
        if verbose:
            async with client.messages.stream(
                model       = MODEL_ID,
                max_tokens  = 4096,
                temperature = TEMPERATURE,
                system      = cached_system,
                tools       = cached_tools,
                messages    = msgs,
            ) as stream:
                async for text in stream.text_stream:
                    print(text, end="", flush=True)
                response = await stream.get_final_message()
            if any(b.type == "text" for b in response.content):
                print()
        else:
            response = await client.messages.create(
                model       = MODEL_ID,
                max_tokens  = 4096,
                temperature = TEMPERATURE,
                system      = cached_system,
                tools       = cached_tools,
                messages    = msgs,
            )

        tool_results = []
        for block in response.content:
            if block.type == "text":
                accumulated_text += block.text

            elif block.type == "tool_use":
                tool_results.append(
                    await _dispatch_tool_use(session, block, seen_calls, completed, verbose)
                )

        msgs.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if tool_results:
            msgs.append({"role": "user", "content": tool_results})

    return msgs, accumulated_text


# ── Public flow functions ─────────────────────────────────────────

async def run_flow(
    user_request: str,
    skill_names: list[str],
    verbose: bool = True,
    completed: set[str] | None = None,
) -> str:
    """
    Single-shot: Claude calls tools until it decides it's done.

    `completed` pre-seeds the order guard with tools already known to have
    succeeded. Normally left as None (nothing has run yet). Pass a seed only
    when a prerequisite was satisfied outside this conversation and you have
    verified it — see run_flow_offboard_confirm().
    """
    system_prompt = _build_system_prompt(load_skill(*skill_names))
    tools = tools_for_skills(skill_names)

    if verbose:
        _print_header(user_request, skill_names)

    async with start_mcp_session() as session:
        _, response_text = await _run_tool_loop(
            system_prompt,
            [{"role": "user", "content": user_request}],
            session,
            verbose,
            tools=tools,
            completed=completed,
        )
    return response_text


async def run_flow_stream(user_request: str, skill_names: list[str]):
    """
    Single-shot flow that yields text chunks as Claude generates them.
    Used by the orchestrator's /flow/stream endpoint.
    Tool calls are executed silently — only the final text is streamed.

    The MCP session is opened fresh per tool-use round (not held open across
    yields) to avoid leaking the anyio TaskGroup inside streamablehttp_client
    into the event loop state between requests.
    """
    system_prompt  = _build_system_prompt(load_skill(*skill_names))
    messages       = [{"role": "user", "content": user_request}]
    tools = tools_for_skills(skill_names)
    cached_tools   = _cache_tools(tools)
    cached_system  = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    seen_calls: dict = {}
    completed: set[str] = set()

    iteration = 0
    while True:
        iteration += 1
        if _iteration_guard(iteration, "run_flow_stream"):
            break
        tool_results: list[dict] = []
        stop = False

        # Fresh session per Bedrock call. Yield inside the session — safe because
        # each iteration opens and closes its own session. The anyio TaskGroup
        # is fully cleaned up before the next iteration's yield.
        async with start_mcp_session() as session:
            async with client.messages.stream(
                model       = MODEL_ID,
                max_tokens  = 4096,
                temperature = TEMPERATURE,
                system      = cached_system,
                tools       = cached_tools,
                messages    = messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text              # stream immediately
                response = await stream.get_final_message()

            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append(
                        await _dispatch_tool_use(session, block, seen_calls, completed)
                    )

            messages.append({"role": "assistant", "content": response.content})
            stop = response.stop_reason == "end_turn"
        # Session closes here — tool calls done, no yield pending

        if stop:
            break

        if tool_results:
            messages.append({"role": "user", "content": tool_results})


async def run_flow_until_complete_stream(
    user_request: str,
    skill_names:  list[str],
    max_rounds:   int = 3,
):
    """
    Convergence loop that yields text chunks as Claude generates them.
    Judge calls run silently between rounds — only Claude's text is streamed.
    """
    system_prompt = _build_system_prompt(load_skill(*skill_names))
    messages      = [{"role": "user", "content": user_request}]
    tools = tools_for_skills(skill_names)
    cached_tools  = _cache_tools(tools)
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    seen_calls: dict = {}
    completed: set[str] = set()
    all_text = ""          # accumulates across rounds — what the judge grades

    for round_num in range(1, max_rounds + 1):
        round_text = ""
        stop = False

        # Fresh session per round — safe to yield inside while session is open.
        # The session closes after the inner tool-use loop, before the judge call.
        async with start_mcp_session() as session:
            round_messages = list(messages)
            iteration = 0
            while True:
                iteration += 1
                if _iteration_guard(iteration, "run_flow_until_complete_stream"):
                    break
                tool_results: list[dict] = []
                async with client.messages.stream(
                    model       = MODEL_ID,
                    max_tokens  = 4096,
                    temperature = TEMPERATURE,
                    system      = cached_system,
                    tools       = cached_tools,
                    messages    = round_messages,
                ) as stream:
                    async for text in stream.text_stream:
                        yield text           # stream immediately — session stays open
                        round_text += text
                    response = await stream.get_final_message()

                for block in response.content:
                    if block.type == "tool_use":
                        tool_results.append(
                            await _dispatch_tool_use(session, block, seen_calls, completed)
                        )

                round_messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "end_turn":
                    stop = True
                    break
                if tool_results:
                    round_messages.append({"role": "user", "content": tool_results})

            messages = round_messages
        # Session now closed — judge call runs with no session open

        all_text += round_text

        if stop or round_num == max_rounds:
            break

        # ── Completeness judge (silent — no streaming) ──
        # Judge the accumulated transcript, not just this round. Round 2 only
        # fills the gaps round 1 left, so judging round_text alone always looks
        # incomplete and the loop can never converge before max_rounds.
        check   = await _check_completeness(user_request, all_text)
        missing = check.get("missing") or []

        if check.get("judge_unavailable"):
            # Out-of-band event, not response text. app.py forwards dicts as-is.
            yield {"warning": (
                f"Completeness judge returned no verdict on round {round_num} — "
                f"this response was NOT checked for completeness."
            )}

        if check.get("complete") or not missing:
            break

        missing_text = "\n".join(f"- {m}" for m in missing)
        messages.append({
            "role":    "user",
            "content": f"Your response is incomplete. Please also check:\n{missing_text}",
        })


async def run_flow_with_reflection_stream(
    user_request: str,
    skill_names:  list[str],
):
    """
    Critic-revise that yields text chunks as Claude generates them.
    The critic call runs silently — only Claude's text is streamed.
    """
    system_prompt = _build_system_prompt(load_skill(*skill_names))
    tools = tools_for_skills(skill_names)
    cached_tools  = _cache_tools(tools)
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    seen_calls: dict = {}
    completed: set[str] = set()

    # ── Phase 1 — initial response ────────────────────────────────
    messages     = [{"role": "user", "content": user_request}]
    initial_text = ""

    async with start_mcp_session() as session:
        iteration = 0
        while True:
            iteration += 1
            if _iteration_guard(iteration, "run_flow_with_reflection_stream (phase 1)"):
                break
            tool_results: list[dict] = []
            async with client.messages.stream(
                model=MODEL_ID, max_tokens=4096, temperature=TEMPERATURE,
                system=cached_system, tools=cached_tools, messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text              # stream immediately
                    initial_text += text
                response = await stream.get_final_message()

            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append(
                        await _dispatch_tool_use(session, block, seen_calls, completed)
                    )

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "end_turn":
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
    # Session closed — critic call runs with no session open

    # ── Phase 2 — critic (silent) ─────────────────────────────────
    critique = await _critique_response(user_request, initial_text)
    issues   = critique.get("issues") or []

    if critique.get("judge_unavailable"):
        # Out-of-band event, not response text. app.py forwards dicts as-is.
        yield {"warning": (
            "Critic returned no verdict — this response was NOT reviewed for "
            "errors or unjustified claims."
        )}

    if not critique.get("has_issues") or not issues:
        return

    # ── Phase 3 — revision ────────────────────────────────────────
    issues_text = "\n".join(f"- {issue}" for issue in issues)
    messages.append({
        "role":    "user",
        "content": f"Your assessment has the following issues:\n{issues_text}\n\nPlease revise your assessment to address these points.",
    })

    async with start_mcp_session() as session:
        iteration = 0          # phase 3 gets its own budget, not phase 1's leftovers
        while True:
            iteration += 1
            if _iteration_guard(iteration, "run_flow_with_reflection_stream (phase 3)"):
                break
            tool_results = []
            async with client.messages.stream(
                model=MODEL_ID, max_tokens=4096, temperature=TEMPERATURE,
                system=cached_system, tools=cached_tools, messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text              # stream immediately
                response = await stream.get_final_message()

            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append(
                        await _dispatch_tool_use(session, block, seen_calls, completed)
                    )

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "end_turn":
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})


async def run_flow_until_complete(
    user_request: str,
    skill_names: list[str],
    max_rounds: int = 3,
    verbose: bool = True,
) -> tuple[str, list[str]]:
    """
    Convergence loop: after each round a second LLM call checks whether the
    response is complete. If not, the missing items are fed back as a follow-up
    in the same conversation and Claude runs another tool-use pass.
    A fresh MCP session is opened per round so the connection does not time
    out during the completeness check between rounds.

    Returns (response_text, warnings). `warnings` is empty on a normal run and
    carries one entry per round where the judge returned no readable verdict —
    the caller needs this to know the response went out unverified.
    """
    system_prompt = _build_system_prompt(load_skill(*skill_names))
    tools = tools_for_skills(skill_names)

    if verbose:
        _print_header(user_request, skill_names)

    messages   = [{"role": "user", "content": user_request}]
    all_text   = ""
    seen_calls = {}
    completed: set[str] = set()
    warnings: list[str] = []

    for round_num in range(1, max_rounds + 1):
        if verbose and round_num > 1:
            print(f"\n[CONVERGENCE] ── Round {round_num} ─────────────────────")

        async with start_mcp_session() as session:
            messages, round_text = await _run_tool_loop(system_prompt, messages, session, verbose, seen_calls, tools=tools, completed=completed)
        all_text += round_text

        if round_num == max_rounds:
            if verbose:
                print(f"\n[CONVERGENCE] Max rounds ({max_rounds}) reached.")
            break

        check   = await _check_completeness(user_request, all_text)
        missing = check.get("missing") or []

        if check.get("judge_unavailable"):
            warnings.append(
                f"Completeness judge returned no verdict on round {round_num} — "
                f"this response was NOT checked for completeness."
            )

        if verbose:
            if check.get("judge_unavailable"):
                # Not a pass — the judge call returned no readable verdict.
                print(f"\n[CONVERGENCE] Round {round_num} ⚠ judge unavailable — "
                      f"stopping with the response UNVERIFIED")
            elif check.get("complete"):
                print(f"\n[CONVERGENCE] Round {round_num} ✓ complete")
            elif not missing:
                print(f"\n[CONVERGENCE] Round {round_num} incomplete but judge named "
                      f"nothing missing — stopping")
            else:
                print(f"\n[CONVERGENCE] Round {round_num} incomplete — missing: {missing}")

        # `or not missing` matches run_flow_until_complete_stream. Without it, a
        # verdict of {"complete": false, "missing": []} sends Claude
        # "Please also check:" with nothing after the colon — a contentless
        # instruction that burns a full round, and repeats until max_rounds.
        if check.get("complete") or not missing:
            break

        missing_text = "\n".join(f"- {m}" for m in missing)
        messages.append({
            "role":    "user",
            "content": f"Your response is incomplete. Please also check:\n{missing_text}",
        })

    return all_text, warnings


async def run_flow_with_reflection(
    user_request: str,
    skill_names: list[str],
    verbose: bool = True,
) -> tuple[str, list[str]]:
    """
    Critic-revise: runs the flow once, then a second LLM call critiques the
    output. If issues are found, Claude revises within the same conversation
    thread — it has full context of every tool call it already made.
    One MCP server process spans both the initial pass and the revision.

    Returns (response_text, warnings). `warnings` carries an entry when the
    critic returned no readable verdict, so the caller knows the response was
    never actually reviewed.
    """
    system_prompt = _build_system_prompt(load_skill(*skill_names))
    tools = tools_for_skills(skill_names)

    if verbose:
        _print_header(user_request, skill_names)
        print("[REFLECTION] Phase 1: Initial assessment...")

    seen_calls = {}
    completed: set[str] = set()
    warnings: list[str] = []

    async with start_mcp_session() as session:
        # Phase 1 — initial response
        messages, initial_text = await _run_tool_loop(
            system_prompt,
            [{"role": "user", "content": user_request}],
            session,
            verbose,
            seen_calls,
            tools=tools,
            completed=completed,
        )

        # Phase 2 — critique (pure LLM call, no MCP needed)
        if verbose:
            print("\n[REFLECTION] Phase 2: Critiquing assessment...")

        critique = await _critique_response(user_request, initial_text)
        issues   = critique.get("issues") or []

        if critique.get("judge_unavailable"):
            warnings.append(
                "Critic returned no verdict — this response was NOT reviewed for "
                "errors or unjustified claims."
            )

        if not critique.get("has_issues") or not issues:
            if verbose:
                if critique.get("judge_unavailable"):
                    # Not a clean review — the critic call returned no readable verdict.
                    print("[REFLECTION] ⚠ Critic unavailable — returning initial "
                          "response UNREVIEWED.")
                else:
                    print("[REFLECTION] No issues found — returning initial response.")
            return initial_text, warnings

        if verbose:
            print("[REFLECTION] Issues found:")
            for issue in issues:
                print(f"  - {issue}")
            print("\n[REFLECTION] Phase 3: Revising in same conversation...")

        # Phase 3 — revision in the same conversation + same MCP session
        issues_text = "\n".join(f"- {issue}" for issue in issues)
        messages.append({
            "role":    "user",
            "content": (
                f"Your assessment has the following issues:\n{issues_text}\n\n"
                "Please revise your assessment to address these points."
            ),
        })

        _, revised_text = await _run_tool_loop(system_prompt, messages, session, verbose, seen_calls, tools=tools, completed=completed)

    return revised_text, warnings


# ── Parallel multi-agent risk scoring ────────────────────────────

# Structured output tool each dimension agent calls as its final action
_DIMENSION_SCORE_TOOL = {
    "name": "report_dimension_score",
    "description": (
        "Call this when you have finished fetching data and scoring your dimension. "
        "Report your score, the conditions that triggered points, and the raw evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score":     {"type": "integer", "description": "Points scored (0 to max)"},
            "max_score": {"type": "integer", "description": "Maximum possible for this dimension"},
            "factors":   {"type": "array",   "items": {"type": "string"}, "description": "Conditions that added points, e.g. 'MFA disabled (+2)'"},
            "evidence":  {"type": "array",   "items": {"type": "string"}, "description": "Specific data points from the tools"},
            "reasoning": {"type": "string",  "description": "One-sentence summary of the key factor that drove this score, e.g. 'No MFA on a contractor account with admin-level DB access'"},
        },
        "required": ["score", "max_score", "factors", "evidence", "reasoning"],
    },
}

# Scoped tool sets — each agent only sees what its dimension needs
_DIMENSION_TOOLS = {
    "auth":        [t for t in USER_TOOLS if t["name"] in {"get_user", "get_user_activity"}],
    "permissions": [t for t in USER_TOOLS if t["name"] in {"get_user", "get_user_permissions"}],
    "behaviour":   [t for t in USER_TOOLS if t["name"] in {"get_user_activity"}],
    "account":     [t for t in USER_TOOLS if t["name"] in {"get_user", "get_audit_log"}],
}

_RISK_LEVELS = [
    (10, "🔴 Critical", "Immediate deactivation recommended"),
    (6,  "🟠 High",     "Flag account and notify manager"),
    (3,  "🟡 Medium",   "Monitor and review permissions"),
    (0,  "🟢 Low",      "No action needed"),
]


async def run_dimension_agent(dimension: str, user_id: str, verbose: bool = False, thinking: bool = True) -> dict:
    """
    Run one dimension scoring agent. Opens its own MCP session, fetches data,
    scores its dimension, and returns a structured result via report_dimension_score.
    When thinking=True, adaptive thinking is enabled — Claude reasons step-by-step
    before scoring, and returns a summary of that reasoning, making the logic auditable.
    """
    system_prompt = _build_system_prompt(load_skill(f"risk-{dimension}"))
    messages      = [{"role": "user", "content": f"Score the {dimension} risk dimension for user {user_id}."}]
    base_tools    = _DIMENSION_TOOLS[dimension]
    all_tools     = base_tools + [_DIMENSION_SCORE_TOOL]
    cached_tools  = _cache_tools(all_tools)
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    score_result  = None
    tools_called: list[str] = []

    async with start_mcp_session() as session:
        iteration = 0
        while True:
            iteration += 1
            if _iteration_guard(iteration, "run_dimension_agent"):
                break
            create_kwargs = dict(
                model      = MODEL_ID,
                max_tokens = 10000 if thinking else 2048,
                system     = cached_system,
                tools      = cached_tools,
                messages   = messages,
            )
            if thinking:
                # Adaptive thinking — Claude picks the depth per request instead of
                # spending a fixed budget. display="summarized" is required: the
                # default is "omitted", which returns thinking blocks with empty text
                # and would silently break the [THINKING — ...] audit output below.
                # temperature is deliberately left unset here — the API requires
                # temperature=1 when extended thinking is enabled and rejects any
                # other value.
                create_kwargs["thinking"]      = {"type": "adaptive", "display": "summarized"}
                create_kwargs["output_config"] = {"effort": "high"}
            else:
                create_kwargs["temperature"] = TEMPERATURE

            response = await client.messages.create(**create_kwargs)

            tool_results = []
            for block in response.content:
                if block.type == "thinking":
                    if verbose:
                        print(f"\n[THINKING — {dimension.upper()}]\n{block.thinking}\n")

                elif block.type == "tool_use":
                    if block.name == "report_dimension_score":
                        score_result = block.input
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     '{"status": "recorded"}',
                        })
                    else:
                        tools_called.append(block.name)
                        result = await execute_tool(session, block.name, block.input)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })

            messages.append({"role": "assistant", "content": response.content})

            if score_result or response.stop_reason == "end_turn":
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

    return score_result or {"score": 0, "max_score": 0, "factors": [], "evidence": []}, tools_called


def _synthesize_risk_report(user_id: str, auth: dict, perms: dict, behav: dict, acct: dict, prior: dict | None = None, thinking: bool = True) -> str:
    dims = [
        ("Authentication", auth,  auth.get("max_score",  6)),
        ("Permissions",    perms, perms.get("max_score", 5)),
        ("Behaviour",      behav, behav.get("max_score", 4)),
        ("Account",        acct,  acct.get("max_score",  3)),
    ]

    total     = sum(r.get("score", 0) for _, r, _ in dims)
    max_total = sum(max_s for _, _, max_s in dims)
    level_label, recommendation = next(
        (label, rec) for threshold, label, rec in _RISK_LEVELS if total >= threshold
    )

    all_evidence = (
        auth.get("evidence", []) +
        perms.get("evidence", []) +
        behav.get("evidence", []) +
        acct.get("evidence", [])
    )

    lines = [
        f"## Risk Assessment — {user_id} "
        f"(Parallel Agents{' + Extended Thinking' if thinking else ''})",
        f"*Max possible score: {max_total} points across 4 dimensions*",
        "",
        f"**Risk Score:** {total}/{max_total}   **Level:** {level_label}",
        "",
        "### Score Breakdown",
        "| Dimension      | Score | Key Factors |",
        "|----------------|-------|-------------|",
    ]
    for dim_name, result, max_score in dims:
        score   = result.get("score", 0)
        factors = ", ".join(result.get("factors", []) or ["None"])
        lines.append(f"| {dim_name:<14} | {score}/{max_score}   | {factors} |")

    reasoning_notes = [
        f"- **{dim_name}:** {result['reasoning']}"
        for dim_name, result, _ in dims
        if result.get("reasoning")
    ]

    lines += ["", "### Recommended Action", recommendation]

    if reasoning_notes:
        lines += ["", "### Agent Reasoning"]
        lines += reasoning_notes

    lines += ["", "### Evidence"]
    for item in all_evidence[:6]:
        lines.append(f"- {item}")

    if prior and not prior.get("none"):
        delta     = total - prior["total_score"]
        direction = f"+{delta}" if delta > 0 else str(delta)
        dim_deltas = [
            ("Authentication", auth.get("score", 0)  - prior["auth_score"]),
            ("Permissions",    perms.get("score", 0) - prior["perms_score"]),
            ("Behaviour",      behav.get("score", 0) - prior["behav_score"]),
            ("Account",        acct.get("score", 0)  - prior["acct_score"]),
        ]
        lines += [
            "",
            "### Change Since Prior Assessment",
            f"Prior: {prior['total_score']}/{prior['max_score']} "
            f"({prior['risk_level']}) on {prior['assessed_at'][:10]}",
            f"Current: {total}/{max_total} — overall change: **{direction}**",
        ]
        changed = [(n, d) for n, d in dim_deltas if d != 0]
        if changed:
            for dim_name, d in changed:
                arrow = "↑" if d > 0 else "↓"
                lines.append(f"- {dim_name}: {arrow} {abs(d)} point{'s' if abs(d) != 1 else ''}")
        else:
            lines.append("- No change in any dimension")
    elif prior and prior.get("none"):
        lines += ["", "### Prior Assessment",
                  "None — this is the baseline assessment. Scores will be compared on the next run."]

    return "\n".join(lines)


async def run_flow_parallel_risk_with_memory(user_id: str, verbose: bool = True) -> str:
    """
    Option 8: parallel agents + extended thinking + memory.
    Fetches the prior assessment before launching agents, synthesizes a delta
    comparison, then saves the new result — all via MCP, all Python-driven.
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"PARALLEL RISK ASSESSMENT WITH MEMORY: {user_id}")
        print(f"{'='*60}")

    # Step 1 — fetch prior (Python-driven, before agents launch)
    async with start_mcp_session() as session:
        prior_raw = await execute_tool(session, "get_prior_assessment", {"user_id": user_id})
    prior = json.loads(prior_raw)

    if verbose:
        if prior.get("none"):
            print("[MEMORY] No prior assessment — this will be the baseline.")
        else:
            print(f"[MEMORY] Prior: {prior['total_score']}/{prior['max_score']} "
                  f"({prior['risk_level']}) on {prior['assessed_at'][:10]}")
        print("[AGENTS] Launching 4 dimension agents concurrently...\n")

    # Step 2 — parallel agents with extended thinking
    (auth, _), (perms, _), (behav, _), (acct, _) = await asyncio.gather(
        run_dimension_agent("auth",        user_id, verbose, thinking=True),
        run_dimension_agent("permissions", user_id, verbose, thinking=True),
        run_dimension_agent("behaviour",   user_id, verbose, thinking=True),
        run_dimension_agent("account",     user_id, verbose, thinking=True),
    )

    if verbose:
        print("\n[AGENTS] All 4 agents complete. Synthesizing...\n")

    # Step 3 — synthesize with comparison section
    report = _synthesize_risk_report(user_id, auth, perms, behav, acct, prior=prior, thinking=True)

    # Step 4 — save this assessment
    dims      = [(auth, 6), (perms, 5), (behav, 4), (acct, 3)]
    total     = sum(r.get("score", 0) for r, _ in dims)
    max_total = sum(r.get("max_score", m) for r, m in dims)
    level     = next(label for threshold, label, _ in _RISK_LEVELS if total >= threshold)
    summary   = next(
        (r.get("reasoning") for r in [auth, perms, behav, acct] if r.get("reasoning")),
        "No summary available",
    )

    async with start_mcp_session() as session:
        await execute_tool(session, "save_assessment", {
            "user_id":     user_id,
            "total_score": total,
            "max_score":   max_total,
            "risk_level":  level,
            "auth_score":  auth.get("score", 0),
            "perms_score": perms.get("score", 0),
            "behav_score": behav.get("score", 0),
            "acct_score":  acct.get("score", 0),
            "summary":     summary,
        })

    if verbose:
        print("[MEMORY] Assessment saved.\n")
        print(report)

    return report


async def run_flow_parallel_risk(
    user_id: str, verbose: bool = True, thinking: bool = False
) -> tuple[str, list[str]]:
    """
    Fan out to 4 independent Claude agents — one per risk dimension.
    thinking=False (default for option 7) — faster, no extended thinking.
    thinking=True  (option 8) — slower, but reasoning is auditable.

    Returns (report, tools_called) — the flat list of tool names across all
    four agents. Note run_flow_parallel_risk_with_memory() returns the report
    alone; callers of the two are not interchangeable.
    """
    if verbose:
        label = "PARALLEL RISK ASSESSMENT" + (" + EXTENDED THINKING" if thinking else "")
        print(f"\n{'='*60}\n{label}: {user_id}\n{'='*60}")
        print("[AGENTS] Launching 4 dimension agents concurrently...")
        print("         auth | permissions | behaviour | account\n")

    (auth, auth_tools), (perms, perms_tools), (behav, behav_tools), (acct, acct_tools) = \
        await asyncio.gather(
            run_dimension_agent("auth",        user_id, verbose, thinking),
            run_dimension_agent("permissions", user_id, verbose, thinking),
            run_dimension_agent("behaviour",   user_id, verbose, thinking),
            run_dimension_agent("account",     user_id, verbose, thinking),
        )

    all_tools = auth_tools + perms_tools + behav_tools + acct_tools

    if verbose:
        print("\n[AGENTS] All 4 agents complete. Synthesizing...\n")

    report = _synthesize_risk_report(user_id, auth, perms, behav, acct, thinking=thinking)

    if verbose:
        print(report)

    return report, all_tools


# ── Example flows ─────────────────────────────────────────────────

async def example_lookup():
    return await run_flow(
        user_request = "Look up user usr_005",
        skill_names  = ["_base", "lookup-user"],
    )


async def example_risk_assessment():
    return await run_flow(
        user_request = "Give me a risk assessment for usr_005",
        skill_names  = ["_base", "lookup-user", "user-risk-profile"],
    )


async def run_flow_offboard_prepare(user_id: str, reason: str, verbose: bool = True) -> str:
    """
    Phase 1 of HITL offboarding: lookup → risk → flag.
    Returns a report for human review. Does NOT deactivate.
    The client owns the confirmation gate.
    """
    return await run_flow(
        user_request = (
            f"Prepare offboarding for user {user_id}. Reason: {reason}. "
            f"Run lookup, risk assessment, and pre-deactivation flag. "
            f"Stop after flagging — do NOT ask for confirmation or deactivate."
        ),
        skill_names  = ["_base", "lookup-user", "user-risk-profile", "offboard-prepare"],
        verbose      = verbose,
    )


async def run_flow_offboard_confirm(user_id: str, reason: str, verbose: bool = True) -> str:
    """
    Phase 2 of HITL offboarding: deactivate.
    Called only after the human has confirmed in the client.

    ORDER_REQUIREMENTS says deactivate_user needs flag_user, but the guard
    tracks tools completed *in the current conversation* — and this is a new
    one. Phase 1's flag is recorded durably (flag_user sets status='flagged'),
    so verify it in the DB and seed the guard from that.

    This is stricter than the in-conversation check, not a way around it: that
    check only proves a flag_user call was made, while this proves the flag
    actually persisted. An unflagged user is refused before any model call.

    The get_user lookup is Python-driven — execute_tool() reaches the MCP
    server directly, so it does not make get_user visible to Claude and
    SKILL_TOOLS stays untouched. Same pattern as get_prior_assessment.
    """
    async with start_mcp_session() as session:
        user = json.loads(await execute_tool(session, "get_user", {"user_id": user_id}))

    if "error" in user:
        return f"Cannot deactivate {user_id}: {user['error']}"
    if user.get("status") == "inactive":
        return f"User {user_id} is already inactive — nothing to do."
    if user.get("status") != "flagged":
        return (
            f"Refusing to deactivate {user_id}: account status is "
            f"'{user.get('status')}', not 'flagged'. Run the prepare phase first "
            f"so the flag and its audit trail exist before deactivation."
        )

    if verbose:
        print(f"[OFFBOARD] {user_id} confirmed flagged in DB — order guard satisfied.")

    return await run_flow(
        user_request = (
            f"Deactivate user {user_id}. Reason: {reason}. "
            f"The human has confirmed this action. Proceed with deactivation."
        ),
        skill_names  = ["_base", "offboard-confirm"],
        verbose      = verbose,
        completed    = {"flag_user"},   # proven by the DB status check above
    )


async def example_offboard():
    user_id = input("User ID: ").strip() or "usr_005"
    reason  = input("Reason: ").strip() or "contractor contract ended"

    print("\n[PHASE 1] Running assessment and flagging...")
    await run_flow_offboard_prepare(user_id, reason)

    response = input("\nType CONFIRM to deactivate, anything else to cancel: ").strip()
    if response.upper() != "CONFIRM":
        print("\n❌ Offboarding cancelled. Account remains flagged for review.")
        return

    print("\n[PHASE 2] Deactivating...")
    await run_flow_offboard_confirm(user_id, reason)


async def example_find_by_email():
    return await run_flow(
        user_request = "Check the risk profile for eve@vendor.com",
        skill_names  = ["_base", "lookup-user", "user-risk-profile"],
    )


async def example_risk_with_convergence():
    text, _warnings = await run_flow_until_complete(
        user_request = "Give me a thorough risk assessment for usr_005",
        skill_names  = ["_base", "lookup-user", "user-risk-profile"],
        max_rounds   = 3,
    )
    return text


async def example_risk_with_reflection():
    text, _warnings = await run_flow_with_reflection(
        user_request = "Give me a risk assessment for usr_005",
        skill_names  = ["_base", "lookup-user", "user-risk-profile"],
    )
    return text


async def example_parallel_risk():
    user_id = input("User ID: ").strip() or "usr_005"
    report, _ = await run_flow_parallel_risk(user_id, thinking=False)
    return report


async def example_parallel_risk_with_thinking():
    user_id = input("User ID: ").strip() or "usr_005"
    report, _ = await run_flow_parallel_risk(user_id, thinking=True)
    return report


async def example_parallel_risk_with_memory():
    user_id = input("User ID: ").strip() or "usr_005"
    return await run_flow_parallel_risk_with_memory(user_id)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    flows = {
        "1": ("Lookup user",                         example_lookup),
        "2": ("Risk assessment",                     example_risk_assessment),
        "3": ("Full offboarding",                    example_offboard),
        "4": ("Find by email + risk",                example_find_by_email),
        "5": ("Risk assessment (convergence loop)",  example_risk_with_convergence),
        "6": ("Risk assessment (critic-revise)",     example_risk_with_reflection),
        "7": ("Risk assessment (parallel agents)",                              example_parallel_risk),
        "8": ("Risk assessment (parallel agents + extended thinking)",          example_parallel_risk_with_thinking),
        "9": ("Risk assessment (parallel + extended thinking + memory)",        example_parallel_risk_with_memory),
    }

    print("User Intelligence — Flow Runner")
    print("================================")
    for k, (name, _) in flows.items():
        print(f"  {k}. {name}")

    choice = input("\nChoose a flow (1-9): ").strip()

    if choice in flows:
        _, fn = flows[choice]
        await fn()
    else:
        print("Running flow 1 (lookup) as default...")
        await example_lookup()



if __name__ == "__main__":
    asyncio.run(main())
