"""A service named on the Confluence page or in an S3 list is inventoried without a new collector."""

import json
from datetime import datetime, timedelta, timezone

from sre_agent.aws.fake import FakeCloud
from sre_agent.discovery.service import AccountTarget, Scope
from sre_agent.discovery.service_list import parse_service_list, types_to_discover
from sre_agent.integrations.confluence import FakeConfluence, load_catalog
from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig, ResourceRecord
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


_EKS_PAGE = """
<h2>Service: EKS (eks:cluster)</h2>
<table>
<tr><th>Symptom ID</th><th>Type</th><th>Namespace / log group</th><th>Dimension</th><th>Metric / query</th><th>Stat</th><th>Condition</th><th>Threshold</th><th>Window (min)</th><th>Role</th><th>Status</th></tr>
<tr><td>EKS-S01</td><td>metric</td><td>AWS/EKS</td><td>ClusterName={name}</td><td>cluster_failed_node_count</td><td>Maximum</td><td>&gt;</td><td>0</td><td>5</td><td>primary</td><td>Active</td></tr>
</table>
"""


def test_an_unlisted_confluence_service_keeps_its_aws_type():
    catalog = load_catalog(FakeConfluence(_EKS_PAGE, version=3))
    assert catalog.rules[0].resource_type == "eks:cluster"
    assert catalog.rules[0].namespace == "AWS/EKS"
    assert catalog.rules[0].dimension == "ClusterName={name}"


def test_the_service_file_can_name_the_type_for_a_heading():
    listed = parse_service_list(json.dumps([{"service": "EKS", "resource_type": "eks:cluster"}]))
    storage = _EKS_PAGE.replace("Service: EKS (eks:cluster)", "Service: EKS")
    catalog = load_catalog(FakeConfluence(storage), services=listed)
    assert catalog.rules[0].resource_type == "eks:cluster"
    assert [item.resource_type for item in types_to_discover(catalog.rules, listed)] == ["eks:cluster"]


def test_a_service_list_adds_that_type_and_the_catalog_marks_it_unhealthy():
    world = build_world()
    cluster = "arn:aws:eks:us-east-1:123456789012:cluster/payments"
    world.cloud.tagged["eks:cluster"] = [{"resource_id": cluster, "name": "payments"}]
    when = datetime(2026, 10, 6, 12, 50, tzinfo=timezone.utc)
    world.cloud.metrics[cluster] = {"cluster_failed_node_count": [(when, 2.0)]}
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="recommend"),
        confluence=FakeConfluence(_EKS_PAGE),
    )
    report = agent.run_cycle()
    found = [item for item in report.resources if item.resource_type == "eks:cluster"]
    assert [item.name for item in found] == ["payments"]
    assert found[0].health == "unhealthy"
    assert any(item.problem_code == "catalog.symptom" for item in report.diagnoses if item.resource_id == cluster)


def test_without_a_list_or_a_new_page_the_extra_collector_is_not_used():
    world = build_world()
    world.cloud.tagged["eks:cluster"] = [{"resource_id": "arn:aws:eks:us-east-1:123456789012:cluster/other", "name": "other"}]
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.run_cycle()
    assert "list_tagged" not in world.cloud.calls


def test_a_restricting_list_skips_a_page_type_that_is_not_named(monkeypatch, tmp_path):
    path = tmp_path / "services.json"
    path.write_text(json.dumps([{"service": "SNS", "resource_type": "sns:topic"}]), encoding="utf-8")
    monkeypatch.setenv("SRE_SERVICE_LIST_URI", str(path))
    cloud = FakeCloud("123456789012", "us-east-1")
    cloud.tagged["eks:cluster"] = [{"resource_id": "arn:aws:eks:us-east-1:123456789012:cluster/payments", "name": "payments"}]
    cloud.tagged["sns:topic"] = [{"resource_id": "arn:aws:sns:us-east-1:123456789012:alarms", "name": "alarms"}]
    scope = Scope(accounts=[AccountTarget("123456789012", ["us-east-1"], reader=cloud)])
    agent = OperationsAgent(
        scope,
        Registry(),
        __import__("sre_agent.lab.scenarios", fromlist=["build_world"]).build_world().clock,
        confluence=FakeConfluence(_EKS_PAGE),
    )
    report = agent.run_cycle()
    types = {item.resource_type for item in report.resources}
    assert "sns:topic" in types
    assert "eks:cluster" not in types
