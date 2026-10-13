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
    assert analytics.health == "healthy"
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
    missing = assess(record, {"Errors": None, "Invocations": None, "Throttles": None, "Duration": None}, {})
    assert missing.state == "degraded"
    assert "Errors" in " ".join(missing.reasons)
    assert "Invocations" in " ".join(missing.reasons)


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
    assert missing_cpu.state == "degraded"
    assert "CPUUtilization" in " ".join(missing_cpu.reasons)


def test_a_quiet_queue_with_no_datapoints_is_healthy():
    from datetime import datetime, timezone

    from sre_agent.monitoring.telemetry import sample

    start = datetime(2026, 10, 9, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 0, 15, tzinfo=timezone.utc)
    record = ResourceRecord(
        resource_id="arn:aws:sqs:us-east-2:123456789012:quiet",
        account_id="123456789012",
        region="us-east-2",
        resource_type="sqs:queue",
        name="quiet",
        config={"redrive_policy": {"deadLetterTargetArn": "arn:aws:sqs:us-east-2:123456789012:quiet-dlq"}},
        presence="present",
    )

    class Reader:
        def get_metric_data(self, *args, **kwargs):
            return {}

    assessment = sample(record, Reader(), start, end)
    assert assessment.state == "healthy"
    assert assessment.context.get("unreadable") != "yes"
    assert "ApproximateAgeOfOldestMessage" not in assessment.missing_signals
    assert assessment.observed_signals["ApproximateNumberOfMessagesVisible"] == 0
    assert assessment.observed_signals["dlq.ApproximateNumberOfMessagesVisible"] == 0


def test_batched_metrics_are_one_cloudwatch_call():
    from datetime import datetime, timezone

    from sre_agent.monitoring.telemetry import prefetched_reader, sample

    start = datetime(2026, 10, 9, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 0, 5, tzinfo=timezone.utc)
    record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-2:414279120882:function:worker",
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="worker",
        config={"timeout": 3},
        presence="present",
    )

    class Reader:
        def __init__(self) -> None:
            self.metric_calls = 0
            self.batch_calls = 0

        def query_metrics(self, region, queries, start, end):
            self.batch_calls += 1
            assert region == "us-east-2"
            assert len(queries) >= 4
            return {item["id"]: [(end, 0.0)] for item in queries}

        def get_metric_data(self, *args, **kwargs):
            self.metric_calls += 1
            raise AssertionError("per-resource GetMetricData")

    reader = Reader()
    wrapped = prefetched_reader([record], reader, [], start, end)
    assessment = sample(record, wrapped, start, end)
    assert reader.batch_calls == 1
    assert reader.metric_calls == 0
    assert assessment.state == "healthy"


def test_display_log_queries_use_a_short_window():
    from datetime import datetime, timezone

    from sre_agent.integrations.confluence import CatalogRule
    from sre_agent.monitoring.telemetry import prefetched_reader

    start = datetime(2026, 10, 9, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 0, 15, tzinfo=timezone.utc)
    record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-2:414279120882:function:worker",
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="worker",
        config={"timeout": 3},
        presence="present",
    )
    rule = CatalogRule(
        rule_id="logs",
        version=1,
        resource_type="lambda:function",
        signal="errors",
        statistic="Sum",
        comparison="GreaterThanThreshold",
        threshold=0,
        log_query="stats count(*)",
        period_seconds=900,
        namespace="/aws/lambda/worker",
    )

    class Reader:
        def __init__(self) -> None:
            self.spans: list[int] = []

        def query_metrics(self, region, queries, start, end):
            return {item["id"]: [(end, 0.0)] for item in queries}

        def run_logs_query(self, account_id, region, resource_id, query, start, end, log_group=None):
            self.spans.append(int((end - start).total_seconds()))
            return []

    reader = Reader()
    prefetched_reader([record], reader, [rule], start, end, log_seconds=120)
    assert reader.spans == [120]


def test_denied_metrics_are_unknown():
    world = build_world()
    world.cloud.deny.add("get_metrics")
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    assessments = agent.monitor()
    assert assessments
    assert all(item.state == "degraded" for item in assessments)
    assert any("does not mean" in reason for item in assessments for reason in item.reasons)
