"""Load gitignored local settings without overriding variables already set.

A deployed Lambda keeps the environment it was given. ``config.local.json``
fills only names that are still empty.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "config.local.json"

# JSON key -> environment variable. Process environment wins over the file.
FIELDS: dict[str, str] = {
    "aws_account_id": "SRE_ACCOUNT_ID",
    "aws_region": "SRE_REGION",
    "aws_profile": "AWS_PROFILE",
    "confluence_base_url": "CONFLUENCE_BASE_URL",
    "confluence_page_id": "CONFLUENCE_PAGE_ID",
    "confluence_page_link": "CONFLUENCE_PAGE_LINK",
    "confluence_email": "CONFLUENCE_EMAIL",
    "confluence_token": "CONFLUENCE_TOKEN",
    "confluence_secret_arn": "CONFLUENCE_SECRET_ARN",
    "jira_base_url": "JIRA_BASE_URL",
    "jira_project": "JIRA_PROJECT",
    "jira_issue_type": "JIRA_ISSUE_TYPE",
    "jira_transition": "JIRA_TRANSITION",
    "jira_email": "JIRA_EMAIL",
    "jira_token": "JIRA_TOKEN",
    "jira_secret_arn": "JIRA_SECRET_ARN",
    "jira_browse_link": "JIRA_BROWSE_LINK",
    "gemini_api_key": "GEMINI_API_KEY",
    "gemini_enabled": "SRE_ENABLE_GEMINI",
    "gemini_model": "GEMINI_MODEL",
    "ses_from_address": "SES_FROM_ADDRESS",
    "aws_console_url": "AWS_CONSOLE_URL",
    "sites_url": "SRE_SITES_URL",
    "order_table": "SRE_ORDER_TABLE",
    "registry_table": "SRE_REGISTRY_TABLE",
    "explorer_view_arn": "SRE_EXPLORER_VIEW_ARN",
    "service_list_uri": "SRE_SERVICE_LIST_URI",
    "enable_config": "SRE_ENABLE_CONFIG",
    "discovery_role_arn": "SRE_DISCOVERY_ROLE_ARN",
    "executor_queue_url": "SRE_EXECUTOR_QUEUE_URL",
    "lab_targets": "LAB_TARGETS",
    "mode": "SRE_MODE",
    "approval_mode": "SRE_APPROVAL_MODE",
    "require_approval": "SRE_REQUIRE_APPROVAL",
    "approvers": "SRE_APPROVERS",
    "repairs_enabled": "SRE_REPAIRS_ENABLED",
}


def example_document() -> dict[str, str]:
    return {key: "" for key in FIELDS}


def load_local_config(root: Path | None = None) -> None:
    """Copy config.local.json into the environment where a variable is still unset.

    A deployed Lambda keeps the environment it was given. Local demo and CLI
    runs work from the JSON file alone. Unit tests supply their own environment.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    base = root or ROOT
    _apply_json(base / CONFIG_NAME)


def get(env_name: str) -> str:
    """Return one setting. An already-set environment variable wins over the file."""
    load_local_config()
    return os.environ.get(env_name, "").strip()


def public_settings() -> dict[str, str]:
    """Values the demo page may show. Secrets stay out of this dict."""
    return {
        "account_id": get("SRE_ACCOUNT_ID"),
        "region": get("SRE_REGION"),
        "confluence_page_id": get("CONFLUENCE_PAGE_ID"),
        "confluence_page_link": get("CONFLUENCE_PAGE_LINK"),
        "jira_browse_link": get("JIRA_BROWSE_LINK"),
        "sites_url": get("SRE_SITES_URL"),
        "ses_from_configured": "yes" if get("SES_FROM_ADDRESS") else "no",
    }


def _apply_json(path: Path) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    for key, env_name in FIELDS.items():
        if key not in payload or payload[key] is None:
            continue
        text = str(payload[key]).strip()
        if text:
            os.environ.setdefault(env_name, text)
