"""Executor Lambda. It uses its own role. The discovery worker does not assume that role.

Repairs stay off until SRE_REPAIRS_ENABLED=true. Deployed Terraform leaves that false.
"""

from __future__ import annotations

import json


def handler(event, context) -> dict:
    del context
    from sre_agent.local_config import get

    if get("SRE_REPAIRS_ENABLED") != "true":
        return {"handled": [], "repairs": "disabled"}
    agent = build_executor()
    handled = []
    for record in event.get("Records") or []:
        body = json.loads(record.get("body") or "{}")
        if body.get("type") != "repair":
            continue
        decision = next(
            (
                item
                for item in agent.registry.decisions()
                if item.incident_id == body.get("incident_id") and item.outcome == "execute"
            ),
            None,
        )
        if decision is None:
            handled.append("missing-decision")
            continue
        actions = [agent.executor.execute(decision)]
        agent.verify(actions)
        handled.append(actions[0].status)
    return {"handled": handled, "repairs": "enabled"}


def build_executor():
    from sre_agent.aws.boto3_reader import Boto3Reader
    from sre_agent.clock import SystemClock
    from sre_agent.discovery.service import AccountTarget, Scope
    from sre_agent.models import AgentConfig
    from sre_agent.registry.store import Boto3TableAdapter, Registry
    from sre_agent.service import OperationsAgent
    from sre_agent.worker import _confluence, _jira

    from sre_agent.local_config import get

    region = get("SRE_REGION")
    account_id = get("SRE_ACCOUNT_ID")
    table_name = get("SRE_REGISTRY_TABLE")
    if not region or not account_id or not table_name:
        raise RuntimeError("Account, region, and registry table come from config.local.json or the process environment.")
    reader = Boto3Reader(region=region, order_table=get("SRE_ORDER_TABLE") or None)
    scope = Scope(accounts=[AccountTarget(account_id=account_id, regions=[region], reader=reader)])
    import boto3

    registry = Registry(Boto3TableAdapter(boto3.resource("dynamodb").Table(table_name)))
    from sre_agent.integrations.secrets import secret_value

    approval_mode = get("SRE_APPROVAL_MODE") or "required"
    targets = frozenset(item for item in get("LAB_TARGETS").split(",") if item)
    config = AgentConfig(
        mode="automatic",
        enabled_automatic_runbooks=frozenset(["sqs.redrive_dlq"]),
        lab_targets=targets,
        require_approval=(get("SRE_REQUIRE_APPROVAL") or "true") == "true" or approval_mode == "required",
        approvers=frozenset(item for item in get("SRE_APPROVERS").split(",") if item),
        approval_mode=approval_mode,
        jira_issue_type=secret_value("JIRA_ISSUE_TYPE", "JIRA_SECRET_ARN") or "Task",
        jira_transition=secret_value("JIRA_TRANSITION", "JIRA_SECRET_ARN") or "Done",
    )

    def mutator(account_id: str, reader=reader):
        del account_id
        return reader

    return OperationsAgent(
        scope,
        registry,
        SystemClock(),
        config,
        mutator_for=mutator,
        confluence=_confluence(),
        jira=_jira(),
    )
