"""The one validated repair: redrive a dead-letter queue after transient failures."""

from __future__ import annotations

from uuid import uuid4

from sre_agent.aws.errors import CloudError
from sre_agent.clock import Clock
from sre_agent.diagnosis.rules import diagnose, order_outcomes
from sre_agent.models import ActionRecord, PolicyDecision, RunbookSpec

REDRIVE = RunbookSpec(
    runbook_id="sqs.redrive_dlq",
    description="Move messages from the dead-letter queue back to the source queue after transient failures.",
    validated=True,
    mutates_configuration=False,
    permitted_types=("sqs:queue",),
    max_attempts=1,
    cooldown_seconds=3600,
    rollback_supported=False,
)

CATALOG = {REDRIVE.runbook_id: REDRIVE}


class Role:
    def __init__(self, name: str, allows_mutation: bool, runbooks: frozenset[str]) -> None:
        self.name = name
        self.allows_mutation = allows_mutation
        self.runbooks = runbooks


DIAGNOSIS_ROLE = Role("diagnosis", False, frozenset())
EXECUTOR_ROLE = Role("executor", True, frozenset({REDRIVE.runbook_id}))


class ControlledExecutor:
    def __init__(self, role: Role, tools, registry, clock: Clock, mutator_for, lab_targets: frozenset[str] | None = None, gate=None) -> None:
        if not role.allows_mutation:
            raise PermissionError("the executor requires a role that can mutate")
        self.role = role
        self.tools = tools
        self.registry = registry
        self.clock = clock
        self.mutator_for = mutator_for
        self.lab_targets = frozenset(lab_targets or ())
        self.gate = gate

    def execute(self, decision: PolicyDecision) -> ActionRecord:
        if not self.role.allows_mutation or decision.runbook_id not in self.role.runbooks:
            raise PermissionError("this role cannot run that runbook")
        if decision.outcome != "execute":
            raise PermissionError("the executor only accepts a decision the policy gate marked execute")
        started = self.clock.iso()
        action = ActionRecord(
            action_id=str(uuid4()),
            incident_id=decision.incident_id,
            runbook_id=decision.runbook_id or "",
            resource_id=decision.resource_id,
            status="refused",
            started_at=started,
            finished_at=started,
            idempotency_key=f"{decision.incident_id}|{decision.runbook_id}|{decision.resource_id}",
        )
        previous = _previous_success(self.registry, action.idempotency_key)
        if previous is not None:
            return previous
        record = self.registry.get_by_resource_id(decision.resource_id)
        if record is None:
            action.api_result = {"error": "target missing"}
            self.registry.add_action(action)
            return action
        self.tools.inspect(record.resource_id, decision.incident_id)
        fresh = diagnose(self.tools.snapshot(), self.clock.now())
        proposal = fresh.proposed_action
        if proposal is None or not proposal.automatic_eligible or proposal.runbook_id != decision.runbook_id:
            action.api_result = {"error": "live recheck did not confirm the runbook"}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        if self.gate is not None:
            fresh.incident_id = decision.incident_id
            stored = next((item for item in self.registry.incidents() if item.incident_id == decision.incident_id), None)
            if stored is not None:
                fresh.jira_issue_key = stored.jira_issue_key
                fresh.dedup_key = stored.dedup_key
            recheck = self.gate.decide(fresh)
            if recheck.outcome != "execute":
                action.api_result = {"error": "; ".join(recheck.reasons) or "policy recheck refused execution"}
                action.finished_at = self.clock.iso()
                self.registry.add_action(action)
                return action
        mutator = self.mutator_for(record.account_id)
        if mutator is None:
            action.api_result = {"error": "no separate executor credentials"}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        if mutator.__class__.__name__ != "FakeCloud" and record.resource_id not in self.lab_targets:
            action.api_result = {"error": "mutation is limited to designated lab targets"}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        if not self.registry.acquire_lock(record.resource_id, action.runbook_id, action.action_id, self.clock.iso(), 900):
            action.api_result = {"error": "resource lock is held"}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        policy = record.config.get("redrive_policy") or {}
        dlq_arn = policy.get("deadLetterTargetArn")
        samples = _samples(mutator, record.account_id, record.region, dlq_arn)
        lines: list[str] = []
        for read in self.tools.snapshot().logs.values():
            lines.extend(read.lines)
        order_ids, rejected, notes = order_outcomes(samples, lines)
        if not order_ids:
            self.registry.release_lock(record.resource_id, action.runbook_id, action.action_id)
            action.api_result = {"error": "no verified transient orders to replay", "rejected": rejected}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        try:
            running = [
                task
                for task in mutator.list_message_move_tasks(record.account_id, record.region, dlq_arn)
                if task.get("status") == "RUNNING"
            ]
            if running:
                self.registry.release_lock(record.resource_id, action.runbook_id, action.action_id)
                action.api_result = {"error": "a redrive is already running"}
                action.finished_at = self.clock.iso()
                self.registry.add_action(action)
                return action
            result = mutator.replay_orders(record.account_id, record.region, record.resource_id, dlq_arn, order_ids)
        except CloudError as exc:
            self.registry.release_lock(record.resource_id, action.runbook_id, action.action_id)
            action.status = "failed"
            action.api_result = {"error": str(exc)}
            action.finished_at = self.clock.iso()
            self.registry.add_action(action)
            return action
        action.status = "succeeded"
        action.task_id = result.get("task_id", "")
        action.order_ids = list(result.get("order_ids") or order_ids)
        action.api_result = {**dict(result), "rejected": rejected, "notes": notes}
        action.finished_at = self.clock.iso()
        self.registry.add_action(action)
        return action


def _samples(mutator, account_id: str, region: str, dlq_arn: str) -> list[dict]:
    sample = getattr(mutator, "sample_dlq_messages", None)
    if sample is None or not dlq_arn:
        return []
    return [dict(item) for item in sample(account_id, region, dlq_arn)]


def _previous_success(registry, key: str) -> ActionRecord | None:
    for existing in registry.actions():
        if existing.idempotency_key == key and existing.status == "succeeded":
            return existing
    return None
