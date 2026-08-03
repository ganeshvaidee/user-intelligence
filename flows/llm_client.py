# flows/llm_client.py
# Provider toggle — one place to switch between the direct Anthropic API and
# AWS Bedrock. Set LLM_PROVIDER=bedrock to use bedrock_client.py; anything
# else (or unset) uses anthropic_client.py.

import os

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

# Deterministic by default — this is a security tool where reproducible tool
# selection, risk scoring, and judge/critic verdicts matter more than variety.
# Split in two: TEMPERATURE for the main agentic loop, JUDGE_TEMPERATURE for
# the completeness-judge/critic calls, since they may need to diverge later.
TEMPERATURE       = float(os.environ.get("LLM_TEMPERATURE", "0"))
JUDGE_TEMPERATURE = float(os.environ.get("LLM_JUDGE_TEMPERATURE", "0"))

if LLM_PROVIDER == "bedrock":
    from bedrock_client import client, BEDROCK_MODEL_ID as MODEL_ID
else:
    from anthropic_client import client, MODEL_ID
