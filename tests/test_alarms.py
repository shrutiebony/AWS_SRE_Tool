"""CloudWatch alarm state is a health input for every discovered service."""

from sre_agent.clock import SimulatedClock
from sre_agent.discovery.service import AccountTarget, Scope
from sre_agent.integrations.jira import FakeJira, JiraIssue, issue_is_actionable, parse_star_description
from sre_agent.models import ResourceRecord
from sre_agent.monitoring.alarms import alarm_dimensions, breaching_names, match_alarms
from sre_agent.registry.store import Registry
from sre_agent.service import CycleReport, OperationsAgent

ACCOUNT = "414279120882"
REGION = "us-east-2"


class _Reader:
    def __init__(self, alarms: list[dict], values: dict | None = None) -> None:
        self.alarms = alarms
        self.values = dict(values or {})
        self.calls = 0

    def query_metrics(self, region, queries, start, end):
        found = {}
        for item in queries:
            name = str(item.get("metric_name") or "")
            found[item["id"]] = [(end, float(self.values.get(name, 0.0)))]
        return found

    def list_metric_alarms(self, region):
        assert region == REGION
        self.calls += 1
        return list(self.alarms)


def _alarm(name: str, state: str, dimension: str, value: str, *, math: bool = False) -> dict:
    pair = {"Name": dimension, "Value": value}
    if not math:
        return {"name": name, "state": state, "dimensions": [pair]}
    return {
        "name": name,
        "state": state,
        "dimensions": [],
        "metrics": [{"MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": "Errors", "Dimensions": [pair]}}}],
    }


def _record(resource_id: str, resource_type: str, name: str, config: dict | None = None) -> ResourceRecord:
    return ResourceRecord(
        resource_id=resource_id,
        account_id=ACCOUNT,
        region=REGION,
        resource_type=resource_type,
        name=name,
        config=dict(config or {}),
        presence="present",
    )


def _lambda(table_name: str) -> ResourceRecord:
    return _record(
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:sre-dir-lab",
        "lambda:function",
        "sre-dir-lab",
        {"timeout": 10, "table_name": table_name},
    )


def _agent(records: list[ResourceRecord], alarms: list[dict], values: dict | None = None) -> tuple[OperationsAgent, _Reader]:
    reader = _Reader(alarms, values)
    registry = Registry()
    for record in records:
        registry.save(record)
    agent = OperationsAgent(
        Scope(accounts=[AccountTarget(ACCOUNT, [REGION], reader=reader)]),
        registry,
        SimulatedClock(),
        jira=FakeJira(),
    )
    return agent, reader


def _board(agent: OperationsAgent) -> CycleReport:
    assessments = agent.monitor()
    diagnoses = agent._display_diagnoses(assessments)
    report = CycleReport(
        resources=agent.registry.list_resources(),
        assessments=assessments,
        diagnoses=diagnoses,
    )
    agent.publish_health_board(report)
    return report


def _diagnosis(report: CycleReport, resource_id: str):
    return next(item for item in report.diagnoses if item.resource_id == resource_id)


def test_alarms_match_by_dimension_and_ignore_a_shared_name():
    function = _lambda("sre-dir-lab")
    table = _record(f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/sre-dir-lab", "dynamodb:table", "sre-dir-lab")
    queue = _record(f"arn:aws:sqs:{REGION}:{ACCOUNT}:payments", "sqs:queue", "payments")
    api = _record(f"arn:aws:apigateway:{REGION}::/restapis/abc", "apigateway:restapi", "sites")
    topic = _record(f"arn:aws:sns:{REGION}:{ACCOUNT}:demo-alerts", "sns:topic", "demo-alerts")
    instance = _record(
        f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-1",
        "ec2:instance",
        "web",
        {"instance_id": "i-1"},
    )
    alarms = [
        _alarm("sre-dir-lab-lambda-errors", "ALARM", "FunctionName", "sre-dir-lab", math=True),
        _alarm("payments-age", "OK", "QueueName", "payments"),
        _alarm("sites-5xx", "ALARM", "ApiName", "sites"),
        _alarm("demo-alerts-failed", "INSUFFICIENT_DATA", "TopicName", "demo-alerts"),
        _alarm("web-status", "ALARM", "InstanceId", "i-1"),
        _alarm("table-errors", "ALARM", "TableName", "sre-dir-lab"),
    ]
    matched = match_alarms([function, table, queue, api, topic, instance], alarms)
    assert breaching_names(matched[function.resource_id]) == ["sre-dir-lab-lambda-errors"]
    assert breaching_names(matched[table.resource_id]) == ["table-errors"]
    assert breaching_names(matched[queue.resource_id]) == []
    assert breaching_names(matched[api.resource_id]) == ["sites-5xx"]
    assert breaching_names(matched[topic.resource_id]) == []
    assert breaching_names(matched[instance.resource_id]) == ["web-status"]
    assert alarm_dimensions(alarms[0]) == [("FunctionName", "sre-dir-lab")]


def test_breaching_lambda_alarm_opens_one_restore_ticket():
    function = _lambda("missing-table")
    agent, reader = _agent([function], [_alarm("sre-dir-lab-lambda-errors", "ALARM", "FunctionName", "sre-dir-lab")])
    report = _board(agent)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    diagnosis = _diagnosis(report, function.resource_id)
    assert saved.health == "unhealthy"
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "lambda.errors_after_deploy"
    assert "TABLE_NAME is missing-table" in diagnosis.observed_problem
    assert issue_is_actionable(diagnosis) is True
    assert list(agent.jira.issues) == ["SRE-1"]
    assert agent.jira.issues["SRE-1"].status != "Done"
    parsed = parse_star_description(agent.jira.issues["SRE-1"].description)
    assert parsed is not None
    assert "TABLE_NAME to sre-dir-lab" in parsed["proposed"]
    assert reader.calls == 1


def test_ok_alarm_is_not_a_problem_and_closes_only_that_ticket():
    function = _lambda("missing-table")
    agent, reader = _agent([function], [_alarm("sre-dir-lab-lambda-errors", "ALARM", "FunctionName", "sre-dir-lab")])
    _board(agent)
    assert "SRE-1" in agent.jira.issues
    agent.jira.issues["SRE-9"] = JiraIssue(
        key="SRE-9",
        dedup_key="other",
        summary="Other queue",
        description="Unrelated backlog on a different queue.",
    )
    saved = agent.registry.get_by_resource_id(function.resource_id)
    saved.config["table_name"] = "sre-dir-lab"
    agent.registry.save(saved)
    reader.alarms = [_alarm("sre-dir-lab-lambda-errors", "OK", "FunctionName", "sre-dir-lab")]
    report = _board(agent)
    diagnosis = _diagnosis(report, function.resource_id)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    text = " ".join(saved.health_reasons) + " " + diagnosis.observed_problem
    assert saved.health == "healthy"
    assert diagnosis.problem_code == "healthy"
    assert "sre-dir-lab-lambda-errors" not in text
    assert "ALARM" not in text
    assert issue_is_actionable(diagnosis) is False
    assert agent.jira.issues["SRE-1"].status == "Done"
    assert agent.jira.issues["SRE-9"].status != "Done"
    assert [item["key"] for item in agent.jira.list_open()] == ["SRE-9"]


def test_stale_alarm_without_a_current_fault_stays_healthy_and_opens_no_ticket():
    table = _record(f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/orders", "dynamodb:table", "orders")
    queue = _record(f"arn:aws:sqs:{REGION}:{ACCOUNT}:payments", "sqs:queue", "payments")
    api = _record(f"arn:aws:apigateway:{REGION}::/restapis/abc", "apigateway:restapi", "sites")
    topic = _record(f"arn:aws:sns:{REGION}:{ACCOUNT}:demo-alerts", "sns:topic", "demo-alerts")
    alarms = [
        _alarm("orders-user-errors", "ALARM", "TableName", "orders"),
        _alarm("payments-age", "ALARM", "QueueName", "payments"),
        _alarm("sites-5xx", "ALARM", "ApiName", "sites"),
        _alarm("demo-alerts-failed", "ALARM", "TopicName", "demo-alerts"),
    ]
    agent, reader = _agent([table, queue, api, topic], alarms)
    report = _board(agent)
    assert reader.calls == 1
    for record in (table, queue, api):
        saved = agent.registry.get_by_resource_id(record.resource_id)
        diagnosis = _diagnosis(report, record.resource_id)
        text = " ".join(saved.health_reasons) + " " + diagnosis.observed_problem
        assert saved.health == "healthy"
        assert diagnosis.health == "healthy"
        assert diagnosis.problem_code == "healthy"
        assert "is in ALARM" not in text
        assert issue_is_actionable(diagnosis) is False
    topic_saved = agent.registry.get_by_resource_id(topic.resource_id)
    assert topic_saved.health != "unhealthy"
    assert not any("ALARM" in reason for reason in topic_saved.health_reasons)
    assert agent.jira.issues == {}


def test_current_table_fault_and_alarm_stay_unhealthy_without_a_ticket():
    table = _record(f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/orders", "dynamodb:table", "orders")
    agent, _reader = _agent(
        [table],
        [_alarm("orders-user-errors", "ALARM", "TableName", "orders")],
        {"UserErrors": 12},
    )
    report = _board(agent)
    diagnosis = _diagnosis(report, table.resource_id)
    saved = agent.registry.get_by_resource_id(table.resource_id)
    assert saved.health == "unhealthy"
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "cloudwatch.alarm"
    assert "orders-user-errors" in diagnosis.observed_problem
    assert any("orders-user-errors" in reason for reason in saved.health_reasons)
    assert issue_is_actionable(diagnosis) is False
    assert agent.jira.issues == {}


def test_directory_alarm_opens_a_ticket_when_the_table_is_already_correct():
    function = _lambda("sre-dir-lab")
    agent, _reader = _agent([function], [_alarm("sre-dir-lab-lambda-errors", "ALARM", "FunctionName", "sre-dir-lab")])
    report = _board(agent)
    diagnosis = _diagnosis(report, function.resource_id)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    text = " ".join(saved.health_reasons) + " " + diagnosis.observed_problem
    assert saved.health == "unhealthy"
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "lambda.errors"
    assert "sre-dir-lab-lambda-errors" in text
    assert "ALARM" in text
    assert issue_is_actionable(diagnosis) is True
    assert list(agent.jira.issues) == ["SRE-1"]
    assert agent.jira.issues["SRE-1"].status != "Done"
    parsed = parse_star_description(agent.jira.issues["SRE-1"].description)
    assert parsed is not None
    assert "TABLE_NAME to sre-dir-lab" in parsed["proposed"]


def test_current_lambda_errors_and_alarm_stay_unhealthy():
    function = _record(
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:payments-worker",
        "lambda:function",
        "payments-worker",
        {"timeout": 10},
    )
    agent, _reader = _agent(
        [function],
        [_alarm("payments-worker-errors", "ALARM", "FunctionName", "payments-worker")],
        {"Errors": 4},
    )
    report = _board(agent)
    diagnosis = _diagnosis(report, function.resource_id)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    assert saved.health == "unhealthy"
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "lambda.errors"
    assert any("payments-worker-errors" in reason for reason in saved.health_reasons)
    assert issue_is_actionable(diagnosis) is False
    assert agent.jira.issues == {}


def test_restored_directory_stays_healthy_while_the_old_alarm_is_still_alarm():
    function = _lambda("missing-table")
    agent, reader = _agent([function], [_alarm("sre-dir-lab-lambda-errors", "ALARM", "FunctionName", "sre-dir-lab")])
    _board(agent)
    assert agent.jira.issues["SRE-1"].status != "Done"
    saved = agent.registry.get_by_resource_id(function.resource_id)
    saved.config["table_name"] = "sre-dir-lab"
    agent.registry.save(saved)
    report = _board(agent)
    diagnosis = _diagnosis(report, function.resource_id)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    text = " ".join(saved.health_reasons) + " " + diagnosis.observed_problem
    assert saved.health == "unhealthy"
    assert diagnosis.health == "unhealthy"
    assert diagnosis.problem_code == "lambda.errors"
    assert "sre-dir-lab-lambda-errors" in text
    assert issue_is_actionable(diagnosis) is True
    assert agent.jira.issues["SRE-1"].status != "Done"
    assert reader.alarms[0]["state"] == "ALARM"


def test_insufficient_data_does_not_mark_a_healthy_function_unhealthy():
    function = _lambda("sre-dir-lab")
    agent, _reader = _agent([function], [_alarm("sre-dir-lab-lambda-errors", "INSUFFICIENT_DATA", "FunctionName", "sre-dir-lab")])
    report = _board(agent)
    saved = agent.registry.get_by_resource_id(function.resource_id)
    diagnosis = _diagnosis(report, function.resource_id)
    text = " ".join(saved.health_reasons) + " " + diagnosis.observed_problem
    assert saved.health == "healthy"
    assert diagnosis.problem_code == "healthy"
    assert "sre-dir-lab-lambda-errors" not in text
    assert agent.jira.issues == {}
