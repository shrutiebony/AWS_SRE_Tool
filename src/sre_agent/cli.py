"""Command line for the local demo, the lab, and a live read-only inventory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sre_agent.lab.fault_injection import main as lab_main
from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig
from sre_agent.registry.store import Registry
from sre_agent.report import format_report
from sre_agent.service import OperationsAgent


def _load_local_env() -> None:
    """Fill unset variables from a gitignored .env next to the project."""
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    parser = argparse.ArgumentParser(description="Discover existing resources, monitor them, and gate one repair.")
    parser.add_argument("command", choices=("demo", "lab", "inventory", "mcp"))
    parser.add_argument("--account", default=os.environ.get("SRE_ACCOUNT_ID", "123456789012"))
    parser.add_argument("--region", default=os.environ.get("SRE_REGION", "us-east-2"))
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo()
    if args.command == "lab":
        return lab_main()
    if args.command == "mcp":
        from sre_agent.mcp_server import serve

        serve()
        return 0
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


def build_live_agent(account_id: str, region: str) -> OperationsAgent:
    """The same read-only agent `inventory` runs against a live account."""
    from sre_agent.aws.boto3_reader import Boto3Reader
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
    return OperationsAgent(
        scope,
        registry,
        SystemClock(),
        AgentConfig(mode="recommend", require_approval=True),
        confluence=_confluence(),
        jira=_jira(),
    )


def run_live_cycle(account_id: str, region: str):
    """Run one inventory cycle and return the report plus the agent that produced it."""
    agent = build_live_agent(account_id, region)
    return agent.run_cycle(), agent


def inventory(account_id: str, region: str) -> int:
    report, _agent = run_live_cycle(account_id, region)
    print(format_report(report))
    return 0
