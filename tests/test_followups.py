from datetime import datetime, timedelta, timezone

from sre_agent.executor_worker import handler as executor_handler
from sre_agent.integrations.confluence import FakeConfluence, load_catalog
from sre_agent.integrations.jira import FakeJira, approval_from_comments
from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig
from sre_agent.monitoring.catalog import aggregate_points, interpret_log_rows
from sre_agent.registry.store import Boto3TableAdapter, Registry
from sre_agent.service import OperationsAgent


def test_approval_uses_the_comment_author_not_text_in_the_comment():
    approval = approval_from_comments(
        "SRE-1",
        [
            {
                "author_account_id": "mallory",
                "body": "APPROVE resource=arn:queue action=sqs.redrive_dlq expires=2026-10-06T14:00:00+00:00 approver=alice",
            }
        ],
    )
    assert approval.approver == "mallory"
    world = build_world()
    jira = FakeJira()
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(
            mode="automatic",
            enabled_automatic_runbooks={"sqs.redrive_dlq"},
            require_approval=True,
            approvers={"alice"},
        ),
        jira=jira,
    )
    first = agent.run_cycle()
    diagnosis = next(item for item in first.diagnoses if item.problem_code == "sqs.dlq_transient")
    jira.comment(
        diagnosis.jira_issue_key,
        "mallory",
        f"APPROVE resource={world.queue_id} action=sqs.redrive_dlq expires=2026-10-06T14:00:00+00:00 approver=alice",
    )
    assert not any(item.outcome == "execute" for item in agent.run_cycle().decisions)


def test_lock_acquisition_is_a_conditional_write():
    registry = Registry()
    assert registry.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:00:00+00:00", 900)
    assert not registry.acquire_lock("queue", "sqs.redrive_dlq", "worker-b", "2026-10-06T13:00:00+00:00", 900)
    registry.release_lock("queue", "sqs.redrive_dlq", "worker-b")
    assert registry.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:01:00+00:00", 900)
    registry.release_lock("queue", "sqs.redrive_dlq", "worker-a")
    assert registry.acquire_lock("queue", "sqs.redrive_dlq", "worker-b", "2026-10-06T13:01:00+00:00", 900)

    class Table:
        def __init__(self):
            self.calls = []

        def put_item(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) > 1:
                raise ConditionalFailure()

    class ConditionalFailure(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    table = Table()
    adapter = Boto3TableAdapter(table)
    item = {"identity": "locks#queue|sqs.redrive_dlq", "owner": "worker-a", "expires_at": "2026-10-06T13:15:00+00:00"}
    assert adapter.put_conditional(item, "2026-10-06T13:00:00+00:00")
    assert not adapter.put_conditional({**item, "owner": "worker-b"}, "2026-10-06T13:00:00+00:00")
    assert "ConditionExpression" in table.calls[0]
    assert ":owner" in table.calls[0]["ExpressionAttributeValues"]


def test_redrive_tracks_order_ids_instead_of_the_task_handle():
    world = build_world()
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}),
    )
    report = agent.run_cycle()
    action = report.actions[0]
    assert action.order_ids == ["order-1", "order-2"]
    assert "moved_message_ids" not in action.api_result
    assert action.task_id.startswith("task-")


def test_monitor_and_verify_jobs_run():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.queue.send({"type": "monitor"})
    agent.queue.send({"type": "verify"})
    assert agent.process_queue() == ["monitor", "verify"]


def test_catalog_applies_statistic_period_and_query_aggregates():
    start = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
    points = [(start, 1.0), (start + timedelta(seconds=300), 10.0)]
    assert aggregate_points(points, "Sum", 300) == 11
    assert aggregate_points(points, "Maximum", 300) == 10
    assert aggregate_points(points, "Minimum", 300) == 1
    assert interpret_log_rows([{"count": "5", "message": ""}], "Count") == 5
    assert interpret_log_rows([{"message": "a"}, {"message": "b"}], "Count") == 2


def test_symptom_catalog_reads_the_service_tables():
    storage = """
<h2>Service: SQS</h2>
<h3>Scenarios</h3>
<table><tr><th>Scenario ID</th><th>Scenario</th></tr><tr><td>SQS-01</td><td>Visibility</td></tr></table>
<h3>Symptoms</h3>
<table>
<tr><th>Symptom ID</th><th>Scenario IDs</th><th>Type</th><th>Namespace / log group</th><th>Dimension</th><th>Metric / query</th><th>Stat</th><th>Condition</th><th>Threshold</th><th>Window (min)</th><th>Role</th><th>Status</th></tr>
<tr><td>SQS-S01</td><td>SQS-01</td><td>metric_math</td><td>AWS/SQS</td><td>QueueName={queue}</td><td>NumberOfMessagesReceived / NumberOfMessagesSent</td><td>Sum</td><td>&gt;</td><td>1.2</td><td>5</td><td>primary</td><td>Active</td></tr>
<tr><td>SQS-S02</td><td>SQS-01</td><td>metric</td><td>AWS/Lambda</td><td>FunctionName={worker_function}</td><td>Duration</td><td>Maximum</td><td>&gt;=</td><td>{visibility_timeout_ms} * 0.8</td><td>5</td><td>primary</td><td>Active</td></tr>
<tr><td>SQS-S03</td><td>SQS-02</td><td>metric</td><td>AWS/SQS</td><td>QueueName={queue}</td><td>ApproximateAgeOfOldestMessage</td><td>Maximum</td><td>&gt;</td><td>300</td><td>5</td><td>supporting</td><td>Disabled</td></tr>
</table>
"""
    catalog = load_catalog(FakeConfluence(storage, version=2))
    assert [rule.rule_id for rule in catalog.rules] == ["SQS-S01", "SQS-S02"]
    math_rule = catalog.rules[0]
    assert math_rule.resource_type == "sqs:queue"
    assert math_rule.statistic == "Expression"
    assert math_rule.comparison == "GreaterThanThreshold"
    assert math_rule.period_seconds == 300
    duration = catalog.rules[1].resolved({"visibility_timeout_ms": "30000", "worker_function": "payment-worker"})
    assert duration.threshold_expression == "30000 * 0.8"
    assert duration.resource_type == "lambda:function"


def test_an_introductory_confluence_table_does_not_hide_the_rules():
    rules = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th></tr>
<tr><td>lambda-error-rate</td><td>lambda:function</td><td>Errors</td><td>Sum</td><td>GreaterThanThreshold</td><td>1</td></tr>
</table>
"""
    storage = "<table><tr><td>Introduction</td></tr></table>" + rules
    catalog = load_catalog(FakeConfluence(storage, version=4))
    assert catalog.version == 4
    assert [rule.rule_id for rule in catalog.rules] == ["lambda-error-rate"]


def test_deployed_executor_does_not_repair_until_enabled():
    result = executor_handler({"Records": [{"body": '{"type": "repair"}'}]}, None)
    assert result["repairs"] == "disabled"
    assert result["handled"] == []
