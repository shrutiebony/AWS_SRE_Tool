"""Load Jira and Confluence settings from the environment or Secrets Manager.

Environment variables override a JSON secret. A missing login or a denied
read returns an empty string so discovery can continue. One secret may hold
the token and the connection fields. When only one of the two secret ARN
variables is set, both Confluence and Jira use that secret.
"""

from __future__ import annotations

import json

_PARTNER_ARN = {
    "CONFLUENCE_SECRET_ARN": "JIRA_SECRET_ARN",
    "JIRA_SECRET_ARN": "CONFLUENCE_SECRET_ARN",
}

_TOKEN_NAMES = frozenset({"CONFLUENCE_TOKEN", "JIRA_TOKEN"})

# Env name, then JSON keys. Token aliases stay off the other fields.
_SETTING_KEYS = {
    "CONFLUENCE_TOKEN": ("CONFLUENCE_TOKEN", "JIRA_TOKEN", "token", "api_token", "password"),
    "JIRA_TOKEN": ("JIRA_TOKEN", "CONFLUENCE_TOKEN", "token", "api_token", "password"),
    "CONFLUENCE_BASE_URL": ("CONFLUENCE_BASE_URL", "JIRA_BASE_URL", "base_url", "site"),
    "JIRA_BASE_URL": ("JIRA_BASE_URL", "CONFLUENCE_BASE_URL", "base_url", "site"),
    "CONFLUENCE_EMAIL": ("CONFLUENCE_EMAIL", "JIRA_EMAIL", "email"),
    "JIRA_EMAIL": ("JIRA_EMAIL", "CONFLUENCE_EMAIL", "email"),
    "CONFLUENCE_PAGE_ID": ("CONFLUENCE_PAGE_ID", "confluence_page_id"),
    "JIRA_PROJECT": ("JIRA_PROJECT", "jira_project"),
    "JIRA_ISSUE_TYPE": ("JIRA_ISSUE_TYPE", "jira_issue_type"),
    "JIRA_TRANSITION": ("JIRA_TRANSITION", "jira_transition"),
}


def secret_value(name: str, arn_name: str = "", arn: str = "") -> str:
    from sre_agent.local_config import get

    direct = get(name)
    if direct:
        return direct
    secret_arn = arn.strip() if arn else ""
    if not secret_arn and arn_name:
        secret_arn = _resolve_arn(arn_name)
    if not secret_arn:
        return ""
    parsed = _parsed_secret(secret_arn)
    if isinstance(parsed, dict):
        keys = _SETTING_KEYS.get(name, (name, "token", "password", "api_token"))
        return _pick(parsed, keys)
    if name in _SETTING_KEYS and name not in _TOKEN_NAMES:
        return ""
    return parsed if isinstance(parsed, str) else ""


def _resolve_arn(arn_name: str) -> str:
    from sre_agent.local_config import get

    primary = get(arn_name)
    if primary:
        return primary
    partner = _PARTNER_ARN.get(arn_name, "")
    if not partner:
        return ""
    return get(partner)


def _parsed_secret(secret_arn: str) -> dict | str:
    raw = _fetch_secret_string(secret_arn)
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        return parsed
    return raw


def _pick(parsed: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = parsed.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _fetch_secret_string(secret_arn: str) -> str:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        response = boto3.client("secretsmanager", region_name=_region(secret_arn)).get_secret_value(SecretId=secret_arn)
    except (BotoCoreError, ClientError):
        return ""
    return response.get("SecretString") or ""


def _region(arn: str) -> str | None:
    parts = arn.split(":")
    if len(parts) > 3 and parts[2] == "secretsmanager" and parts[3]:
        return parts[3]
    return None
