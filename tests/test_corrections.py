from datetime import datetime, timezone

from sre_agent.aws.fake import FunctionRecord
from sre_agent.clock import SimulatedClock, SystemClock
from sre_agent.events import normalize_event
from sre_agent.integrations.confluence import FakeConfluence, load_catalog
from sre_agent.integrations.jira import FakeJira
from sre_agent.lab.scenarios import ACCOUNT, REGION, build_world, lambda_arn
from sre_agent.models import ActionRecord, AgentConfig, HealthAssessment
from sre_agent.registry.store import Boto3TableAdapter, Registry
from sre_agent.service import OperationsAgent
from sre_agent.verification.verifier import RecoveryVerifier


CATALOG = """
<table>
<tr><th>rule_id</th><th>resource_type</th><th>signal</th><th>statistic</th><th>comparison</th><th>threshold</th><th>expression</th><th>log_query</th></tr>
<tr><td>lambda-error-rate</td><td>lambda:function</td><td>error_rate</td><td>Expression</td><td>GreaterThanThreshold</td><td>-1</td><td>Errors / Invocations</td><td></td></tr>
<tr><td>lambda-timeouts</td><td>lambda:function</td><td>timeout_lines</td><td>Count</td><td>GreaterThanThreshold</td><td>0</td><td></td><td>timed out</td></tr>
</table>
"""


def test_live_clock_is_current_and_simulated_time_stays_fixed():
    frozen = SimulatedClock().now()
    assert frozen == datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
    assert SimulatedClock().simulated
    assert not SystemClock().simulated
    assert abs((SystemClock().now() - datetime.now(timezone.utc)).total_seconds()) < 5


def test_live_verification_waits_instead_of_skipping_ahead():
    class LiveClock:
        simulated = False

        def __init__(self):
            self._now = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)

        def now(self):
            return self._now

        def iso(self):
            return self._now.isoformat()

        def advance(self, seconds):
            raise AssertionError("a live recovery check cannot jump the clock")

    clock = LiveClock()
    action = ActionRecord(
        action_id="a",
        incident_id="i",
        runbook_id="sqs.redrive_dlq",
        resource_id="queue",
        status="succeeded",
        started_at=clock.iso(),
        finished_at=clock.iso(),
        moved_message_ids=["msg-1"],
    )
    result = RecoveryVerifier(clock, 60).verify(
        action,
        lambda resource_id, start, end: HealthAssessment(resource_id, "healthy", ["ok"]),
        lambda action: "should-not-cancel",
        lambda action: {"msg-1"},
    )
    assert result.status == "pending"
    assert result.cancellation == "not-needed"


def test_a_second_cycle_reuses_the_open_incident_and_jira_issue():
    world = build_world()
    jira = FakeJira()
    agent = OperationsAgent(world.scope, Registry(), world.clock, jira=jira)
    first = agent.run_cycle()
    first_ids = {item.incident_id for item in agent.registry.incidents()}
    issue_count = len(jira.issues)
    second = agent.run_cycle()
    assert {item.incident_id for item in second.diagnoses} <= first_ids
    assert len(agent.registry.incidents()) == len(first_ids)
    assert len(jira.issues) == issue_count
    assert issue_count == len({item.dedup_key for item in first.diagnoses})


def test_standard_alarm_dimensions_map_to_the_queue():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    event = {
        "detail-type": "CloudWatch Alarm State Change",
        "account": ACCOUNT,
        "region": REGION,
        "resources": [f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:payments-age"],
        "detail": {
            "alarmName": "payments-age",
            "state": {"value": "ALARM"},
            "configuration": {
                "metrics": [
                    {
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/SQS",
                                "name": "ApproximateAgeOfOldestMessage",
                                "dimensions": {"QueueName": "payments"},
                            }
                        }
                    }
                ]
            },
        },
    }
    mapped = normalize_event(event, agent.registry)
    assert mapped["type"] == "investigate"
    assert mapped["resource_id"] == world.queue_id


def test_a_create_event_discovers_a_resource_that_was_not_in_the_registry():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    arn = lambda_arn("new-fn")
    world.cloud.add_function(FunctionRecord(name="new-fn", arn=arn))
    agent.accept_event(
        {
            "detail-type": "AWS API Call via CloudTrail",
            "account": ACCOUNT,
            "region": REGION,
            "detail": {"eventName": "CreateFunction20150331", "requestParameters": {"functionName": "new-fn"}},
        }
    )
    agent.process_queue()
    assert agent.registry.get_by_resource_id(arn).presence == "present"


def test_caller_mismatch_does_not_mark_known_resources_missing():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    world.cloud.claimed_account = "999999999999"
    agent.inventory()
    assert agent.registry.get_by_resource_id(world.consumer_id).presence == "present"
    assert any(item.status == "denied" and "Caller identity" in item.message for item in agent.registry.scans())


def test_explorer_stops_at_a_failed_page_and_does_not_claim_full_coverage():
    world = build_world()
    world.cloud.explorer_resources.append(
        {
            "resource_id": "arn:aws:s3:::second-archive",
            "account_id": ACCOUNT,
            "region": REGION,
            "resource_type": "s3:bucket",
            "name": "second-archive",
            "tags": {},
            "config": {},
        }
    )
    world.cloud.explorer_page_error = True
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    partial = [item for item in agent.registry.scans() if item.collector == "resource-explorer" and item.status == "partial"]
    assert partial
    assert "incomplete" in partial[-1].message
    assert agent.registry.get_by_resource_id("arn:aws:s3:::second-archive") is None


def test_registry_query_follows_the_next_page():
    class Table:
        def __init__(self):
            self.calls = 0

        def query(self, **kwargs):
            self.calls += 1
            if "ExclusiveStartKey" not in kwargs:
                return {"Items": [{"identity": "a"}], "LastEvaluatedKey": {"identity": "a"}}
            return {"Items": [{"identity": "b"}]}

    table = Table()
    items = Boto3TableAdapter(table).query_collection("resources")
    assert [item["identity"] for item in items] == ["a", "b"]
    assert table.calls == 2


def test_catalog_metric_math_and_log_query_change_health():
    world = build_world()
    confluence = FakeConfluence(CATALOG, version=3)
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=confluence)
    report = agent.run_cycle()
    consumer = next(item for item in report.assessments if item.resource_id == world.consumer_id)
    assert consumer.state == "unhealthy"
    assert any("lambda-error-rate v3" in reason or "lambda-timeouts v3" in reason for reason in consumer.reasons)
    assert load_catalog(confluence).version == 3


def test_failed_catalog_load_is_not_healthy():
    world = build_world()
    confluence = FakeConfluence(CATALOG)
    confluence.failures = 1
    agent = OperationsAgent(world.scope, Registry(), world.clock, confluence=confluence)
    report = agent.run_cycle()
    assert any(item.collector == "confluence" and item.status == "error" for item in report.scans)
    assert all(item.state != "unknown" for item in report.assessments)
    assert any(item.state == "healthy" for item in report.assessments)


def test_keyword_logs_without_message_ids_do_not_redrive():
    world = build_world()
    world.cloud.queues["payments-dlq"].message_records = []
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}),
    )
    report = agent.run_cycle()
    assert not any(item.outcome == "execute" for item in report.decisions)
    assert "sqs.dlq_unclassified" in {item.problem_code for item in report.diagnoses}


def test_a_repeated_execute_does_not_call_the_move_api_again():
    world = build_world()
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}),
    )
    report = agent.run_cycle()
    decision = next(item for item in report.decisions if item.outcome == "execute")
    again = agent.executor.execute(decision)
    assert again.action_id == report.actions[0].action_id
    assert world.cloud.calls.count("replay_orders") == 1


def test_approval_must_match_the_target_before_execution():
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
    assert not any(item.outcome == "execute" for item in first.decisions)
    diagnosis = next(item for item in first.diagnoses if item.problem_code == "sqs.dlq_transient")
    jira.grant(diagnosis.jira_issue_key, "alice", "arn:wrong", "sqs.redrive_dlq", "2026-10-06T14:00:00+00:00")
    mismatched = agent.run_cycle()
    assert not any(item.outcome == "execute" for item in mismatched.decisions)
    jira.grant(diagnosis.jira_issue_key, "alice", world.queue_id, "sqs.redrive_dlq", "2026-10-06T14:00:00+00:00")
    matched = agent.run_cycle()
    assert any(item.outcome == "execute" and item.resource_id == world.queue_id for item in matched.decisions)


def test_dynamodb_and_api_gateway_are_monitored_and_linked():
    world = build_world()
    table_id = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/payments"
    api_id = f"arn:aws:apigateway:{REGION}::/restapis/abc"
    world.cloud.tables["payments"] = {
        "resource_id": table_id,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "dynamodb:table",
        "name": "payments",
        "tags": {},
        "config": {},
    }
    world.cloud.rest_apis["abc"] = {
        "resource_id": api_id,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "apigateway:restapi",
        "name": "payments-api",
        "tags": {},
        "config": {"integrations": [{"lambda_arn": world.consumer_id, "method": "POST", "path": "/pay"}]},
    }
    now = world.clock.now()
    for name, value in (("ReadThrottleEvents", 0), ("WriteThrottleEvents", 0), ("UserErrors", 0)):
        world.cloud.set_metric(table_id, name, value, now)
    for name, value in (("5XXError", 2), ("Latency", 100), ("Count", 10)):
        world.cloud.set_metric(api_id, name, value, now)
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    report = agent.run_cycle()
    table = agent.registry.get_by_resource_id(table_id)
    api = agent.registry.get_by_resource_id(api_id)
    assert table.monitoring_status == "configured"
    assert api.monitoring_status == "configured"
    assert next(item.state for item in report.assessments if item.resource_id == table_id) == "healthy"
    assert next(item.state for item in report.assessments if item.resource_id == api_id) == "unhealthy"
    assert any(item.kind == "invokes" and item.target_id == world.consumer_id for item in agent.registry.relationships())


def test_jira_resolves_only_after_verified_recovery():
    world = build_world()
    jira = FakeJira()
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}),
        jira=jira,
    )
    agent.inventory()
    agent.monitor()
    diagnoses = agent.diagnose_open()
    decisions = agent.review(diagnoses)
    actions = agent.execute([item for item in decisions if item.outcome == "execute"])
    from sre_agent.lab.scenarios import publish_recovery

    publish_recovery(world)
    agent.verify(actions)
    issue = next(item for item in jira.issues.values() if item.dedup_key.endswith("sqs.dlq_transient"))
    assert issue.status == "Done"
    assert jira.transitioned == [issue.key]
    assert any("verified recovery" in comment for comment in issue.comments)
