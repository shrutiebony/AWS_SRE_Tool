"""Command line for the local demo, the lab, and a live read-only inventory."""

from __future__ import annotations

import argparse

from sre_agent.lab.fault_injection import main as lab_main
from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig
from sre_agent.registry.store import Registry
from sre_agent.report import format_report
from sre_agent.service import OperationsAgent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover existing resources, monitor them, and gate one repair.")
    parser.add_argument("command", choices=("demo", "lab", "inventory"))
    parser.add_argument("--account", default="123456789012")
    parser.add_argument("--region", default="us-east-2")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo()
    if args.command == "lab":
        return lab_main()
    return inventory(args.account, args.region)


def demo() -> int:
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(mode="recommend"))
    agent.discovery.ingest_sns(
        {
            "resource_id": world.queue_id,
            "owner": "payments",
            "notes": "optional registration; discovery does not require this",
        }
    )
    report = agent.run_cycle()
    print(format_report(report))
    print("")
    print("Terraform state contributed ownership only. The retired function was not in AWS and stayed uncertain.")
    print("SNS added optional metadata. The repair stayed a recommendation.")
    return 0


def inventory(account_id: str, region: str) -> int:
    from sre_agent.aws.boto3_reader import Boto3Reader
    import os

    from sre_agent.clock import SystemClock
    from sre_agent.discovery.service import AccountTarget, Scope
    from sre_agent.registry.store import Boto3TableAdapter, Registry
    from sre_agent.worker import _confluence, _jira

    reader = Boto3Reader(
        region=region,
        explorer_view_arn=os.environ.get("SRE_EXPLORER_VIEW_ARN"),
        order_table=os.environ.get("SRE_ORDER_TABLE") or None,
    )
    scope = Scope(
        accounts=[
            AccountTarget(
                account_id=account_id,
                regions=[region],
                reader=reader,
                enable_resource_explorer=bool(os.environ.get("SRE_EXPLORER_VIEW_ARN")),
                enable_config=os.environ.get("SRE_ENABLE_CONFIG") == "true",
            )
        ]
    )
    table_name = os.environ.get("SRE_REGISTRY_TABLE", "")
    if table_name:
        import boto3

        registry = Registry(Boto3TableAdapter(boto3.resource("dynamodb").Table(table_name)))
    else:
        registry = Registry()
    agent = OperationsAgent(
        scope,
        registry,
        SystemClock(),
        AgentConfig(mode="recommend", require_approval=True),
        confluence=_confluence(),
        jira=_jira(),
    )
    print(format_report(agent.run_cycle()))
    return 0
