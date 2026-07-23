# flows/anthropic_client.py
# Async Anthropic client (direct API) — one place to change credentials or model.

import anthropic
import os

MODEL_ID = os.environ.get("ANTHROPIC_MODEL_ID", "claude-sonnet-4-6")

client = anthropic.AsyncAnthropic()
