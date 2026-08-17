# client/cli.py
#
# Interactive CLI for the User Intelligence orchestrator.
# Mirrors the menu from flows/run_flow.py — same 6 flows, same prompts.
#
# Requires a running orchestrator:
#   MCP_URL=http://localhost:8001 python orchestrator/app.py
#
# Run: python client/cli.py
# Run with custom orchestrator: ORCHESTRATOR_URL=http://localhost:8000 python client/cli.py

import json
import os
import sys
import httpx

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000")
_timeout_env = os.environ.get("CLIENT_TIMEOUT")
CLIENT_TIMEOUT = None if _timeout_env == "0" else float(_timeout_env or 3600)

# Each flow: (display name, skill_names, flow_type, input_prompts, request_template)
# input_prompts is a list of (label, key) pairs — each becomes one input() call.
# request_template is formatted with the collected values.
FLOWS = {
    "1": (
        "Lookup user",
        ["_base", "lookup-user"],
        "single",
        [("User ID", "user_id")],
        "Look up user {user_id}",
    ),
    "2": (
        "Risk assessment",
        ["_base", "lookup-user", "user-risk-profile"],
        "single",
        [("User ID", "user_id")],
        "Give me a risk assessment for {user_id}",
    ),
    "3": (
        "Full offboarding",
        ["_base", "lookup-user", "user-risk-profile", "offboard-user"],
        "single",
        [("User ID", "user_id"), ("Reason", "reason")],
        "Offboard user {user_id}. Reason: {reason}",
    ),
    "4": (
        "Find by email + risk",
        ["_base", "lookup-user", "user-risk-profile"],
        "single",
        [("Email", "email")],
        "Check the risk profile for {email}",
    ),
    "5": (
        "Risk assessment (convergence loop)",
        ["_base", "lookup-user", "user-risk-profile"],
        "convergence",
        [("User ID", "user_id")],
        "Give me a thorough risk assessment for {user_id}",
    ),
    "6": (
        "Risk assessment (critic-revise)",
        ["_base", "lookup-user", "user-risk-profile"],
        "reflection",
        [("User ID", "user_id")],
        "Give me a risk assessment for {user_id}",
    ),
    "7": (
        "Risk assessment (parallel agents)",
        [],
        "risk-parallel",
        [("User ID", "user_id")],
        "{user_id}",
    ),
    "8": (
        "Risk assessment (parallel agents + extended thinking)",
        [],
        "risk-parallel-thinking",
        [("User ID", "user_id")],
        "{user_id}",
    ),
    "9": (
        "Risk assessment (parallel + extended thinking + memory)",
        [],
        "risk-parallel-memory",
        [("User ID", "user_id")],
        "{user_id}",
    ),
}


_thinking_buffers: dict[str, str] = {}


def _render_event(event: dict) -> bool:
    """
    Render one SSE event. Returns True when the stream is finished.

    Shared by both streaming callers. It used to be copy-pasted into each, and
    this repo has already been bitten by that: duplicate tool-call detection
    drifted across five copies until only one still worked. One implementation
    means a new event type cannot land in half the client.

    Routing rule — stdout carries the answer and nothing else, so it stays
    pipeable. Warnings and reasoning go to stderr.
    """
    if event.get("done"):
        _flush_thinking()
        print()
        return True

    if "error" in event:
        _flush_thinking()
        print(f"\nERROR: {event['error']}")
        sys.exit(1)

    if "warning" in event:
        # A degradation notice, e.g. a judge that returned no verdict.
        print(f"\n⚠  WARNING: {event['warning']}", file=sys.stderr)

    if "thinking" in event:
        # A dimension agent's reasoning, streamed live. Deltas arrive as
        # fragments and four agents stream concurrently, so buffer per dimension
        # and emit whole lines — raw passthrough interleaves them mid-word.
        dim = event.get("dimension", "?")
        buf = _thinking_buffers.get(dim, "") + event["thinking"]
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip():
                print(f"\033[2m[{dim}] {line.strip()}\033[0m", file=sys.stderr, flush=True)
        _thinking_buffers[dim] = buf

    if "text" in event:
        print(event["text"], end="", flush=True)

    return False


def _flush_thinking() -> None:
    """Emit trailing partial lines — usually each agent's concluding thought."""
    for dim, buf in _thinking_buffers.items():
        if buf.strip():
            print(f"\033[2m[{dim}] {buf.strip()}\033[0m", file=sys.stderr, flush=True)
    _thinking_buffers.clear()


def call_orchestrator_stream(user_request: str, skill_names: list, flow_type: str = "single", max_rounds: int = 3) -> None:
    """Stream any flow, printing tokens as they arrive."""
    try:
        with httpx.Client(timeout=CLIENT_TIMEOUT) as http:
            with http.stream("POST", f"{ORCHESTRATOR_URL}/flow/stream", json={
                "user_request": user_request,
                "skill_names":  skill_names,
                "flow_type":    flow_type,
                "max_rounds":   max_rounds,
            }) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    if _render_event(json.loads(line[6:])):
                        break
    except httpx.ConnectError:
        print(f"\nERROR: Could not connect to orchestrator at {ORCHESTRATOR_URL}")
        print("Is it running?  MCP_URL=http://localhost:8001 python orchestrator/app.py")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"\nERROR {e.response.status_code}: {e.response.text}")
        sys.exit(1)


def call_orchestrator(user_request: str, skill_names: list, flow_type: str) -> str:
    try:
        with httpx.Client(timeout=CLIENT_TIMEOUT) as http:
            response = http.post(f"{ORCHESTRATOR_URL}/flow", json={
                "user_request": user_request,
                "skill_names":  skill_names,
                "flow_type":    flow_type,
            })
            response.raise_for_status()
            payload = response.json()
            for warning in payload.get("warnings", []):
                print(f"\n⚠  WARNING: {warning}", file=sys.stderr)
            return payload["response"]
    except httpx.ConnectError:
        print(f"\nERROR: Could not connect to orchestrator at {ORCHESTRATOR_URL}")
        print("Is it running?  MCP_URL=http://localhost:8001 python orchestrator/app.py")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"\nERROR {e.response.status_code}: {e.response.text}")
        sys.exit(1)


def call_offboard_phase_stream(endpoint: str, user_id: str, reason: str) -> None:
    """Call /offboard/prepare/stream or /offboard/confirm/stream and print SSE output."""
    try:
        with httpx.Client(timeout=CLIENT_TIMEOUT) as http:
            with http.stream("POST", f"{ORCHESTRATOR_URL}{endpoint}",
                             json={"user_id": user_id, "reason": reason}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    if _render_event(json.loads(line[6:])):
                        break
    except httpx.ConnectError:
        print(f"\nERROR: Could not connect to orchestrator at {ORCHESTRATOR_URL}")
        print("Is it running?  MCP_URL=http://localhost:8001 python orchestrator/app.py")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"\nERROR {e.response.status_code}: {e.response.text}")
        sys.exit(1)


def run_offboard_hitl(user_id: str, reason: str) -> None:
    """
    Two-phase Human-in-the-Loop offboarding.
    Phase 1: assess + flag (orchestrator). Phase 2: human confirms. Phase 3: deactivate (orchestrator).
    Context between phases lives here in the client — the orchestrator is stateless.
    """
    # Phase 1 — assess and flag
    print(f"\n{'='*60}")
    print(f"PHASE 1: Assessing and flagging {user_id}...")
    print(f"{'='*60}\n")
    call_offboard_phase_stream("/offboard/prepare/stream", user_id, reason)

    # Human confirmation gate — client owns this
    print(f"\n{'='*60}")
    print("⚠️  Review the assessment above.")
    print("The account has been FLAGGED. Deactivation is pending your confirmation.")
    print(f"{'='*60}")
    response = input("\nType CONFIRM to deactivate, anything else to cancel: ").strip()

    if response.upper() != "CONFIRM":
        print("\n❌ Offboarding cancelled. Account remains flagged for review.")
        print(f"   User {user_id} is flagged in the DB — a security signal is recorded.")
        return

    # Phase 2 — deactivate
    print(f"\n{'='*60}")
    print(f"PHASE 2: Deactivating {user_id}...")
    print(f"{'='*60}\n")
    call_offboard_phase_stream("/offboard/confirm/stream", user_id, reason)


def main():
    print("User Intelligence — Flow Runner")
    print("================================")
    for key, (name, *_) in FLOWS.items():
        print(f"  {key}. {name}")

    choice = input("\nChoose a flow (1-9): ").strip()

    if choice not in FLOWS:
        print("Running flow 1 (lookup) as default...")
        choice = "1"

    name, skill_names, flow_type, prompts, template = FLOWS[choice]

    # Collect inputs
    values = {}
    for label, key in prompts:
        values[key] = input(f"{label}: ").strip()

    user_request = template.format(**values)

    # Option 3 — two-phase HITL offboarding
    if choice == "3":
        run_offboard_hitl(values["user_id"], values["reason"])
        return

    print(f"\n{'='*60}")
    print(f"REQUEST: {user_request}")
    print(f"SKILLS:  {', '.join(skill_names)}")
    print(f"{'='*60}\n")

    call_orchestrator_stream(user_request, skill_names, flow_type)


if __name__ == "__main__":
    main()
