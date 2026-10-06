"""Automatic repair evidence: restore, replay, verify, and the cases that must stop."""

from datetime import datetime, timedelta, timezone

from sre_agent.lab.scenarios import build_world, publish_recovery
from sre_agent.models import ActionRecord, AgentConfig, HealthAssessment
from sre_agent.registry.store import Boto3TableAdapter, Registry
from sre_agent.service import OperationsAgent
from sre_agent.verification.verifier import RecoveryVerifier
from sre_agent.integrations.jira import FakeJira


def _automatic(world, jira=None, **overrides):
    config = dict(
        mode="automatic",
        enabled_automatic_runbooks={"sqs.redrive_dlq"},
        approval_mode="lab",
        lab_targets={world.queue_id},
        require_approval=True,
        approvers=set(),
    )
    config.update(overrides)
    return OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(**config), jira=jira)


def _restore_consumer(world) -> None:
    fresh = world.clock.now() + timedelta(seconds=1)
    for name, value in (("Errors", 0), ("Invocations", 8), ("Throttles", 0), ("Duration", 100)):
        world.cloud.set_metric(world.consumer_id, name, value, fresh)
    world.clock.advance(1)


def test_restored_consumer_replays_verifies_and_resolves_jira():
    world = build_world(consumer_unhealthy=True)
    for record in world.cloud.queues["payments-dlq"].message_records:
        record["failure"] = "poison"
    jira = FakeJira()
    agent = _automatic(world, jira)
    failed = agent.run_cycle()
    assert not any(item.outcome == "execute" for item in failed.decisions)
    assert "sqs.backlog_consumer_failing" in {item.problem_code for item in failed.diagnoses}

    _restore_consumer(world)
    restored = agent.run_cycle()
    action = next(item for item in restored.actions if item.status == "succeeded")
    assert action.order_ids == ["order-1", "order-2"]
    assert world.cloud.queues["payments-dlq"].messages == 0
    assert world.cloud.calls.count("replay_orders") == 1
    decision = next(item for item in restored.decisions if item.outcome == "execute")
    duplicate = agent.executor.execute(decision)
    assert duplicate.action_id == action.action_id
    assert world.cloud.calls.count("replay_orders") == 1
    issue = jira.issues[next(item.jira_issue_key for item in restored.diagnoses if item.problem_code == "sqs.dlq_transient")]
    assert issue.status != "Done"

    publish_recovery(world)
    verified = agent.verify([action])
    assert verified[0].recovered
    assert issue.status == "Done"
    assert jira.transitioned == [issue.key]


def test_poison_malformed_and_unsampled_orders_stay_on_the_queue():
    world = build_world()
    world.cloud.set_logs(
        world.consumer_id,
        [
            '{"event":"transient_failure","order_id":"order-1","detail":"Task timed out"}',
            '{"event":"permanent_failure","order_id":"order-2","detail":"schema validation failed"}',
        ],
        world.clock.iso(),
    )
    dlq = world.cloud.queues["payments-dlq"]
    dlq.message_records[1]["failure"] = "transient"
    dlq.message_records.append({"id": "msg-3", "body": "not an order"})
    dlq.messages = 5
    report = _automatic(world).run_cycle()
    action = next(item for item in report.actions if item.status == "succeeded")
    assert action.order_ids == ["order-1"]
    assert dlq.messages == 4
    remaining = {item.get("order_id") for item in dlq.message_records}
    assert "order-1" not in remaining
    assert "order-2" in remaining
    assert any(not item.get("order_id") for item in dlq.message_records)


def test_expired_approval_is_rechecked_before_execution():
    world = build_world()
    jira = FakeJira()
    agent = _automatic(
        world,
        jira,
        approval_mode="required",
        lab_targets=set(),
        approvers={"alice"},
    )
    opened = agent.run_cycle()
    diagnosis = next(item for item in opened.diagnoses if item.problem_code == "sqs.dlq_transient")
    jira.grant(diagnosis.jira_issue_key, "alice", world.queue_id, "sqs.redrive_dlq", "2026-10-06T14:00:00+00:00")
    approved = agent.diagnose_open()
    decision = next(item for item in agent.review(approved) if item.outcome == "execute")
    jira.grant(diagnosis.jira_issue_key, "alice", world.queue_id, "sqs.redrive_dlq", "2026-10-06T12:00:00+00:00")
    action = agent.executor.execute(decision)
    assert action.status == "refused"
    assert "expired" in action.api_result["error"]
    assert "replay_orders" not in world.cloud.calls


def test_denied_replay_and_failed_recovery_do_not_resolve():
    world = build_world()
    world.cloud.deny.add("replay_orders")
    jira = FakeJira()
    denied = _automatic(world, jira).run_cycle()
    assert denied.actions[0].status == "failed"
    assert world.cloud.queues["payments-dlq"].messages == 2
    assert jira.transitioned == []

    world = build_world()
    earlier = (world.clock.now() - timedelta(hours=1)).isoformat()
    world.cloud.lab_mark_processed(["order-1", "order-2"], at=earlier)
    jira = FakeJira()
    agent = _automatic(world, jira)
    report = agent.run_cycle()
    assert report.actions[0].status == "succeeded"
    assert not report.verifications[0].recovered
    assert any("not processed after the repair" in reason for reason in report.verifications[0].reasons)
    issue = next(iter(jira.issues.values()))
    assert issue.status != "Done"
    assert jira.transitioned == []


def test_jira_sync_retries_then_succeeds():
    world = build_world()
    jira = FakeJira()
    jira.failures = 2
    agent = OperationsAgent(world.scope, Registry(), world.clock, jira=jira)
    report = agent.run_cycle()
    assert any(item.jira_issue_key for item in report.diagnoses)
    assert not any(item.collector == "jira" and item.status == "error" for item in report.scans)


def test_investigation_uses_the_repair_queue():
    world = build_world()
    sent = []
    agent = _automatic(world)
    agent.repair_queue = sent.append
    agent.inventory()
    agent.monitor()
    agent.queue.send({"type": "investigate", "resource_id": world.queue_id})
    agent.queue.send({"type": "monitor"})
    handled = agent.process_queue()
    assert handled[0] == "investigate"
    assert "monitor" in handled
    assert sent
    assert {item["type"] for item in sent} == {"repair"}
    assert "replay_orders" not in world.cloud.calls


def test_adapter_lock_handles_concurrency_expiry_and_duplicate_delivery():
    class ConditionalFailure(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    class EnforcingTable:
        def __init__(self):
            self.items = {}
            self.expressions = []

        def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None, **kwargs):
            del kwargs
            current = self.items.get(Item["identity"])
            if ConditionExpression:
                self.expressions.append(ConditionExpression)
                names = ExpressionAttributeNames or {}
                values = ExpressionAttributeValues or {}
                owner_key = names.get("#owner", "owner")
                expires_key = names.get("#expires", "expires_at")
                if current is not None and owner_key in current:
                    owner = current.get(owner_key) or ""
                    expires = current.get(expires_key) or ""
                    allowed = (
                        owner == values.get(":empty")
                        or owner == values.get(":owner")
                        or (expires != "" and expires <= values.get(":now", ""))
                    )
                    if not allowed:
                        raise ConditionalFailure()
            self.items[Item["identity"]] = dict(Item)

        def update_item(self, Key, ConditionExpression, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            del ConditionExpression, UpdateExpression
            current = self.items.get(Key["identity"])
            owner_key = ExpressionAttributeNames["#owner"]
            if current is None or current.get(owner_key) != ExpressionAttributeValues[":owner"]:
                raise ConditionalFailure()
            current[owner_key] = ExpressionAttributeValues[":empty"]
            current["expires_at"] = ExpressionAttributeValues[":empty_time"]

    raw = EnforcingTable()
    first = Registry(Boto3TableAdapter(raw))
    second = Registry(Boto3TableAdapter(raw))
    assert first.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:00:00+00:00", 900)
    assert not second.acquire_lock("queue", "sqs.redrive_dlq", "worker-b", "2026-10-06T13:01:00+00:00", 900)
    assert first.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:02:00+00:00", 900)
    assert second.acquire_lock("queue", "sqs.redrive_dlq", "worker-b", "2026-10-06T13:17:00+00:00", 900)
    second.release_lock("queue", "sqs.redrive_dlq", "worker-a")
    assert not first.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:18:00+00:00", 900)
    second.release_lock("queue", "sqs.redrive_dlq", "worker-b")
    assert first.acquire_lock("queue", "sqs.redrive_dlq", "worker-a", "2026-10-06T13:18:00+00:00", 900)
    assert "#expires <= :now" in raw.expressions[0]
    assert "#owner = :owner" in raw.expressions[0]


def test_verification_times_out_on_a_live_clock():
    class ManualClock:
        simulated = False

        def __init__(self):
            self._now = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)

        def now(self):
            return self._now

        def iso(self):
            return self._now.isoformat()

        def advance(self, seconds):
            self._now += timedelta(seconds=seconds)

    clock = ManualClock()
    verifier = RecoveryVerifier(clock, 60, timeout_seconds=120)
    action = ActionRecord(
        action_id="act",
        incident_id="inc",
        runbook_id="sqs.redrive_dlq",
        resource_id="queue",
        status="succeeded",
        started_at=clock.iso(),
        finished_at=clock.iso(),
        order_ids=["order-1"],
    )

    def unhealthy(resource_id, start, end):
        del resource_id, start, end
        return HealthAssessment("queue", "unhealthy", ["still behind"], [], {}, {})

    clock.advance(30)
    pending = verifier.verify(action, unhealthy, lambda item: "not-needed", lambda item: set())
    assert pending.status == "pending"
    clock.advance(100)
    timed_out = verifier.verify(action, unhealthy, lambda item: "already-finished", lambda item: set())
    assert not timed_out.recovered
    assert timed_out.reasons[0].startswith("Verification timed out")
