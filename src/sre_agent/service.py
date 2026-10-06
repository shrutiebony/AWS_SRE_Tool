"""Operations cycle: inventory, registry, monitoring, diagnosis, one gated repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sre_agent.clock import Clock, parse_time
from sre_agent.diagnosis.agent import DiagnosisAgent, DiagnosticTools
from sre_agent.discovery.service import DiscoveryReconciliationService, Scope, _reader_for
from sre_agent.events import InMemoryQueue, normalize_event
from sre_agent.executor.runbooks import EXECUTOR_ROLE, ControlledExecutor
from sre_agent.lessons.store import LessonStore
from sre_agent.models import (
    ActionRecord,
    AgentConfig,
    Diagnosis,
    Escalation,
    HealthAssessment,
    Lesson,
    Outcome,
    PolicyDecision,
    ResourceRecord,
    ScanAccess,
    VerificationResult,
)
from sre_agent.integrations.confluence import ConfluenceError, load_catalog
from sre_agent.monitoring.catalog import apply_catalog
from sre_agent.monitoring.packs import SUPPORTED, runbooks_for
from sre_agent.monitoring.telemetry import sample
from sre_agent.policy.gate import PolicyGate
from sre_agent.registry.store import Registry
from sre_agent.verification.verifier import RecoveryVerifier


@dataclass
class CycleReport:
    scans: list[ScanAccess] = field(default_factory=list)
    resources: list[ResourceRecord] = field(default_factory=list)
    assessments: list[HealthAssessment] = field(default_factory=list)
    diagnoses: list[Diagnosis] = field(default_factory=list)
    decisions: list[PolicyDecision] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)


class OperationsAgent:
    def __init__(
        self,
        scope: Scope,
        registry: Registry,
        clock: Clock,
        config: AgentConfig | None = None,
        queue: InMemoryQueue | None = None,
        mutator_for=None,
        interpreter=None,
        confluence=None,
        jira=None,
        repair_queue=None,
    ) -> None:
        self.scope = scope
        self.registry = registry
        self.clock = clock
        self.config = config or AgentConfig()
        self.queue = queue or InMemoryQueue()
        self.confluence = confluence
        self.jira = jira
        self.repair_queue = repair_queue
        self.catalog_status = "not-configured"
        self.catalog_rules = []
        self.discovery = DiscoveryReconciliationService(scope, registry, clock)
        self.lessons = LessonStore(registry)
        self.tools = DiagnosticTools(scope, registry, clock, self.config.metric_lookback_seconds)
        self.agent = DiagnosisAgent(self.tools, interpreter)
        self.gate = PolicyGate(self.config, registry, clock, scope, jira=jira)
        self.mutator_for = mutator_for or (lambda account_id: _simulator_mutator(scope, account_id))
        self.executor = ControlledExecutor(
            EXECUTOR_ROLE, self.tools, registry, clock, self.mutator_for, self.config.lab_targets, gate=self.gate
        )
        self.verifier = RecoveryVerifier(clock, self.config.verify_window_seconds, self.config.verify_timeout_seconds)

    def run_cycle(self) -> CycleReport:
        self.inventory()
        assessments = self.monitor()
        diagnoses = self.diagnose_open()
        decisions = self.review(diagnoses)
        actions = self.execute([item for item in decisions if item.outcome == "execute"])
        verifications = self.verify(actions)
        self._note_agent_health()
        stored = {item.incident_id: item for item in self.registry.incidents()}
        diagnoses = [stored.get(item.incident_id, item) for item in diagnoses]
        return CycleReport(
            scans=self.registry.scans(),
            resources=self.registry.list_resources(),
            assessments=assessments,
            diagnoses=diagnoses,
            decisions=decisions,
            actions=actions,
            verifications=verifications,
            escalations=self.registry.escalations(),
            outcomes=self.registry.outcomes(),
        )

    def inventory(self):
        return self.discovery.run()

    def monitor(self) -> list[HealthAssessment]:
        self.attach_packs()
        self._load_catalog()
        start, end = self._lookback()
        assessments = []
        for record in self.registry.list_resources():
            if record.presence == "present" and record.monitoring_status == "configured":
                assessments.append(self._apply_sample(record, start, end, False))
        return assessments

    def diagnose_open(self) -> list[Diagnosis]:
        diagnoses = []
        for resource_id in self._targets():
            diagnosis = self.agent.investigate(resource_id)
            self._reuse_incident(diagnosis)
            self._sync_jira(diagnosis)
            decision = self.gate.decide(diagnosis)
            self._record(diagnosis, decision)
            diagnoses.append(diagnosis)
        return diagnoses

    def _dispatch(self, diagnoses: list[Diagnosis]) -> list[ActionRecord]:
        actions = self.execute([item for item in self.review(diagnoses) if item.outcome == "execute"])
        if actions:
            self.verify(actions)
        return actions

    def review(self, diagnoses: list[Diagnosis]) -> list[PolicyDecision]:
        by_incident = {item.incident_id: item for item in self.registry.decisions()}
        return [by_incident[item.incident_id] for item in diagnoses if item.incident_id in by_incident]

    def execute(self, decisions: list[PolicyDecision]) -> list[ActionRecord]:
        if self.repair_queue is not None:
            for decision in decisions:
                self.repair_queue(
                    {
                        "type": "repair",
                        "incident_id": decision.incident_id,
                        "runbook_id": decision.runbook_id or "",
                        "resource_id": decision.resource_id,
                    }
                )
            return []
        return [self.executor.execute(decision) for decision in decisions]

    def verify_due(self) -> list[VerificationResult]:
        return self.verify([])

    def verify(self, actions: list[ActionRecord]) -> list[VerificationResult]:
        results = []
        for action in [*self._due_actions(), *actions]:
            diagnosis = next(item for item in self.registry.incidents() if item.incident_id == action.incident_id)
            if action.status != "succeeded":
                result = VerificationResult(
                    action_id=action.action_id,
                    incident_id=action.incident_id,
                    resource_id=action.resource_id,
                    recovered=False,
                    status="escalated",
                    reasons=["The repair command did not succeed, so recovery was not checked."],
                    rollback="not-needed",
                    cancellation="not-needed",
                )
                self.registry.release_lock(action.resource_id, action.runbook_id, action.action_id)
                self._close_failed_verification(diagnosis, action, result, block=False)
            else:
                result = self.verifier.verify(action, self._reassess, self._stop_move, self._processed)
                if result.status == "pending":
                    self.registry.add_verification(result)
                    self.registry.backend.save("due_verifications", action.action_id, action)
                    results.append(result)
                    continue
                self.registry.release_lock(action.resource_id, action.runbook_id, action.action_id)
                self._close_failed_verification(diagnosis, action, result, block=not result.recovered)
            results.append(result)
        return results

    def accept_event(self, event: dict) -> None:
        item = normalize_event(event, self.registry)
        if item is None:
            return
        if item.get("type") == "unmapped":
            self.registry.add_scan(
                ScanAccess(
                    account_id=str(event.get("account", "*")),
                    region=str(event.get("region", "*")),
                    collector="event",
                    status="unmapped",
                    scanned_at=self.clock.iso(),
                    message=item.get("summary", "Unmapped event."),
                )
            )
            return
        self.queue.send(item)

    def process_queue(self) -> list[str]:
        handled = []
        while True:
            item = self.queue.receive()
            if item is None:
                break
            handled.append(self._handle(item))
        if handled:
            self.verify_due()
        return handled

    def enqueue_reconcile_if_due(self) -> bool:
        if any(item.get("type") == "reconcile" for item in self.queue.pending()):
            return False
        if not self.reconcile_due():
            return False
        self.queue.send({"type": "reconcile"})
        return True

    def reconcile_due(self) -> bool:
        stamped = [item for item in self.registry.scans() if item.collector == "reconciliation"]
        if not stamped:
            return True
        latest = max(parse_time(item.scanned_at) for item in stamped)
        return self.clock.now() - latest >= timedelta(seconds=self.config.reconcile_interval_seconds)

    def attach_packs(self) -> None:
        for record in self.registry.list_resources():
            if record.presence != "present":
                record.monitoring_status = "not-configured"
                record.allowed_runbooks = []
            elif runbooks_for(record) or record.resource_type in SUPPORTED:
                record.monitoring_status = "configured"
                record.allowed_runbooks = runbooks_for(record)
            else:
                record.monitoring_status = "unsupported"
                record.allowed_runbooks = []
            self.registry.save(record)

    def _targets(self) -> list[str]:
        targets = []
        for record in self.registry.list_resources():
            if record.presence == "deleted" or record.monitoring_status == "unsupported":
                continue
            if record.presence == "uncertain":
                targets.append(record.resource_id)
            elif record.monitoring_status == "configured" and record.health in {"degraded", "unhealthy", "unknown"}:
                targets.append(record.resource_id)
        return targets

    def _handle(self, item: dict) -> str:
        kind = item.get("type")
        if kind == "reconcile":
            self.inventory()
            self.monitor()
            self._dispatch(self.diagnose_open())
            self.verify_due()
            return "reconcile"
        if kind == "monitor":
            self.monitor()
            self._dispatch(self.diagnose_open())
            self.verify_due()
            return "monitor"
        if kind == "verify":
            self.verify_due()
            return "verify"
        if kind == "refresh":
            resource_id = item["resource_id"]
            self.registry.note_change(resource_id, item.get("event_name", "change"), item.get("summary", ""), self.clock.iso())
            outcome = self.discovery.refresh_one(resource_id)
            if outcome == "present":
                self.attach_packs()
                record = self.registry.get_by_resource_id(resource_id)
                if record is not None:
                    start, end = self._lookback()
                    self._apply_sample(record, start, end, False)
                    record = self.registry.get_by_resource_id(resource_id)
                    if record is not None and record.health in {"degraded", "unhealthy", "unknown"}:
                        self._investigate_one(resource_id)
            return f"refresh:{outcome}"
        if kind == "investigate":
            self._investigate_one(item["resource_id"])
            return "investigate"
        return "ignored"

    def _investigate_one(self, resource_id: str) -> None:
        self.attach_packs()
        record = self.registry.get_by_resource_id(resource_id)
        if record is not None and record.presence == "present" and record.monitoring_status == "configured":
            start, end = self._lookback()
            self._apply_sample(record, start, end, False)
        diagnosis = self.agent.investigate(resource_id)
        self._reuse_incident(diagnosis)
        self._sync_jira(diagnosis)
        decision = self.gate.decide(diagnosis)
        self._record(diagnosis, decision)
        if decision.outcome == "execute":
            actions = self.execute([decision])
            if actions:
                self.verify(actions)

    def _record(self, diagnosis: Diagnosis, decision: PolicyDecision) -> None:
        self._reuse_incident(diagnosis, decision)
        if decision.outcome == "recommend":
            diagnosis.status = "recommended"
        elif decision.outcome == "deny":
            diagnosis.status = "escalated"
        elif decision.outcome == "execute":
            diagnosis.status = "executing"
        elif diagnosis.status == "open":
            diagnosis.status = "unresolved"
        self._sync_jira(diagnosis)
        self.registry.add_incident(diagnosis)
        self.registry.add_decision(decision)
        if diagnosis.status == "escalated":
            self.registry.add_escalation(
                Escalation(
                    incident_id=diagnosis.incident_id,
                    resource_id=diagnosis.resource_id,
                    reason=diagnosis.observed_problem,
                    created_at=self.clock.iso(),
                    runbook_id=decision.runbook_id or "",
                )
            )
        self.registry.add_outcome(
            Outcome(
                incident_id=diagnosis.incident_id,
                resource_id=diagnosis.resource_id,
                status=diagnosis.status,
                summary=diagnosis.observed_problem,
                created_at=self.clock.iso(),
            )
        )

    def _close_failed_verification(self, diagnosis: Diagnosis, action: ActionRecord, result: VerificationResult, block: bool) -> None:
        self.registry.add_verification(result)
        if result.recovered:
            diagnosis.status = "recovered"
            self.lessons.record(
                Lesson(
                    symptom_key=diagnosis.problem_code,
                    runbook_id=action.runbook_id,
                    resource_id=action.resource_id,
                    outcome="recovered",
                    summary=result.reasons[0],
                    created_at=self.clock.iso(),
                )
            )
        else:
            diagnosis.status = "escalated"
            if block:
                self.registry.block_runbook(action.resource_id, action.runbook_id, "recovery failed")
                self.lessons.record(
                    Lesson(
                        symptom_key=diagnosis.problem_code,
                        runbook_id=action.runbook_id,
                        resource_id=action.resource_id,
                        outcome="failed",
                        summary=result.reasons[0],
                        created_at=self.clock.iso(),
                    )
                )
            self.registry.add_escalation(
                Escalation(
                    incident_id=diagnosis.incident_id,
                    resource_id=diagnosis.resource_id,
                    reason=result.reasons[0],
                    created_at=self.clock.iso(),
                    runbook_id=action.runbook_id,
                )
            )
        self._sync_jira(diagnosis)
        self.registry.add_incident(diagnosis)
        self.registry.add_outcome(
            Outcome(
                incident_id=diagnosis.incident_id,
                resource_id=diagnosis.resource_id,
                status=diagnosis.status,
                summary=result.reasons[0],
                created_at=self.clock.iso(),
            )
        )

    def _stop_move(self, action: ActionRecord) -> str:
        record = self.registry.get_by_resource_id(action.resource_id)
        if record is None:
            return "not-cancelled"
        mutator = self.mutator_for(record.account_id)
        if mutator is None:
            return "not-cancelled"
        policy = record.config.get("redrive_policy") or {}
        dlq_arn = policy.get("deadLetterTargetArn", "")
        status = action.api_result.get("status")
        if dlq_arn:
            tasks = mutator.list_message_move_tasks(record.account_id, record.region, dlq_arn)
            current = next((item for item in tasks if item.get("task_id") == action.task_id), None)
            if current is not None:
                status = current.get("status")
        if status == "RUNNING":
            mutator.cancel_message_move_task(record.account_id, record.region, action.task_id)
            return "cancelled-task"
        if status == "COMPLETED":
            return "already-finished"
        return "not-needed"

    def _processed(self, action: ActionRecord) -> set[str]:
        record = self.registry.get_by_resource_id(action.resource_id)
        if record is None:
            return set()
        reader = _reader_for(self.scope, record.account_id)
        sample = getattr(reader, "processed_order_ids", None)
        if sample is None:
            return set()
        return set(sample(record.account_id, record.region, action.order_ids, action.finished_at))

    def _reuse_incident(self, diagnosis: Diagnosis, decision: PolicyDecision | None = None) -> None:
        record = self.registry.get_by_resource_id(diagnosis.resource_id)
        if record is None:
            return
        key = f"{record.account_id}|{record.region}|{record.resource_id}|{diagnosis.problem_code}"
        diagnosis.dedup_key = key
        open_states = {"open", "recommended", "executing", "escalated", "unresolved"}
        for existing in self.registry.incidents():
            if existing.dedup_key == key and existing.status in open_states and existing.incident_id != diagnosis.incident_id:
                diagnosis.incident_id = existing.incident_id
                if decision is not None:
                    decision.incident_id = existing.incident_id
                if existing.jira_issue_key:
                    diagnosis.jira_issue_key = existing.jira_issue_key
                return

    def _sync_jira(self, diagnosis: Diagnosis) -> None:
        if self.jira is None or not diagnosis.dedup_key:
            return
        last = "Jira synchronization failed."
        for _ in range(3):
            try:
                diagnosis.jira_issue_key = self.jira.sync(diagnosis)
                return
            except Exception as exc:
                last = str(exc)
        self.registry.add_scan(
            ScanAccess(
                account_id="*",
                region="*",
                collector="jira",
                status="error",
                scanned_at=self.clock.iso(),
                message=f"Jira synchronization failed after retries. {last}",
            )
        )

    def _load_catalog(self) -> None:
        if self.confluence is None:
            self.catalog_status = "not-configured"
            self.catalog_rules = []
            return
        try:
            catalog = load_catalog(self.confluence)
        except (ConfluenceError, ValueError) as exc:
            self.catalog_status = "failed"
            self.catalog_rules = []
            self.registry.add_scan(
                ScanAccess(
                    account_id="*",
                    region="*",
                    collector="confluence",
                    status="error",
                    scanned_at=self.clock.iso(),
                    message=f"Catalog load failed. Checks are unknown, not healthy. {exc}",
                )
            )
            return
        self.catalog_status = "loaded"
        self.catalog_rules = list(catalog.rules)
        self.registry.add_scan(
            ScanAccess(
                account_id="*",
                region="*",
                collector="confluence",
                status="success",
                scanned_at=self.clock.iso(),
                resource_count=len(catalog.rules),
                message=f"Loaded Confluence catalog version {catalog.version}.",
            )
        )

    def _due_actions(self) -> list[ActionRecord]:
        if getattr(self.clock, "simulated", True):
            return []
        ready = []
        for action in self.registry.backend.load_all("due_verifications"):
            if not isinstance(action, ActionRecord):
                continue
            latest = [item for item in self.registry.verifications() if item.action_id == action.action_id]
            if not latest or latest[-1].status != "pending":
                continue
            elapsed = (self.clock.now() - parse_time(action.finished_at)).total_seconds()
            if elapsed >= self.config.verify_window_seconds or elapsed >= self.config.verify_timeout_seconds:
                ready.append(action)
        return ready

    def _note_agent_health(self) -> None:
        depth = len(self.queue.pending())
        if depth:
            self.registry.add_scan(
                ScanAccess(
                    account_id="*",
                    region="*",
                    collector="agent-queue",
                    status="degraded",
                    scanned_at=self.clock.iso(),
                    resource_count=depth,
                    message=f"Work queue has {depth} messages waiting.",
                )
            )
        stamped = [item for item in self.registry.scans() if item.collector == "reconciliation" and item.status == "success"]
        if stamped:
            latest = max(parse_time(item.scanned_at) for item in stamped)
            limit = timedelta(seconds=self.config.reconcile_interval_seconds * 2)
            if self.clock.now() - latest >= limit:
                self.registry.add_scan(
                    ScanAccess(
                        account_id="*",
                        region="*",
                        collector="agent-scan",
                        status="error",
                        scanned_at=self.clock.iso(),
                        message="The last successful reconciliation is stale.",
                    )
                )

    def _reassess(self, resource_id: str, start, end) -> HealthAssessment:
        record = self.registry.get_by_resource_id(resource_id)
        return self._apply_sample(record, start, end, True)

    def _apply_sample(self, record: ResourceRecord, start, end, start_exclusive: bool) -> HealthAssessment:
        reader = _reader_for(self.scope, record.account_id)
        assessment = sample(record, reader, start, end, start_exclusive)
        if self.catalog_status == "failed":
            assessment = HealthAssessment(
                record.resource_id,
                "unknown",
                ["The Confluence catalog failed to load. Missing rules are unknown, not healthy.", *assessment.reasons],
                list(assessment.missing_signals),
                dict(assessment.observed_signals),
                dict(assessment.context),
            )
        elif self.catalog_rules and reader is not None:
            assessment = apply_catalog(assessment, self.catalog_rules, record, reader, start, end)
        record.health = assessment.state
        record.health_reasons = list(assessment.reasons)
        record.missing_signals = list(assessment.missing_signals)
        record.observed_signals = {key: float(value) for key, value in assessment.observed_signals.items()}
        record.signal_context = dict(assessment.context)
        self.registry.save(record)
        return assessment

    def _lookback(self):
        end = self.clock.now()
        return end - timedelta(seconds=self.config.metric_lookback_seconds), end


def _simulator_mutator(scope: Scope, account_id: str):
    """The in-memory lab may repair itself. A live reader is never used as the mutation client."""
    reader = _reader_for(scope, account_id)
    if reader is not None and reader.__class__.__name__ == "FakeCloud":
        return reader
    return None
