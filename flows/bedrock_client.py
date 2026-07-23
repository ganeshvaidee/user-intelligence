# flows/bedrock_client.py
# Async Bedrock client — one place to change credentials or model.

import anthropic
import boto3
import os

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


def _make_bedrock_client() -> anthropic.AsyncAnthropicBedrock:
    session = boto3.Session(profile_name="default", region_name="us-west-2")
    creds   = session.get_credentials().get_frozen_credentials()
    return anthropic.AsyncAnthropicBedrock(
        aws_access_key=creds.access_key,
        aws_secret_key=creds.secret_key,
        aws_session_token=creds.token,
        aws_region=session.region_name or "us-east-1",
    )


client = _make_bedrock_client()
