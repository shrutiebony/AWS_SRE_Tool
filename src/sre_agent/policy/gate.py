"""Policy gate. Recommendation mode never calls the executor."""

from __future__ import annotations

from datetime import timedelta

from sre_agent.clock import Clock, parse_time
from sre_agent.discovery.service import Scope
from sre_agent.evidence.validator import validate_evidence
from sre_agent.executor.runbooks import CATALOG
from sre_agent.models import AgentConfig, Diagnosis, PolicyDecision
from sre_agent.registry.store import Registry


class PolicyGate:
    def __init__(self, config: AgentConfig, registry: Registry, clock: Clock, scope: Scope, catalog: dict | None = None, jira=None) -> None:
        self.config = config
        self.registry = registry
        self.clock = clock
        self.scope = scope
        self.catalog = CATALOG if catalog is None else catalog
        self.jira = jira

    def decide(self, diagnosis: Diagnosis) -> PolicyDecision:
        action = diagnosis.proposed_action
        if action is None:
            return PolicyDecision(diagnosis.incident_id, diagnosis.resource_id, "no-action", ["No repair was proposed."])
        reasons = validate_evidence(diagnosis)
        if reasons:
            return self._deny(diagnosis, action.runbook_id, reasons)
        spec = self.catalog.get(action.runbook_id)
        if spec is None:
            return self._deny(diagnosis, action.runbook_id, ["The runbook is not in the catalog."])
        record = self.registry.get_by_resource_id(action.target_resource_id)
        if record is None:
            return self._deny(diagnosis, action.runbook_id, ["The target is not in the registry."])
        if record.presence != "present":
            return self._deny(diagnosis, action.runbook_id, ["The target is not confirmed present."])
        if record.resource_type not in spec.permitted_types:
            return self._deny(diagnosis, action.runbook_id, ["The runbook does not apply to this resource type."])
        if action.runbook_id not in record.allowed_runbooks:
            return self._deny(diagnosis, action.runbook_id, ["The monitoring pack does not allow this runbook on the target."])
        if not spec.validated:
            return self._deny(diagnosis, action.runbook_id, ["The runbook has not been validated."])
        if not _in_scope(self.scope, record.account_id, record.region):
            return self._deny(diagnosis, action.runbook_id, ["The target is outside the configured scope."])
        blocked = record.runbook_blocks.get(action.runbook_id)
        if blocked:
            return self._deny(diagnosis, action.runbook_id, [f"Repeated action is stopped: {blocked}."])
        if spec.mutates_configuration and record.managed_by == "terraform":
            return self._deny(
                diagnosis,
                action.runbook_id,
                ["Terraform owns this setting. Coordinate the change in configuration so the next apply does not undo it."],
            )
        attempts = _recent_successes(self.registry, record.resource_id, action.runbook_id, self.clock, spec.cooldown_seconds)
        if attempts >= spec.max_attempts:
            return self._deny(diagnosis, action.runbook_id, ["The runbook is inside its cooldown."])
        if self.config.mode != "automatic":
            return PolicyDecision(
                diagnosis.incident_id,
                diagnosis.resource_id,
                "recommend",
                ["The agent is in recommendation mode. A diagnosis does not authorize a repair."],
                action.runbook_id,
            )
        if action.runbook_id not in self.config.enabled_automatic_runbooks:
            return PolicyDecision(
                diagnosis.incident_id,
                diagnosis.resource_id,
                "recommend",
                ["This runbook is validated, and automatic execution is not enabled."],
                action.runbook_id,
            )
        if not action.automatic_eligible:
            return PolicyDecision(
                diagnosis.incident_id,
                diagnosis.resource_id,
                "recommend",
                ["The failure mode is not eligible for automatic repair."],
                action.runbook_id,
            )
        if self.config.approval_mode == "lab" and not self.config.lab_targets:
            return self._deny(diagnosis, action.runbook_id, ["Lab approval mode needs an explicit list of lab targets."])
        if self.config.lab_targets and action.target_resource_id not in self.config.lab_targets:
            return self._deny(diagnosis, action.runbook_id, ["The target is not a designated lab resource."])
        if _lab_auto(self.config, action):
            return PolicyDecision(
                diagnosis.incident_id,
                diagnosis.resource_id,
                "execute",
                ["The designated lab redrive is allowed without a per-incident approval."],
                action.runbook_id,
            )
        if self.config.require_approval or self.config.approval_mode == "required":
            approval_reasons = _approval_reasons(self.jira, diagnosis, action, self.config, self.clock)
            if approval_reasons:
                return PolicyDecision(
                    diagnosis.incident_id,
                    diagnosis.resource_id,
                    "recommend",
                    approval_reasons,
                    action.runbook_id,
                )
        return PolicyDecision(
            diagnosis.incident_id,
            diagnosis.resource_id,
            "execute",
            ["Policy checks passed for the validated runbook."],
            action.runbook_id,
        )

    def _deny(self, diagnosis: Diagnosis, runbook_id: str, reasons: list[str]) -> PolicyDecision:
        return PolicyDecision(diagnosis.incident_id, diagnosis.resource_id, "deny", reasons, runbook_id)


def _lab_auto(config: AgentConfig, action) -> bool:
    return (
        config.approval_mode == "lab"
        and action.runbook_id == "sqs.redrive_dlq"
        and action.target_resource_id in config.lab_targets
    )


def _approval_reasons(jira, diagnosis: Diagnosis, action, config: AgentConfig, clock: Clock) -> list[str]:
    if jira is None or not diagnosis.jira_issue_key:
        return ["Execution is waiting for a Jira comment whose author is an allowed approver."]
    approval = jira.approval_for(diagnosis.jira_issue_key)
    if approval is None:
        return ["The Jira issue has no approval from a comment author."]
    if not config.approvers or approval.approver not in config.approvers:
        return [f"Comment author {approval.approver} is not an allowed approver."]
    if approval.resource_id != action.target_resource_id or approval.action != action.runbook_id:
        return ["The approval does not match this target and action."]
    try:
        expires = parse_time(approval.expires_at)
    except ValueError:
        return ["The approval expiry is not a timestamp."]
    if expires <= clock.now():
        return ["The approval has expired."]
    return []


def _in_scope(scope: Scope, account_id: str, region: str) -> bool:
    return any(account.account_id == account_id and region in account.regions for account in scope.accounts)


def _recent_successes(registry: Registry, resource_id: str, runbook_id: str, clock: Clock, cooldown_seconds: int) -> int:
    cutoff = clock.now() - timedelta(seconds=cooldown_seconds)
    count = 0
    for action in registry.actions():
        if action.resource_id != resource_id or action.runbook_id != runbook_id or action.status != "succeeded":
            continue
        try:
            stamp = parse_time(action.finished_at)
        except ValueError:
            continue
        if stamp >= cutoff:
            count += 1
    return count
