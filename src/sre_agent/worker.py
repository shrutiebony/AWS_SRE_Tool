"""Queue worker. It discovers and records incidents. It does not repair unless a separate executor is configured."""

from __future__ import annotations

import json

from sre_agent.clock import SystemClock
from sre_agent.models import AgentConfig
from sre_agent.service import OperationsAgent


def handler(event, context) -> dict:
    del context
    agent = build_agent()
    for record in event.get("Records") or []:
        body = json.loads(record.get("body") or "{}")
        if body.get("type") in {"reconcile", "monitor", "verify", "refresh", "investigate"}:
            agent.queue.send(body)
        else:
            agent.accept_event(body)
    if not event.get("Records"):
        agent.queue.send({"type": "reconcile"})
    handled = agent.process_queue()
    _emit_agent_metrics(agent)
    return {"handled": handled}


def build_agent() -> OperationsAgent:
    from sre_agent.aws.boto3_reader import Boto3Reader
    from sre_agent.discovery.service import AccountTarget, Scope
    from sre_agent.local_config import get
    from sre_agent.registry.store import Boto3TableAdapter, Registry

    region = get("SRE_REGION")
    account_id = get("SRE_ACCOUNT_ID")
    table_name = get("SRE_REGISTRY_TABLE")
    if not region or not account_id or not table_name:
        raise RuntimeError("Account, region, and registry table come from config.local.json or the process environment.")
    reader = Boto3Reader(
        role_arn=get("SRE_DISCOVERY_ROLE_ARN") or None,
        region=region,
        explorer_view_arn=get("SRE_EXPLORER_VIEW_ARN") or None,
        order_table=get("SRE_ORDER_TABLE") or None,
    )
    scope = Scope(
        accounts=[
            AccountTarget(
                account_id=account_id,
                regions=[region],
                reader=reader,
                enable_resource_explorer=bool(get("SRE_EXPLORER_VIEW_ARN")),
                enable_config=get("SRE_ENABLE_CONFIG") == "true",
            )
        ]
    )
    import boto3

    registry = Registry(Boto3TableAdapter(boto3.resource("dynamodb").Table(table_name)))
    mode = get("SRE_MODE") or "recommend"
    approval_mode = get("SRE_APPROVAL_MODE") or "required"
    from sre_agent.integrations.secrets import secret_value

    targets = frozenset(item for item in get("LAB_TARGETS").split(",") if item)
    config = AgentConfig(
        mode=mode,
        enabled_automatic_runbooks=frozenset(["sqs.redrive_dlq"]) if mode == "automatic" else frozenset(),
        lab_targets=targets,
        require_approval=(get("SRE_REQUIRE_APPROVAL") or "true") == "true" or approval_mode == "required",
        approvers=frozenset(item for item in get("SRE_APPROVERS").split(",") if item),
        approval_mode=approval_mode,
        jira_issue_type=secret_value("JIRA_ISSUE_TYPE", "JIRA_SECRET_ARN") or "Task",
        jira_transition=secret_value("JIRA_TRANSITION", "JIRA_SECRET_ARN") or "Done",
    )
    confluence = _confluence()
    jira = _jira()
    # Repairs go to a separate executor Lambda. This worker does not assume that role.
    repair_queue = _repair_sender()
    return OperationsAgent(
        scope,
        registry,
        SystemClock(),
        config,
        confluence=confluence,
        jira=jira,
        repair_queue=repair_queue,
    )


def _confluence():
    from sre_agent.integrations.confluence import HttpConfluence
    from sre_agent.integrations.secrets import secret_value

    base = secret_value("CONFLUENCE_BASE_URL", "CONFLUENCE_SECRET_ARN")
    page = secret_value("CONFLUENCE_PAGE_ID", "CONFLUENCE_SECRET_ARN")
    email = secret_value("CONFLUENCE_EMAIL", "CONFLUENCE_SECRET_ARN")
    token = secret_value("CONFLUENCE_TOKEN", "CONFLUENCE_SECRET_ARN")
    if not (base and page and email and token):
        return None
    return HttpConfluence(base, page, email, token)


def _jira():
    from sre_agent.integrations.jira import HttpJira
    from sre_agent.integrations.secrets import secret_value

    base = secret_value("JIRA_BASE_URL", "JIRA_SECRET_ARN")
    project = secret_value("JIRA_PROJECT", "JIRA_SECRET_ARN")
    email = secret_value("JIRA_EMAIL", "JIRA_SECRET_ARN")
    token = secret_value("JIRA_TOKEN", "JIRA_SECRET_ARN")
    if not (base and project and email and token):
        return None
    return HttpJira(
        base,
        project,
        email,
        token,
        transition_name=secret_value("JIRA_TRANSITION", "JIRA_SECRET_ARN") or "Done",
        issue_type=secret_value("JIRA_ISSUE_TYPE", "JIRA_SECRET_ARN") or "Task",
    )


def _repair_sender():
    from sre_agent.local_config import get

    queue_url = get("SRE_EXECUTOR_QUEUE_URL")
    if not queue_url:
        return None

    def send(body: dict, queue_url: str = queue_url) -> None:
        import boto3

        boto3.client("sqs").send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))

    return send


def _emit_agent_metrics(agent: OperationsAgent) -> None:
    try:
        import boto3
    except ImportError:
        return
    catalog_failed = 1 if agent.catalog_status == "failed" else 0
    jira_failed = 1 if any(item.collector == "jira" and item.status == "error" for item in agent.registry.scans()) else 0
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace="SRE/Agent",
            MetricData=[
                {"MetricName": "CatalogLoadFailed", "Value": catalog_failed, "Unit": "Count"},
                {"MetricName": "JiraSyncFailed", "Value": jira_failed, "Unit": "Count"},
                {"MetricName": "QueueBacklog", "Value": len(agent.queue.pending()), "Unit": "Count"},
            ],
        )
    except Exception:
        return
