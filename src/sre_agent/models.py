"""Shared records for the registry, diagnoses, and repair history."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def identity_for(account_id: str, region: str, resource_type: str, resource_id: str) -> str:
    return f"{account_id}|{region}|{resource_type}|{resource_id}"


@dataclass
class ResourceRecord:
    resource_id: str
    account_id: str
    region: str
    resource_type: str
    name: str
    tags: dict[str, str] = field(default_factory=dict)
    last_seen_at: str = ""
    discovery_source: str = ""
    monitoring_status: str = "not-configured"
    allowed_runbooks: list[str] = field(default_factory=list)
    presence: str = "present"
    health: str = "unknown"
    sources: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    intended_config: dict[str, Any] = field(default_factory=dict)
    ownership: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    managed_by: str = ""
    miss_count: int = 0
    first_seen_at: str = ""
    health_reasons: list[str] = field(default_factory=list)
    missing_signals: list[str] = field(default_factory=list)
    observed_signals: dict[str, float] = field(default_factory=dict)
    signal_context: dict[str, str] = field(default_factory=dict)
    runbook_blocks: dict[str, str] = field(default_factory=dict)
    recent_changes: list[dict[str, str]] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return identity_for(self.account_id, self.region, self.resource_type, self.resource_id)

    def public_view(self) -> dict[str, Any]:
        """The registry fields the first version promises to keep for every resource."""
        return {
            "resource_id": self.resource_id,
            "account_id": self.account_id,
            "region": self.region,
            "resource_type": self.resource_type,
            "name": self.name,
            "tags": dict(self.tags),
            "last_seen_at": self.last_seen_at,
            "discovery_source": self.discovery_source,
            "monitoring_status": self.monitoring_status,
            "allowed_runbooks": list(self.allowed_runbooks),
        }


@dataclass
class Relationship:
    source_id: str
    target_id: str
    kind: str
    evidence_source: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f"{self.evidence_source}|{self.kind}|{self.source_id}|{self.target_id}"


@dataclass
class ScanAccess:
    account_id: str
    region: str
    collector: str
    status: str
    scanned_at: str
    resource_count: int = 0
    message: str = ""


@dataclass
class Evidence:
    id: str
    source: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProposedAction:
    runbook_id: str
    target_resource_id: str
    rationale: str
    evidence_ids: list[str]
    automatic_eligible: bool


@dataclass
class Diagnosis:
    incident_id: str
    resource_id: str
    observed_problem: str
    problem_code: str
    health: str
    evidence: list[Evidence] = field(default_factory=list)
    likely_causes: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    proposed_action: ProposedAction | None = None
    tool_trace: list[list[str]] = field(default_factory=list)
    status: str = "open"
    dedup_key: str = ""
    jira_issue_key: str = ""


@dataclass
class PolicyDecision:
    incident_id: str
    resource_id: str
    outcome: str
    reasons: list[str] = field(default_factory=list)
    runbook_id: str | None = None


@dataclass
class ActionRecord:
    action_id: str
    incident_id: str
    runbook_id: str
    resource_id: str
    status: str
    started_at: str
    finished_at: str
    api_result: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    idempotency_key: str = ""
    moved_message_ids: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    action_id: str
    incident_id: str
    resource_id: str
    recovered: bool
    status: str
    reasons: list[str] = field(default_factory=list)
    rollback: str = "not-needed"
    cancellation: str = "not-needed"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Escalation:
    incident_id: str
    resource_id: str
    reason: str
    created_at: str
    runbook_id: str = ""


@dataclass
class Outcome:
    incident_id: str
    resource_id: str
    status: str
    summary: str
    created_at: str


@dataclass
class Lesson:
    symptom_key: str
    runbook_id: str
    resource_id: str
    outcome: str
    summary: str
    created_at: str


@dataclass
class HealthAssessment:
    resource_id: str
    state: str
    reasons: list[str] = field(default_factory=list)
    missing_signals: list[str] = field(default_factory=list)
    observed_signals: dict[str, float] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentConfig:
    mode: str = "recommend"
    enabled_automatic_runbooks: frozenset[str] = field(default_factory=frozenset)
    verify_window_seconds: int = 60
    reconcile_interval_seconds: int = 3600
    metric_lookback_seconds: int = 900
    lab_targets: frozenset[str] = field(default_factory=frozenset)
    require_approval: bool = False
    approvers: frozenset[str] = field(default_factory=frozenset)
    approval_mode: str = "off"
    verify_timeout_seconds: int = 900
    jira_issue_type: str = "Task"
    jira_transition: str = "Done"

    def __post_init__(self) -> None:
        self.enabled_automatic_runbooks = frozenset(self.enabled_automatic_runbooks)
        self.lab_targets = frozenset(self.lab_targets)
        self.approvers = frozenset(self.approvers)


@dataclass
class RunbookSpec:
    runbook_id: str
    description: str
    validated: bool
    mutates_configuration: bool
    permitted_types: tuple[str, ...]
    max_attempts: int = 1
    cooldown_seconds: int = 3600
    rollback_supported: bool = False
