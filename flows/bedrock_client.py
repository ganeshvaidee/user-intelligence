# flows/bedrock_client.py
# Async Bedrock client — one place to change credentials or model.

import anthropic
import boto3
import os

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


DEFAULT_REGION = "us-west-2"


def _make_bedrock_client() -> anthropic.AsyncAnthropicBedrock:
    # profile_name=None lets boto3 use its standard resolution chain:
    # AWS_PROFILE -> the "default" profile -> AWS_ACCESS_KEY_ID/SECRET env vars
    # -> instance/container role. Passing an explicit profile name here would
    # override AWS_PROFILE and hard-fail on any machine without a "default".
    session = boto3.Session(
        profile_name = os.environ.get("AWS_PROFILE"),
        region_name  = os.environ.get("AWS_REGION", DEFAULT_REGION),
    )

    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError(
            "No AWS credentials found for Bedrock. Set AWS_PROFILE to a configured "
            "profile, add a [default] profile to ~/.aws/credentials, or export "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. "
            "To use the Anthropic API instead, unset LLM_PROVIDER."
        )
    frozen = creds.get_frozen_credentials()

    return anthropic.AsyncAnthropicBedrock(
        aws_access_key    = frozen.access_key,
        aws_secret_key    = frozen.secret_key,
        aws_session_token = frozen.token,
        aws_region        = session.region_name or DEFAULT_REGION,
    )


client = _make_bedrock_client()
