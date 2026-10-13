"""Jira opens only when a symptom actually breached."""

from datetime import datetime, timezone

from sre_agent.diagnosis.agent import Snapshot
from sre_agent.diagnosis.rules import diagnose
from sre_agent.integrations.confluence import CatalogRule, FakeConfluence
from sre_agent.integrations.jira import FakeJira, issue_is_actionable
from sre_agent.lab.scenarios import build_world
from sre_agent.models import HealthAssessment, ResourceRecord
from sre_agent.monitoring.catalog import apply_catalog
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


PLACEHOLDER = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th><th>expression</th><th>log_query</th></tr>
<tr><td>SQS-VIS</td><td>lambda:function</td><td>${visibility_timeout_ms}</td><td>Maximum</td><td>GreaterThanThreshold</td><td>1</td><td></td><td></td></tr>
</table>
"""

BREACH = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th><th>expression</th><th>log_query</th></tr>
<tr><td>LAMBDA-ERR</td><td>lambda:function</td><td>CustomBreaches</td><td>Sum</td><td>GreaterThanThreshold</td><td>0</td><td></td><td></td></tr>
<tr><td>SQS-VIS</td><td>lambda:function</td><td>${visibility_timeout_ms}</td><td>Maximum</td><td>GreaterThanThreshold</td><td>1</td><td></td><td></td></tr>
</table>
"""


def test_unresolved_placeholder_does_not_change_health():
    record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-2:123456789012:function:worker",
        account_id="123456789012",
        region="us-east-2",
        resource_type="lambda:function",
        name="worker",
    )
    rule = CatalogRule(
        rule_id="SQS-VIS",
        version=1,
        resource_type="lambda:function",
        signal="${visibility_timeout_ms}",
        statistic="Maximum",
        comparison="GreaterThanThreshold",
        threshold=1,
    )
    start = datetime(2026, 10, 9, tzinfo=timezone.utc)
    end = datetime(2026, 10, 9, 0, 5, tzinfo=timezone.utc)
    result = apply_catalog(
        HealthAssessment(record.resource_id, "healthy", ["The latest invocations succeeded."]),
        [rule],
        record,
        None,
        start,
        end,
    )
    assert result.state == "healthy"
    assert result.reasons == ["The latest invocations succeeded."]
    assert "visibility_timeout_ms" in result.context["catalog_skips"]
    assert "Could not read catalog rule" not in " ".join(result.reasons)


def test_a_breached_catalog_rule_is_unhealthy():
    record = ResourceRecord(
        resource_id="arn:aws:lambda:us-east-2:123456789012:function:worker",
        account_id="123456789012",
        region="us-east-2",
        resource_type="lambda:function",
        name="worker",
    )
    rule = CatalogRule(
        rule_id="LAMBDA-ERR",
        version=2,
        resource_type="lambda:function",
        signal="Errors",
        statistic="Sum",
        comparison="GreaterThanThreshold",
        threshold=0,
    )
    end = datetime(2026, 10, 9, 0, 5, tzinfo=timezone.utc)

    class Reader:
        def get_metric_data(self, *args, **kwargs):
            return {"Errors": [(end, 4.0)]}

    result = apply_catalog(
        HealthAssessment(record.resource_id, "healthy", ["quiet"]),
        [rule],
        record,
        Reader(),
        datetime(2026, 10, 9, tzinfo=timezone.utc),
        end,
    )
    assert result.state == "unhealthy"
    assert any("LAMBDA-ERR" in reason and "Errors is 4" in reason for reason in result.reasons)


def test_placeholder_rule_is_a_scan_note_and_does_not_open_jira():
    world = build_world()
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=FakeConfluence(PLACEHOLDER), jira=jira)
    report = agent.run_cycle()
    analytics = next(item for item in report.assessments if item.resource_id == world.analytics_id)
    assert analytics.state == "healthy"
    assert not any("Could not read catalog rule" in reason for item in report.assessments for reason in item.reasons)
    assert any(
        item.collector == "catalog" and item.status == "note" and "visibility_timeout_ms" in item.message
        for item in report.scans
    )
    assert jira.issues
    assert not any("visibility_timeout" in issue.summary.lower() or "catalog rule" in issue.summary.lower() for issue in jira.issues.values())
    assert not any(item.problem_code == "catalog.symptom" for item in report.diagnoses)


def test_breached_symptom_opens_a_jira_issue_and_a_placeholder_does_not():
    world = build_world()
    now = world.clock.now()
    world.cloud.set_metric(world.analytics_id, "CustomBreaches", 4, now)
    world.cloud.set_metric(world.analytics_id, "Errors", 0, now)
    world.cloud.set_metric(world.analytics_id, "Invocations", 10, now)
    world.cloud.set_metric(world.analytics_id, "Throttles", 0, now)
    world.cloud.set_metric(world.analytics_id, "Duration", 100, now)
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=FakeConfluence(BREACH), jira=jira)
    report = agent.run_cycle()
    catalog = [item for item in report.diagnoses if item.problem_code == "catalog.symptom"]
    assert any(item.resource_id == world.analytics_id for item in catalog)
    assert any("LAMBDA-ERR" in issue.summary or "CustomBreaches" in issue.summary for issue in jira.issues.values())
    assert not any("visibility_timeout" in issue.summary.lower() for issue in jira.issues.values())
    assert any(item.problem_code.startswith("sqs.") for item in report.diagnoses)
    assert any(item.collector == "catalog" and item.status == "note" for item in report.scans)


def test_a_catalog_breach_does_not_hide_a_confirmed_queue_fault():
    world = build_world()
    page = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th></tr>
<tr><td>SQS-S03</td><td>sqs:queue</td><td>ApproximateAgeOfOldestMessage</td><td>Maximum</td><td>GreaterThanThreshold</td><td>300</td></tr>
</table>
"""
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=FakeConfluence(page), jira=jira)
    report = agent.run_cycle()
    queue = [item for item in report.diagnoses if item.resource_id == world.queue_id]
    assert queue
    assert queue[0].problem_code.startswith("sqs.")
    assert queue[0].jira_issue_key
    opened = jira.issues[queue[0].jira_issue_key]
    assert "placeholder" not in opened.summary.lower()
    assert "has no value" not in opened.summary.lower()
    assert "could not read catalog rule" not in opened.summary.lower()


def test_empty_source_with_dlq_messages_is_not_a_missing_consumer():
    """A dead-letter message is the fault. The empty source queue is not a backlog."""
    from datetime import datetime, timezone

    from sre_agent.diagnosis.agent import Snapshot
    from sre_agent.diagnosis.rules import diagnose
    from sre_agent.models import Relationship

    source_id = "arn:aws:sqs:us-east-2:123456789012:jobs"
    dlq_id = "arn:aws:sqs:us-east-2:123456789012:jobs-dlq"
    source = ResourceRecord(
        resource_id=source_id,
        account_id="123456789012",
        region="us-east-2",
        resource_type="sqs:queue",
        name="jobs",
        config={"redrive_policy": {"deadLetterTargetArn": dlq_id}},
        presence="present",
    )
    dlq = ResourceRecord(
        resource_id=dlq_id,
        account_id="123456789012",
        region="us-east-2",
        resource_type="sqs:queue",
        name="jobs-dlq",
        presence="present",
    )
    rule = CatalogRule(
        rule_id="SQS-S04",
        version=3,
        resource_type="sqs:queue",
        signal="ApproximateNumberOfMessagesVisible",
        statistic="Maximum",
        comparison="GreaterThanThreshold",
        threshold=0,
        dimension="QueueName={dlq}",
        namespace="AWS/SQS",
    )
    end = datetime(2026, 10, 11, 19, 12, tzinfo=timezone.utc)

    class Reader:
        def get_metric_data(self, account, region, resource_id, names, start, end, **kwargs):
            dimensions = kwargs.get("dimensions") or []
            queue = next((item["Value"] for item in dimensions if item.get("Name") == "QueueName"), "")
            if queue == "jobs-dlq":
                return {"ApproximateNumberOfMessagesVisible": [(end, 1.0)]}
            return {"ApproximateNumberOfMessagesVisible": [(end, 0.0)]}

    packed = HealthAssessment(
        source_id,
        "degraded",
        ["Dead-letter queue holds 1 messages."],
        observed_signals={
            "ApproximateNumberOfMessagesVisible": 0.0,
            "ApproximateAgeOfOldestMessage": 0.0,
            "dlq.ApproximateNumberOfMessagesVisible": 1.0,
        },
        context={"pack_state": "degraded", "dlq_growth": "unknown"},
    )
    overlaid = apply_catalog(
        packed,
        [rule.resolved({"dlq": "jobs-dlq", "queue": "jobs"})],
        source,
        Reader(),
        end,
        end,
    )
    assert overlaid.observed_signals["ApproximateNumberOfMessagesVisible"] == 0.0
    assert overlaid.state == "unhealthy"
    assert any("SQS-S04" in reason for reason in overlaid.reasons)
    link = Relationship(source_id, dlq_id, "dead_letters_to", "sqs-redrive-policy", {})
    now = end
    source_dx = diagnose(
        Snapshot(source_id, source, overlaid, {}, [link], {source_id: overlaid}, {}, [], []),
        now,
    )
    assert source_dx.problem_code != "sqs.no_consumer"
    assert source_dx.problem_code.startswith("sqs.dlq_")
    dlq_assessment = HealthAssessment(
        dlq_id,
        "degraded",
        ["Oldest message is 189s with 1 visible messages."],
        observed_signals={
            "ApproximateNumberOfMessagesVisible": 1.0,
            "ApproximateAgeOfOldestMessage": 189.0,
        },
    )
    dlq_dx = diagnose(
        Snapshot(dlq_id, dlq, dlq_assessment, {}, [link], {dlq_id: dlq_assessment}, {}, [], []),
        now,
    )
    assert dlq_dx.problem_code == "sqs.dlq_waiting"
    log_rule = CatalogRule(
        rule_id="SQS-S05",
        version=3,
        resource_type="sqs:queue",
        signal="value",
        statistic="Count",
        comparison="GreaterThanOrEqualToThreshold",
        threshold=3,
        dimension="-",
        log_query="filter @message like /ERROR/",
        namespace="/aws/lambda/jobs",
    )

    class LogReader(Reader):
        def run_logs_query(self, *args, **kwargs):
            return []

    logged = apply_catalog(packed, [log_rule], source, LogReader(), end, end)
    assert logged.observed_signals["ApproximateNumberOfMessagesVisible"] == 0.0


def test_a_healthy_first_inventory_opens_no_jira_issue():
    world = build_world()
    for queue in world.cloud.queues.values():
        queue.messages = 0
        queue.message_records = []
    world.cloud.metrics.pop(world.queue_id, None)
    world.cloud.metrics.pop(world.dlq_id, None)
    page = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th></tr>
<tr><td>SQS-S05</td><td>sqs:queue</td><td>${dlq}</td><td>Sum</td><td>GreaterThanThreshold</td><td>0</td></tr>
</table>
"""
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=FakeConfluence(page), jira=jira)
    agent.run_cycle()
    assert jira.issues == {}


def test_checker_queue_backlog_does_not_open_jira():
    """A healthy account, plus a backlog only on the checker's queues, opens no ticket."""
    from sre_agent.aws.fake import QueueRecord
    from sre_agent.lab.scenarios import queue_arn, queue_url

    world = build_world()
    now = world.clock.now()
    for queue in world.cloud.queues.values():
        queue.messages = 0
        queue.message_records = []
    world.cloud.metrics.pop(world.queue_id, None)
    world.cloud.metrics.pop(world.dlq_id, None)
    world.cloud.set_metric(world.queue_id, "ApproximateAgeOfOldestMessage", 0, now)
    world.cloud.set_metric(world.queue_id, "ApproximateNumberOfMessagesVisible", 0, now)
    world.cloud.set_metric(world.dlq_id, "ApproximateNumberOfMessagesVisible", 0, now)
    world.cloud.set_metric(world.dlq_id, "ApproximateAgeOfOldestMessage", 0, now)
    work_id = queue_arn("sre-agent-work")
    work_dlq_id = queue_arn("sre-agent-work-dlq")
    world.cloud.add_queue(
        QueueRecord(
            name="sre-agent-work",
            arn=work_id,
            url=queue_url("sre-agent-work"),
            redrive_policy={"deadLetterTargetArn": work_dlq_id, "maxReceiveCount": "3"},
            messages=5,
        )
    )
    world.cloud.add_queue(
        QueueRecord(
            name="sre-agent-work-dlq",
            arn=work_dlq_id,
            url=queue_url("sre-agent-work-dlq"),
            messages=2,
            message_records=[{"id": "msg-agent", "order_id": "order-agent"}],
        )
    )
    world.cloud.set_metric(work_id, "ApproximateNumberOfMessagesVisible", 5, now)
    world.cloud.set_metric(work_id, "ApproximateAgeOfOldestMessage", 900, now)
    world.cloud.set_metric(work_dlq_id, "ApproximateNumberOfMessagesVisible", 2, now)
    world.cloud.set_metric(work_dlq_id, "ApproximateAgeOfOldestMessage", 400, now)
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, jira=jira)
    report = agent.run_cycle()
    assert any(item.resource_id == work_id and item.state != "healthy" for item in report.assessments)
    assert any(item.resource_id == work_dlq_id and item.state != "healthy" for item in report.assessments)
    assert jira.issues == {}
    assert not any(item.jira_issue_key for item in report.diagnoses)


def test_new_healthy_lambda_and_empty_queue_are_not_tickets():
    now = datetime(2026, 10, 12, 19, 0, tzinfo=timezone.utc)
    function_id = "arn:aws:lambda:us-east-2:414279120882:function:sre-dir-lab"
    function = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "last_modified": "2026-10-12T18:50:00+00:00"},
    )
    quiet = HealthAssessment(
        function_id,
        "degraded",
        ["The newest Lambda sample is older than one period, so processing is not confirmed."],
        observed_signals={"Errors": 0.0, "Invocations": 0.0, "Throttles": 0.0, "Duration": 0.0},
    )
    quiet_dx = diagnose(Snapshot("inc-quiet", function, quiet, {}, [], {function_id: quiet}, {}, [], []), now)
    assert quiet_dx.problem_code == "healthy"
    assert quiet_dx.problem_code != "lambda.errors_after_deploy"
    assert issue_is_actionable(quiet_dx) is False

    healthy = HealthAssessment(
        function_id,
        "healthy",
        ["No invocations and no errors in the window."],
        observed_signals={"Errors": 0.0, "Invocations": 0.0, "Throttles": 0.0, "Duration": 0.0},
    )
    healthy_dx = diagnose(Snapshot("inc-healthy", function, healthy, {}, [], {function_id: healthy}, {}, [], []), now)
    assert healthy_dx.problem_code == "healthy"
    assert issue_is_actionable(healthy_dx) is False

    failing = HealthAssessment(
        function_id,
        "unhealthy",
        ["Error rate is 100%."],
        observed_signals={"Errors": 1.0, "Invocations": 1.0, "Throttles": 0.0, "Duration": 80.0},
    )
    failing_dx = diagnose(Snapshot("inc-fail", function, failing, {}, [], {function_id: failing}, {}, [], []), now)
    assert failing_dx.problem_code == "lambda.errors_after_deploy"
    assert issue_is_actionable(failing_dx) is True

    queue_id = "arn:aws:sqs:us-east-2:414279120882:sre-queue-lab"
    queue = ResourceRecord(
        resource_id=queue_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="sqs:queue",
        name="sre-queue-lab",
        config={"redrive_policy": {"deadLetterTargetArn": "arn:aws:sqs:us-east-2:414279120882:sre-queue-lab-dlq"}},
    )
    empty = HealthAssessment(
        queue_id,
        "degraded",
        ["Oldest message age is 400s."],
        observed_signals={
            "ApproximateNumberOfMessagesVisible": 0.0,
            "ApproximateAgeOfOldestMessage": 400.0,
            "dlq.ApproximateNumberOfMessagesVisible": 0.0,
        },
    )
    empty_dx = diagnose(Snapshot("inc-empty", queue, empty, {}, [], {queue_id: empty}, {}, [], []), now)
    assert empty_dx.problem_code == "healthy"
    assert empty_dx.problem_code != "sqs.no_consumer"
    assert issue_is_actionable(empty_dx) is False

    backlog = HealthAssessment(
        queue_id,
        "degraded",
        ["Oldest message is 90s with 1 visible messages."],
        observed_signals={
            "ApproximateNumberOfMessagesVisible": 1.0,
            "ApproximateAgeOfOldestMessage": 90.0,
            "dlq.ApproximateNumberOfMessagesVisible": 0.0,
        },
    )
    backlog_dx = diagnose(Snapshot("inc-backlog", queue, backlog, {}, [], {queue_id: backlog}, {}, [], []), now)
    assert backlog_dx.problem_code == "sqs.no_consumer"
    assert issue_is_actionable(backlog_dx) is True


def test_display_cycle_does_not_open_jira():
    world = build_world()
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, jira=jira)
    report = agent.run_display_cycle()
    assert jira.issues == {}
    assert any(item.state == "unhealthy" for item in report.assessments)


def test_empty_queue_ignores_a_stale_visible_message_catalog_breach():
    """SQS-S04 Maximum over the lookback is not a breach once the queue and DLQ are empty."""
    from datetime import timedelta

    source_id = "arn:aws:sqs:us-east-2:414279120882:tiny-workload-orders"
    source = ResourceRecord(
        resource_id=source_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="sqs:queue",
        name="tiny-workload-orders",
        config={"redrive_policy": {"deadLetterTargetArn": source_id + "-dlq"}},
        presence="present",
    )
    rule = CatalogRule(
        rule_id="SQS-S04",
        version=3,
        resource_type="sqs:queue",
        signal="ApproximateNumberOfMessagesVisible",
        statistic="Maximum",
        comparison="GreaterThanThreshold",
        threshold=0,
        dimension="QueueName={queue}",
        namespace="AWS/SQS",
    )
    end = datetime(2026, 10, 12, 19, 40, tzinfo=timezone.utc)
    earlier = end - timedelta(minutes=4)

    class Reader:
        def get_metric_data(self, account, region, resource_id, names, start, end, **kwargs):
            return {"ApproximateNumberOfMessagesVisible": [(earlier, 4.0), (end, 0.0)]}

    packed = HealthAssessment(
        source_id,
        "healthy",
        ["Queue age and depth are within bounds."],
        observed_signals={
            "ApproximateNumberOfMessagesVisible": 0.0,
            "ApproximateAgeOfOldestMessage": 0.0,
            "dlq.ApproximateNumberOfMessagesVisible": 0.0,
        },
        context={"pack_state": "healthy"},
    )
    overlaid = apply_catalog(
        packed,
        [rule.resolved({"queue": "tiny-workload-orders", "dlq": "tiny-workload-orders-dlq"})],
        source,
        Reader(),
        earlier,
        end,
    )
    assert overlaid.state == "healthy"
    assert overlaid.observed_signals["ApproximateNumberOfMessagesVisible"] == 0.0
    assert not any("SQS-S04" in reason for reason in overlaid.reasons)
    found = diagnose(Snapshot(source_id, source, overlaid, {}, [], {source_id: overlaid}, {}, [], []), end)
    assert found.problem_code == "healthy"
    assert found.health == "healthy"


def test_directory_restore_creates_a_ticket_and_api_or_table_does_not():
    """The broken directory Lambda is a ticket with Take this action. The API and table are not."""
    from sre_agent.demo_ui import Demo, render_jira
    from sre_agent.integrations.jira import parse_star_description
    from sre_agent.models import Diagnosis

    now = datetime(2026, 10, 12, 19, 0, tzinfo=timezone.utc)
    function_id = "arn:aws:lambda:us-east-2:414279120882:function:sre-dir-lab"
    function = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "last_modified": "2026-10-12T18:50:00+00:00"},
    )
    failing = HealthAssessment(
        function_id,
        "unhealthy",
        ["Error rate is 100%."],
        observed_signals={"Errors": 1.0, "Invocations": 1.0, "Throttles": 0.0, "Duration": 80.0},
    )
    failing_dx = diagnose(Snapshot("inc-dir", function, failing, {}, [], {function_id: failing}, {}, [], []), now)
    assert failing_dx.problem_code == "lambda.errors_after_deploy"
    assert issue_is_actionable(failing_dx) is True
    jira = FakeJira()
    key = jira.sync(failing_dx)
    assert key
    assert list(jira.issues) == [key]
    parsed = parse_star_description(jira.issues[key].description)
    assert parsed is not None
    assert "TABLE_NAME to sre-dir-lab" in parsed["proposed"]

    demo = Demo()
    demo.rows = [
        {
            "name": "sre-dir-lab",
            "resource_id": "arn:aws:apigateway:us-east-2::/restapis/zi21iybx7i",
            "resource_type": "apigateway:restapi",
            "health": "unhealthy",
            "problem_code": "monitoring.unsupported",
            "problem": "",
            "discovery_source": "api",
            "presence": "present",
        },
        {
            "name": "sre-dir-lab",
            "resource_id": "arn:aws:dynamodb:us-east-2:414279120882:table/sre-dir-lab",
            "resource_type": "dynamodb:table",
            "health": "healthy",
            "problem_code": "",
            "problem": "",
            "discovery_source": "api",
            "presence": "present",
        },
        {
            "name": "sre-dir-lab",
            "resource_id": function_id,
            "resource_type": "lambda:function",
            "health": "unhealthy",
            "problem_code": "lambda.errors_after_deploy",
            "problem": "Lambda errors rose around a recent deployment.",
            "discovery_source": "api",
            "presence": "present",
        },
    ]
    demo.issue_views = [{
        "key": key,
        "summary": parsed["issue"],
        "status": "To Do",
        "situation": parsed["issue"],
        "where_seen": parsed["where_seen"],
        "proposed": parsed["proposed"],
        "url": "",
        "resource_id": "arn:aws:apigateway:us-east-2::/restapis/zi21iybx7i",
        "resource_name": "sre-dir-lab",
        "resource_type": "apigateway:restapi",
    }]
    page = render_jira(demo)
    assert page.count("Take this action") == 1

    api_dx = Diagnosis(
        incident_id="inc-api",
        resource_id="arn:aws:apigateway:us-east-2::/restapis/zi21iybx7i",
        observed_problem="API Gateway is returning server errors.",
        problem_code="apigateway.errors",
        health="unhealthy",
        status="escalated",
    )
    table_dx = Diagnosis(
        incident_id="inc-table",
        resource_id="arn:aws:dynamodb:us-east-2:414279120882:table/sre-dir-lab",
        observed_problem="DynamoDB user errors are elevated.",
        problem_code="dynamodb.user_errors",
        health="unhealthy",
        status="escalated",
    )
    assert issue_is_actionable(api_dx) is False
    assert issue_is_actionable(table_dx) is False
    quiet = FakeJira()
    assert quiet.sync(api_dx) == ""
    assert quiet.sync(table_dx) == ""
    assert quiet.issues == {}


def test_wrong_table_stays_unhealthy_after_errors_leave_the_window():
    """TABLE_NAME=missing-table is unhealthy even when the Errors series has gone quiet."""
    from sre_agent.monitoring.packs import assess

    function_id = "arn:aws:lambda:us-east-2:414279120882:function:sre-dir-lab"
    now = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)
    broken = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "table_name": "missing-table", "last_modified": "2026-10-12T21:13:20+00:00"},
    )
    signals = {"Errors": 0.0, "Invocations": 1.0, "Throttles": 0.0, "Duration": 80.0}
    found = assess(broken, signals, {})
    assert found.state == "unhealthy"
    assert any("TABLE_NAME is missing-table" in reason for reason in found.reasons)
    quiet = HealthAssessment(function_id, found.state, found.reasons, observed_signals=signals)
    diagnosis = diagnose(Snapshot("inc-dir", broken, quiet, {}, [], {function_id: quiet}, {}, [], []), now)
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "lambda.errors_after_deploy"
    assert issue_is_actionable(diagnosis) is True

    restored = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "table_name": "sre-dir-lab"},
    )
    healthy = assess(restored, signals, {})
    assert healthy.state == "healthy"
    quiet_ok = HealthAssessment(function_id, "healthy", healthy.reasons, observed_signals=signals)
    resolved = diagnose(Snapshot("inc-dir", restored, quiet_ok, {}, [], {function_id: quiet_ok}, {}, [], []), now)
    assert resolved.problem_code == "healthy"
    assert issue_is_actionable(resolved) is False


def test_restored_directory_stays_healthy_while_old_errors_remain():
    """Verify and apply leaves TABLE_NAME correct. Lookback errors must not reopen the ticket."""
    from sre_agent.monitoring.packs import assess, release_restored_api, restored_directory_health

    function_id = "arn:aws:lambda:us-east-2:414279120882:function:sre-dir-lab"
    api_id = "arn:aws:apigateway:us-east-2::/restapis/sre-dir-lab"
    now = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
    restored = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "table_name": "sre-dir-lab", "last_modified": "2026-10-12T23:20:00+00:00"},
    )
    signals = {"Errors": 4.0, "Invocations": 4.0, "Throttles": 0.0, "Duration": 80.0}
    context = {"latest_errors": "4", "latest_invocations": "4", "latest_throttles": "0"}
    found = assess(restored, signals, context)
    assert found.state == "healthy"
    catalog_hit = HealthAssessment(
        function_id,
        "unhealthy",
        ["Catalog rule LAMBDA-S01 v1: Errors is 4 GreaterThanThreshold 0."],
        observed_signals=signals,
    )
    cleared = restored_directory_health(restored, catalog_hit)
    assert cleared.state == "healthy"
    diagnosis = diagnose(Snapshot("inc-dir", restored, cleared, {}, [], {function_id: cleared}, {}, [], []), now)
    assert diagnosis.health == "healthy"
    assert diagnosis.problem_code == "healthy"
    assert issue_is_actionable(diagnosis) is False

    api = ResourceRecord(
        resource_id=api_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="apigateway:restapi",
        name="sre-dir-lab",
        config={"integrations": [{"lambda_arn": function_id}]},
    )
    api_health = HealthAssessment(api_id, "unhealthy", ["API 5XX count is 4."], observed_signals={"5XXError": 4.0, "Latency": 20.0, "Count": 4.0})
    released = release_restored_api(api, api_health, [restored])
    assert released.state == "healthy"


def test_page_open_matches_jira_to_live_health(monkeypatch):
    """An unhealthy actionable Lambda opens one ticket and is shown unhealthy.

    A later read that finds that Lambda healthy leaves no open ticket.
    The API, the table, a missing log group, and a checker queue do not open one.
    """
    from sre_agent.clock import SimulatedClock
    from sre_agent.demo_ui import Demo, render, render_jira
    from sre_agent.discovery.service import Scope
    from sre_agent.integrations.jira import parse_star_description
    from sre_agent.models import Diagnosis, ScanAccess
    from sre_agent.service import CycleReport

    now = datetime(2026, 10, 12, 21, 40, tzinfo=timezone.utc)
    function_id = "arn:aws:lambda:us-east-2:414279120882:function:sre-dir-lab"
    api_id = "arn:aws:apigateway:us-east-2::/restapis/zi21iybx7i"
    table_id = "arn:aws:dynamodb:us-east-2:414279120882:table/sre-dir-lab"
    work_id = "arn:aws:sqs:us-east-2:414279120882:sre-agent-work"
    function = ResourceRecord(
        resource_id=function_id,
        account_id="414279120882",
        region="us-east-2",
        resource_type="lambda:function",
        name="sre-dir-lab",
        config={"timeout": 10, "last_modified": "2026-10-12T21:20:00+00:00"},
        health="unhealthy",
    )
    failing = HealthAssessment(
        function_id,
        "unhealthy",
        ["Error rate is 100%."],
        observed_signals={"Errors": 1.0, "Invocations": 1.0, "Throttles": 0.0, "Duration": 80.0},
    )
    failing_dx = diagnose(Snapshot("inc-dir", function, failing, {}, [], {function_id: failing}, {}, [], []), now)
    assert failing_dx.problem_code == "lambda.errors_after_deploy"
    extras = [
        Diagnosis(
            incident_id="inc-api",
            resource_id=api_id,
            observed_problem="API Gateway is returning server errors.",
            problem_code="apigateway.errors",
            health="unhealthy",
            status="escalated",
        ),
        Diagnosis(
            incident_id="inc-table",
            resource_id=table_id,
            observed_problem="DynamoDB user errors are elevated.",
            problem_code="dynamodb.user_errors",
            health="unhealthy",
            status="escalated",
        ),
        Diagnosis(
            incident_id="inc-log",
            resource_id="arn:aws:lambda:us-east-2:414279120882:function:quiet-worker",
            observed_problem="Catalog rule LAMBDA-S02 was not evaluated because the log group does not exist.",
            problem_code="catalog.symptom",
            health="degraded",
            status="open",
        ),
        Diagnosis(
            incident_id="inc-work",
            resource_id=work_id,
            observed_problem="Messages are waiting on the checker queue.",
            problem_code="sqs.backlog",
            health="unhealthy",
            status="escalated",
        ),
    ]
    jira = FakeJira()
    clock = SimulatedClock(now)

    def agent_for(record: ResourceRecord) -> OperationsAgent:
        registry = Registry()
        registry.save(record)
        for item in extras:
            registry.save(
                ResourceRecord(
                    resource_id=item.resource_id,
                    account_id="414279120882",
                    region="us-east-2",
                    resource_type="lambda:function" if "function:" in item.resource_id else "sqs:queue",
                    name=item.resource_id.rsplit(":", 1)[-1],
                    health=item.health,
                )
            )
        return OperationsAgent(Scope(), registry, clock, jira=jira)

    def report_for(record: ResourceRecord, diagnosis: Diagnosis) -> CycleReport:
        return CycleReport(
            scans=[
                ScanAccess(
                    account_id="414279120882",
                    region="us-east-2",
                    collector="lambda",
                    status="success",
                    scanned_at="2026-10-12T21:40:00+00:00",
                )
            ],
            resources=[record],
            diagnoses=[diagnosis, *extras],
        )

    broken = agent_for(function)
    cycles = iter([(report_for(function, failing_dx), broken)])

    def live_cycle(account, region, display=False):
        assert display is True
        try:
            return next(cycles)
        except StopIteration:
            healed = ResourceRecord(
                resource_id=function_id,
                account_id="414279120882",
                region="us-east-2",
                resource_type="lambda:function",
                name="sre-dir-lab",
                config={"timeout": 10, "last_modified": "2026-10-12T21:20:00+00:00"},
                health="healthy",
            )
            quiet = HealthAssessment(function_id, "healthy", ["No errors."], observed_signals={"Errors": 0.0, "Invocations": 1.0})
            healthy_dx = diagnose(Snapshot("inc-dir", healed, quiet, {}, [], {function_id: quiet}, {}, [], []), now)
            assert healthy_dx.problem_code == "healthy"
            return report_for(healed, healthy_dx), agent_for(healed)

    monkeypatch.setattr("sre_agent.demo_ui.refresh_aws_login", lambda *args, **kwargs: None)
    monkeypatch.setattr("sre_agent.demo_ui.run_live_cycle", live_cycle)
    demo = Demo()
    demo.open_health()
    page = render(demo)
    assert "2026-10-12T21:10:00" not in page
    assert "<td>sre-dir-lab</td><td>lambda:function</td><td><span class='health unhealthy'>unhealthy</span>" in page
    assert list(jira.issues) == ["SRE-1"]
    assert jira.issues["SRE-1"].status != "Done"
    parsed = parse_star_description(jira.issues["SRE-1"].description)
    assert parsed is not None
    assert "TABLE_NAME to sre-dir-lab" in parsed["proposed"]
    demo.issue_views = [{
        "key": "SRE-1",
        "summary": parsed["issue"],
        "status": "To Do",
        "situation": parsed["issue"],
        "where_seen": parsed["where_seen"],
        "proposed": parsed["proposed"],
        "url": "",
        "resource_id": function_id,
        "resource_name": "sre-dir-lab",
        "resource_type": "lambda:function",
    }]
    assert render_jira(demo).count("Take this action") == 1

    demo.open_health()
    healed_page = render(demo)
    assert "<td>sre-dir-lab</td><td>lambda:function</td><td><span class='health healthy'>healthy</span>" in healed_page
    assert jira.list_open() == []
    assert jira.issues["SRE-1"].status == "Done"
