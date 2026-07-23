# flows/llm_client.py
# Provider toggle — one place to switch between the direct Anthropic API and
# AWS Bedrock. Set LLM_PROVIDER=bedrock to use bedrock_client.py; anything
# else (or unset) uses anthropic_client.py.

import os

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

if LLM_PROVIDER == "bedrock":
    from bedrock_client import client, BEDROCK_MODEL_ID as MODEL_ID
else:
    from anthropic_client import client, MODEL_ID
