from datetime import timedelta

from sre_agent.diagnosis.agent import ReadOnlyCloud
from sre_agent.evidence.validator import validate_evidence
from sre_agent.executor.runbooks import DIAGNOSIS_ROLE, ControlledExecutor
from sre_agent.lab.fault_injection import run_validation
from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig, Diagnosis, Evidence, ProposedAction, ResourceRecord, RunbookSpec
from sre_agent.policy.gate import PolicyGate
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


def test_lab_validation_passes():
    results = run_validation()
    failed = [item for item in results if not item[1]]
    assert not failed, failed


def test_diagnosis_uses_the_six_tools_and_cannot_mutate():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(mode="recommend"))
    agent.inventory()
    agent.monitor()
    diagnosis = next(item for item in agent.diagnose_open() if item.problem_code == "sqs.dlq_transient")
    names = [step[0] for step in diagnosis.tool_trace]
    for tool in (
        "get_resource_configuration",
        "get_metrics",
        "query_logs",
        "get_dependencies",
        "get_recent_changes",
        "propose_runbook",
    ):
        assert tool in names
    assert diagnosis.proposed_action.automatic_eligible
    assert diagnosis.likely_causes
    record = agent.registry.get_by_resource_id(world.queue_id)
    try:
        ReadOnlyCloud(world.cloud).start_message_move_task(record.account_id, record.region, record.resource_id, world.dlq_id)
        raised = False
    except PermissionError:
        raised = True
    assert raised
    try:
        ControlledExecutor(DIAGNOSIS_ROLE, None, None, None, None)
        role_blocked = False
    except PermissionError:
        role_blocked = True
    assert role_blocked


def test_alarm_starts_an_investigation_and_does_not_skip_the_gate():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(mode="recommend"))
    agent.inventory()
    agent.monitor()
    agent.accept_event(
        {
            "detail-type": "CloudWatch Alarm State Change",
            "account": world.scope.accounts[0].account_id,
            "region": "us-east-1",
            "detail": {"alarmName": "sre-payments-age", "state": {"value": "ALARM"}, "resourceId": world.queue_id},
        }
    )
    agent.process_queue()
    assert "start_message_move_task" not in world.cloud.calls
    assert any(item.outcome == "recommend" and item.runbook_id == "sqs.redrive_dlq" for item in agent.registry.decisions())


def test_unmapped_event_does_not_change_presence():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    agent.accept_event({"detail-type": "Something Else", "detail": {}})
    agent.process_queue()
    assert agent.registry.get_by_resource_id(world.consumer_id).presence == "present"
    assert any(item.status == "unmapped" for item in agent.registry.scans())


def test_cloudtrail_refresh_confirms_deletion_and_records_the_change():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    del world.cloud.functions["payment-worker"]
    agent.accept_event(
        {
            "detail-type": "AWS API Call via CloudTrail",
            "detail": {
                "eventName": "DeleteFunction20150331",
                "resources": [{"ARN": world.consumer_id}],
            },
        }
    )
    assert agent.process_queue() == [f"refresh:deleted"]
    record = agent.registry.get_by_resource_id(world.consumer_id)
    assert record.presence == "deleted"
    assert record.recent_changes[-1]["event_name"] == "DeleteFunction20150331"


def test_reconciliation_is_scheduled_and_events_are_not_the_only_path():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    assert agent.enqueue_reconcile_if_due() is False
    world.clock.advance(1800)
    assert agent.enqueue_reconcile_if_due() is False
    world.clock.advance(1800)
    assert agent.enqueue_reconcile_if_due() is True
    assert agent.process_queue() == ["reconcile"]


def test_terraform_owned_configuration_is_not_patched_in_place():
    world = build_world()
    registry = Registry()
    record = ResourceRecord(
        resource_id=world.consumer_id,
        account_id="123456789012",
        region="us-east-1",
        resource_type="lambda:function",
        name="payment-worker",
        presence="present",
        monitoring_status="configured",
        allowed_runbooks=["lambda.set_timeout"],
        managed_by="terraform",
    )
    registry.upsert(record, "authoritative")
    registry.save(record)
    saved = registry.get_by_resource_id(world.consumer_id)
    saved.managed_by = "terraform"
    saved.allowed_runbooks = ["lambda.set_timeout"]
    registry.save(saved)
    spec = RunbookSpec(
        runbook_id="lambda.set_timeout",
        description="Change a timeout.",
        validated=True,
        mutates_configuration=True,
        permitted_types=("lambda:function",),
    )
    diagnosis = Diagnosis(
        incident_id="inc-1",
        resource_id=world.consumer_id,
        observed_problem="Duration is too close to the timeout.",
        problem_code="lambda.duration",
        health="degraded",
        evidence=[Evidence(id="ev-1", source="cloudwatch", summary="Duration is high.")],
        proposed_action=ProposedAction(
            runbook_id="lambda.set_timeout",
            target_resource_id=world.consumer_id,
            rationale="Raise the timeout.",
            evidence_ids=["ev-1"],
            automatic_eligible=True,
        ),
    )
    gate = PolicyGate(
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"lambda.set_timeout"}),
        registry,
        world.clock,
        world.scope,
        catalog={spec.runbook_id: spec},
    )
    decision = gate.decide(diagnosis)
    assert decision.outcome == "deny"
    assert "Terraform owns this setting" in decision.reasons[0]


def test_evidence_validator_rejects_an_action_without_evidence():
    diagnosis = Diagnosis(
        incident_id="inc",
        resource_id="arn",
        observed_problem="Queue is behind.",
        problem_code="sqs.backlog",
        health="unhealthy",
        proposed_action=ProposedAction("sqs.redrive_dlq", "arn", "redrive", [], True),
    )
    assert validate_evidence(diagnosis)


def test_interpreter_cannot_change_the_proposed_action():
    world = build_world()

    def mischief(diagnosis):
        diagnosis.proposed_action = None
        return "explained"

    agent = OperationsAgent(world.scope, Registry(), world.clock, interpreter=mischief)
    agent.inventory()
    agent.monitor()
    diagnosis = next(item for item in agent.diagnose_open() if item.problem_code == "sqs.dlq_transient")
    assert diagnosis.proposed_action is not None
    assert "explained" in diagnosis.uncertainty


def test_running_redrive_can_be_cancelled_when_recovery_fails():
    world = build_world()
    world.cloud.complete_moves_immediately = False
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}),
    )
    report = agent.run_cycle()
    assert report.verifications[0].cancellation == "cancelled-task"
    assert report.verifications[0].rollback == "not-possible"
    assert "cancel_message_move_task" in world.cloud.calls
    assert world.cloud.queues["payments-dlq"].messages == 2


def test_public_registry_view_has_the_required_fields():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.discovery.ingest_sns({"resource_id": world.queue_id, "owner": "payments"})
    agent.run_cycle()
    view = agent.registry.get_by_resource_id(world.queue_id).public_view()
    assert set(view) == {
        "resource_id",
        "account_id",
        "region",
        "resource_type",
        "name",
        "tags",
        "last_seen_at",
        "discovery_source",
        "monitoring_status",
        "allowed_runbooks",
    }
    assert view["allowed_runbooks"] == ["sqs.redrive_dlq"]
    assert view["resource_type"] == "sqs:queue"
