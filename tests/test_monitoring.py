from sre_agent.lab.scenarios import build_world
from sre_agent.models import ResourceRecord
from sre_agent.monitoring.packs import assess
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


def test_three_service_packs_cover_healthy_degraded_and_unknown():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    assessments = {item.resource_id: item for item in agent.monitor()}
    assert assessments[world.consumer_id].state == "healthy"
    assert assessments[world.queue_id].state == "unhealthy"
    assert "Dead-letter queue grew" in " ".join(assessments[world.queue_id].reasons)
    assert assessments[world.dlq_id].state == "degraded"
    assert assessments[world.instance_id].state == "healthy"
    assert any("agent is required" in reason for reason in assessments[world.instance_id].reasons)
    analytics = agent.registry.get_by_resource_id(world.analytics_id)
    assert analytics.health == "unknown"
    assert analytics.monitoring_status == "configured"
    queue = agent.registry.get_by_resource_id(world.queue_id)
    assert queue.allowed_runbooks == ["sqs.redrive_dlq"]
    assert agent.registry.get_by_resource_id(world.dlq_id).allowed_runbooks == []


def test_missing_required_telemetry_is_unknown_not_healthy():
    record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-1:123456789012:function:bare",
        account_id="123456789012",
        region="us-east-1",
        resource_type="lambda:function",
        name="bare",
        config={"timeout": 30},
    )
    signals = {"Errors": 0, "Invocations": 10, "Throttles": 0, "Duration": None}
    sparse = assess(record, signals, {})
    assert sparse.state == "degraded"
    assert sparse.context["consumer_ready"] == "sparse"
    assert assess(record, {"Errors": None, "Invocations": None, "Throttles": None, "Duration": None}, {}).state == "unknown"


def test_lambda_error_rate_and_ec2_status():
    lambda_record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-1:123456789012:function:bare",
        account_id="123456789012",
        region="us-east-1",
        resource_type="lambda:function",
        name="bare",
        config={"timeout": 3},
    )
    failing = assess(
        lambda_record,
        {"Errors": 20, "Invocations": 100, "Throttles": 0, "Duration": 100},
        {},
    )
    assert failing.state == "unhealthy"
    near_timeout = assess(
        lambda_record,
        {"Errors": 0, "Invocations": 10, "Throttles": 0, "Duration": 2800},
        {},
    )
    assert near_timeout.state == "degraded"
    restored = assess(
        lambda_record,
        {"Errors": 25, "Invocations": 110, "Throttles": 0, "Duration": 100},
        {"latest_errors": "0", "latest_invocations": "10", "latest_throttles": "0", "latest_metric_age_seconds": "0"},
    )
    assert restored.state == "healthy"
    assert "Older errors" in " ".join(restored.reasons)
    stale = assess(
        lambda_record,
        {"Errors": 0, "Invocations": 10, "Throttles": 0, "Duration": 100},
        {"latest_errors": "0", "latest_invocations": "10", "latest_throttles": "0", "latest_metric_age_seconds": "600"},
    )
    assert stale.state == "degraded"
    assert stale.context["consumer_ready"] == "stale"
    instance = ResourceRecord(
        resource_id="arn:aws:ec2:us-east-1:123456789012:instance/i-1",
        account_id="123456789012",
        region="us-east-1",
        resource_type="ec2:instance",
        name="web",
    )
    impaired = assess(instance, {"StatusCheckFailed": 1, "CPUUtilization": 10}, {})
    assert impaired.state == "unhealthy"
    missing_cpu = assess(instance, {"StatusCheckFailed": 0, "CPUUtilization": None}, {})
    assert missing_cpu.state == "unknown"


def test_denied_metrics_are_unknown():
    world = build_world()
    world.cloud.deny.add("get_metrics")
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    assessments = agent.monitor()
    assert assessments
    assert all(item.state == "unknown" for item in assessments)
    assert any("does not mean" in reason for item in assessments for reason in item.reasons)
