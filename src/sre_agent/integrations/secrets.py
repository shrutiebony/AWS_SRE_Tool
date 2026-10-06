"""Load Jira and Confluence credentials from the environment or Secrets Manager."""

from __future__ import annotations

import json
import os


def secret_value(name: str, arn_name: str = "") -> str:
    direct = os.environ.get(name, "")
    if direct:
        return direct
    arn = os.environ.get(arn_name, "") if arn_name else ""
    if not arn:
        return ""
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    raw = response.get("SecretString") or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return str(parsed.get(name) or parsed.get("token") or parsed.get("password") or "")
