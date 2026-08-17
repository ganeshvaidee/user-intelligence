# flows/llm_client.py
# Provider toggle — the single place that decides which client the flows use.
#
#   LLM_PROVIDER=anthropic  (default)  direct Anthropic API
#   LLM_PROVIDER=bedrock               Claude via AWS Bedrock
#   LLM_PROVIDER=local                 open-weight model on LM Studio/vLLM/SGLang
#   LLM_PROVIDER=openai                hosted OpenAI API
#
# The Anthropic path is privileged on purpose. Its features are written
# unconditionally into run_flow.py and tools.py — cache_control, thinking,
# output_config, forced tool_choice, streaming — and openai_compat_client.py
# adapts the other providers to look like that. Those providers are allowed to
# support less; what they must never do is make the Anthropic code simpler,
# blander, or conditional. See docs/improvements/multi-provider.md.

import os

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

# Deterministic by default — this is a security tool where reproducible tool
# selection, risk scoring, and judge/critic verdicts matter more than variety.
# Split in two: TEMPERATURE for the main agentic loop, JUDGE_TEMPERATURE for
# the completeness-judge/critic calls, since they may need to diverge later.
TEMPERATURE       = float(os.environ.get("LLM_TEMPERATURE", "0"))
JUDGE_TEMPERATURE = float(os.environ.get("LLM_JUDGE_TEMPERATURE", "0"))


# ── Capability declaration ────────────────────────────────────────
#
# What each provider can actually honour:
#
#   prompt_caching      explicit cache_control breakpoints, billed and reported
#                       with separate write/read counters
#   thinking_blocks     reasoning traces returned in the response body
#   effort              low/medium/high/xhigh reasoning-depth control
#   forced_tool_choice  a tool_choice the server genuinely enforces
#
# Callers consult this ONLY where an unsupported feature would fail *silently*
# — currently just thinking blocks, whose absence would empty the [THINKING]
# audit trail without any error. Everything else is refused outright by
# openai_compat_client with UnsupportedFeature, which needs no caller check.
# Resist growing this into a general capability-negotiation layer: the moment
# flows start branching per capability, the Anthropic path stops being the
# straightforwardly readable one, which is the whole point of the arrangement.

_CAPABILITIES: dict[str, set[str]] = {
    "anthropic": {"prompt_caching", "thinking_blocks", "effort", "forced_tool_choice"},
    "bedrock":   {"prompt_caching", "thinking_blocks", "effort", "forced_tool_choice"},

    # Local open-weight model behind an OpenAI-compatible server (LM Studio,
    # vLLM, SGLang). All three verified against Muse Glimmer 30B on LM Studio:
    #   effort              "Reasoning strength: <level>" in the system prompt
    #                       genuinely moves reasoning spend (150 tokens at low
    #                       vs 631 at xhigh on the same prompt)
    #   forced_tool_choice  tool_choice="required" is enforced, not a hint — the
    #                       model emitted a tool call even when told not to
    #   thinking_blocks     the trace comes back in reasoning_content, which the
    #                       adapter maps to a ThinkingBlock
    # "prompt_caching" is absent because there is no breakpoint to place; hits
    # still land via prompt_tokens_details.cached_tokens where the server
    # reports them.
    "local":     {"effort", "forced_tool_choice", "thinking_blocks"},

    # The hosted API bills reasoning tokens but does not return the trace, so
    # thinking_blocks stays off here.
    "openai":    {"effort", "forced_tool_choice"},
}

if LLM_PROVIDER not in _CAPABILITIES:
    raise RuntimeError(
        f"Unknown LLM_PROVIDER {LLM_PROVIDER!r}. "
        f"Expected one of {sorted(_CAPABILITIES)}."
    )

CAPABILITIES = _CAPABILITIES[LLM_PROVIDER]


def supports(feature: str) -> bool:
    """True if the active provider can honour `feature`. See _CAPABILITIES."""
    return feature in CAPABILITIES


# ── Client resolution ─────────────────────────────────────────────

if LLM_PROVIDER == "bedrock":
    from bedrock_client import client, BEDROCK_MODEL_ID as MODEL_ID
elif LLM_PROVIDER in ("local", "openai"):
    # Imported lazily inside the branch so `openai` stays an optional install:
    # the default Anthropic path must not require it. See flows/requirements-local.txt.
    from openai_compat_client import make_client
    client, MODEL_ID = make_client(LLM_PROVIDER)
else:
    from anthropic_client import client, MODEL_ID
