"""The six diagnosis tools and the diagnosis agent. Tools can read. They cannot start a repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from uuid import uuid4

from sre_agent.aws.errors import AccessDenied, PartialRead
from sre_agent.clock import Clock, parse_time
from sre_agent.discovery.service import Scope, _reader_for
from sre_agent.evidence.validator import validate_evidence
from sre_agent.models import Diagnosis, HealthAssessment, Relationship, ResourceRecord
from sre_agent.monitoring.catalog import apply_catalog
from sre_agent.monitoring.telemetry import sample
from sre_agent.registry.store import Registry


@dataclass
class LogRead:
    status: str
    lines: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class Snapshot:
    incident_id: str
    record: ResourceRecord | None
    assessment: HealthAssessment
    logs: dict[str, LogRead]
    dependencies: list[Relationship]
    assessments: dict[str, HealthAssessment]
    neighbors: dict[str, ResourceRecord]
    changes: list[dict]
    lessons: list[Lesson]
    dlq_messages: list[dict] = field(default_factory=list)


class ReadOnlyCloud:
    """Stops the diagnosis path from calling a repair API on a shared fake or session."""

    _blocked = {"start_message_move_task", "cancel_message_move_task", "replay_orders"}

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in self._blocked:
            raise PermissionError("diagnosis tools are read-only")
        return getattr(self._inner, name)


class DiagnosticTools:
    def __init__(self, scope: Scope, registry: Registry, clock: Clock, lookback_seconds: int) -> None:
        self.scope = scope
        self.registry = registry
        self.clock = clock
        self.lookback_seconds = lookback_seconds
        self.trace: list[list[str]] = []
        self._draft: dict = {}

    def inspect(self, resource_id: str, incident_id: str) -> Snapshot:
        self.trace = []
        self._draft = {"incident_id": incident_id, "configs": {}, "assessments": {}, "logs": {}, "dependencies": [], "changes": []}
        self.get_resource_configuration(resource_id)
        record = self.registry.get_by_resource_id(resource_id)
        self._draft["dlq_messages"] = self._sample_messages(record)
        self.get_metrics(resource_id)
        self.query_logs(resource_id)
        self.get_dependencies(resource_id)
        neighbor_ids = set()
        for item in self._draft["dependencies"]:
            neighbor_ids.add(item.source_id)
            neighbor_ids.add(item.target_id)
        neighbor_ids.discard(resource_id)
        for neighbor_id in sorted(neighbor_ids):
            if self.registry.get_by_resource_id(neighbor_id) is not None:
                self.get_metrics(neighbor_id)
                self.query_logs(neighbor_id)
        self.get_recent_changes(resource_id)
        return self.snapshot()

    def get_resource_configuration(self, resource_id: str):
        self.trace.append(["get_resource_configuration", resource_id])
        record = self.registry.get_by_resource_id(resource_id)
        self._draft["configs"][resource_id] = record
        return record.public_view() if record else None

    def get_metrics(self, resource_id: str, time_window: tuple[datetime, datetime] | None = None):
        self.trace.append(["get_metrics", resource_id])
        record = self.registry.get_by_resource_id(resource_id)
        if record is None:
            assessment = HealthAssessment(resource_id, "unknown", ["Resource is not in the registry."])
        else:
            start, end = time_window or self._window()
            assessment = sample(record, self._reader(record), start, end)
            rules = getattr(self, "catalog_rules", [])
            if rules:
                assessment = apply_catalog(assessment, rules, record, self._reader(record), start, end)
        self._draft["assessments"][resource_id] = assessment
        return {
            "state": assessment.state,
            "signals": assessment.observed_signals,
            "missing": assessment.missing_signals,
            "reasons": assessment.reasons,
        }

    def query_logs(self, resource_id: str, time_window: tuple[datetime, datetime] | None = None):
        self.trace.append(["query_logs", resource_id])
        record = self.registry.get_by_resource_id(resource_id)
        read = self._read_logs(record, time_window or self._window())
        self._draft["logs"][resource_id] = read
        return {"status": read.status, "lines": list(read.lines)}

    def get_dependencies(self, resource_id: str):
        self.trace.append(["get_dependencies", resource_id])
        relationships = self.registry.dependencies(resource_id)
        self._draft["dependencies"] = relationships
        return [
            {
                "source_id": item.source_id,
                "target_id": item.target_id,
                "kind": item.kind,
                "evidence_source": item.evidence_source,
                "evidence": dict(item.evidence),
            }
            for item in relationships
        ]

    def get_recent_changes(self, resource_id: str, time_window: tuple[datetime, datetime] | None = None):
        self.trace.append(["get_recent_changes", resource_id])
        record = self.registry.get_by_resource_id(resource_id)
        start, end = time_window or self._window()
        changes = []
        if record is not None:
            for change in record.recent_changes:
                try:
                    stamp = parse_time(change.get("at", ""))
                except ValueError:
                    continue
                if start <= stamp <= end:
                    changes.append(dict(change))
        self._draft["changes"] = changes
        return changes

    def propose_runbook(self, incident_id: str):
        from sre_agent.diagnosis.rules import diagnose

        self.trace.append(["propose_runbook", incident_id])
        diagnosis = diagnose(self.snapshot(), self.clock.now())
        diagnosis.tool_trace = [list(item) for item in self.trace]
        return diagnosis

    def snapshot(self) -> Snapshot:
        incident_id = self._draft.get("incident_id", "")
        configs = self._draft.get("configs", {})
        resource_id = next(iter(configs), "")
        record = configs.get(resource_id)
        assessments = self._draft.get("assessments", {})
        assessment = assessments.get(resource_id) or HealthAssessment(resource_id, "unknown", ["No metrics were read."])
        neighbors = {}
        for item in self._draft.get("dependencies", []):
            for related_id in (item.source_id, item.target_id):
                related = self.registry.get_by_resource_id(related_id)
                if related is not None and related.resource_id != resource_id:
                    neighbors[related.resource_id] = related
        return Snapshot(
            incident_id=incident_id,
            record=record,
            assessment=assessment,
            logs=dict(self._draft.get("logs", {})),
            dependencies=list(self._draft.get("dependencies", [])),
            assessments=dict(assessments),
            neighbors=neighbors,
            changes=list(self._draft.get("changes", [])),
            lessons=self.registry.backend.load_all("lessons"),
            dlq_messages=list(self._draft.get("dlq_messages", [])),
        )

    def _window(self) -> tuple[datetime, datetime]:
        end = self.clock.now()
        return end - timedelta(seconds=self.lookback_seconds), end

    def _reader(self, record: ResourceRecord):
        reader = _reader_for(self.scope, record.account_id)
        if reader is None:
            return None
        return ReadOnlyCloud(reader)

    def _read_logs(self, record: ResourceRecord | None, window: tuple[datetime, datetime]) -> LogRead:
        if record is None:
            return LogRead(status="missing")
        reader = self._reader(record)
        if reader is None:
            return LogRead(status="denied", message="No credentials for this account.")
        try:
            events = reader.filter_logs(record.account_id, record.region, record.resource_id, window[0], window[1])
        except PartialRead as exc:
            return LogRead(
                status="partial",
                lines=[str(item.get("message", "")) for item in exc.items if isinstance(item, dict)],
                message=f"Log coverage is incomplete. {exc}",
            )
        except AccessDenied as exc:
            return LogRead(status="denied", message=str(exc))
        return LogRead(status="ok", lines=[str(item.get("message", "")) for item in events])

    def _sample_messages(self, record: ResourceRecord | None) -> list[dict]:
        if record is None:
            return []
        policy = (record.config or {}).get("redrive_policy") or {}
        dlq = policy.get("deadLetterTargetArn") if isinstance(policy, dict) else ""
        if not dlq:
            return []
        reader = self._reader(record)
        sample = getattr(reader, "sample_dlq_messages", None)
        if sample is None:
            return []
        try:
            return [dict(item) for item in sample(record.account_id, record.region, dlq)]
        except AccessDenied:
            return []


class DiagnosisAgent:
    """Calls the six tools. An optional interpreter may add an explanation and cannot change the action."""

    def __init__(self, tools: DiagnosticTools, interpreter=None) -> None:
        self.tools = tools
        self.interpreter = interpreter

    def investigate(self, resource_id: str) -> Diagnosis:
        incident_id = str(uuid4())
        self.tools.inspect(resource_id, incident_id)
        diagnosis = self.tools.propose_runbook(incident_id)
        errors = validate_evidence(diagnosis)
        if errors:
            diagnosis.proposed_action = None
            diagnosis.uncertainty.extend(errors)
            diagnosis.status = "unresolved"
        if self.interpreter is not None:
            action = diagnosis.proposed_action
            note = self.interpreter(diagnosis)
            diagnosis.proposed_action = action
            if note:
                diagnosis.uncertainty.append(str(note))
        return diagnosis
