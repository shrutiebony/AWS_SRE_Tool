"""Atlassian connection comes from a secret JSON or the environment. No network."""

import json

import pytest

from sre_agent.integrations.secrets import secret_value
from sre_agent.worker import _confluence, _jira

_CONNECTION_ENV = (
    "CONFLUENCE_BASE_URL",
    "CONFLUENCE_PAGE_ID",
    "CONFLUENCE_EMAIL",
    "CONFLUENCE_TOKEN",
    "CONFLUENCE_SECRET_ARN",
    "JIRA_BASE_URL",
    "JIRA_PROJECT",
    "JIRA_EMAIL",
    "JIRA_TOKEN",
    "JIRA_SECRET_ARN",
    "JIRA_ISSUE_TYPE",
    "JIRA_TRANSITION",
)

_SECRET_ARN = "arn:aws:secretsmanager:us-east-2:000000000000:secret:atlassian-example"


def _clear_env(monkeypatch) -> None:
    for name in _CONNECTION_ENV:
        monkeypatch.delenv(name, raising=False)


def _install_secret(monkeypatch, payload) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    def fetch(secret_arn: str, expected: str = _SECRET_ARN, body: str = raw) -> str:
        assert secret_arn == expected
        return body

    monkeypatch.setattr("sre_agent.integrations.secrets._fetch_secret_string", fetch)


def test_secret_json_supplies_site_email_page_project_and_token(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONFLUENCE_SECRET_ARN", _SECRET_ARN)
    _install_secret(
        monkeypatch,
        {
            "site": "https://example.atlassian.net",
            "email": "agent@example.com",
            "confluence_page_id": "99",
            "jira_project": "OPS",
            "token": "unit-test-token",
            "jira_issue_type": "Story",
            "jira_transition": "Complete",
        },
    )

    confluence = _confluence()
    jira = _jira()

    assert confluence is not None
    assert confluence.base_url == "https://example.atlassian.net"
    assert confluence.page_id == "99"
    assert confluence.email == "agent@example.com"
    assert confluence.token == "unit-test-token"
    assert jira is not None
    assert jira.base_url == "https://example.atlassian.net"
    assert jira.project == "OPS"
    assert jira.email == "agent@example.com"
    assert jira.token == "unit-test-token"
    assert jira.issue_type == "Story"
    assert jira.transition_name == "Complete"

    monkeypatch.setenv("JIRA_PROJECT", "FROMENV")
    assert _jira().project == "FROMENV"


def test_missing_secret_fields_do_not_fall_back_to_hardcoded_site(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("JIRA_SECRET_ARN", _SECRET_ARN)
    _install_secret(monkeypatch, {"api_token": "unit-test-token"})

    assert _confluence() is None
    assert _jira() is None
    assert secret_value("CONFLUENCE_BASE_URL", "CONFLUENCE_SECRET_ARN") == ""
    assert secret_value("CONFLUENCE_EMAIL", "CONFLUENCE_SECRET_ARN") == ""
    assert secret_value("CONFLUENCE_PAGE_ID", "CONFLUENCE_SECRET_ARN") == ""
    assert secret_value("JIRA_PROJECT", "JIRA_SECRET_ARN") == ""
    assert secret_value("CONFLUENCE_TOKEN", "CONFLUENCE_SECRET_ARN") == "unit-test-token"
    assert secret_value("JIRA_ISSUE_TYPE", "JIRA_SECRET_ARN") == ""

    _clear_env(monkeypatch)
    assert secret_value("CONFLUENCE_BASE_URL", "CONFLUENCE_SECRET_ARN") == ""
    assert secret_value("JIRA_PROJECT", "JIRA_SECRET_ARN") == ""
    assert secret_value("CONFLUENCE_TOKEN", "CONFLUENCE_SECRET_ARN") == ""

    _clear_env(monkeypatch)
    monkeypatch.setenv("CONFLUENCE_SECRET_ARN", _SECRET_ARN)
    _install_secret(
        monkeypatch,
        {
            "base_url": "https://example.atlassian.net",
            "email": "agent@example.com",
            "confluence_page_id": "99",
            "jira_project": "OPS",
            "password": "unit-test-token",
        },
    )
    jira = _jira()
    assert jira is not None
    assert jira.project == "OPS"
    assert jira.issue_type == "Task"


def test_missing_aws_login_does_not_crash(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import NoCredentialsError

    _clear_env(monkeypatch)
    monkeypatch.setenv("CONFLUENCE_SECRET_ARN", _SECRET_ARN)

    def client(service_name, region_name=None):
        assert service_name == "secretsmanager"
        assert region_name == "us-east-2"

        class Denied:
            def get_secret_value(self, SecretId):
                raise NoCredentialsError()

        return Denied()

    monkeypatch.setattr(boto3, "client", client)
    assert secret_value("CONFLUENCE_TOKEN", "CONFLUENCE_SECRET_ARN") == ""
    assert _confluence() is None
    assert _jira() is None
