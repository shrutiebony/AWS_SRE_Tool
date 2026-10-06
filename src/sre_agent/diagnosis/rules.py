"""Deterministic diagnosis. An interpreter may explain the result; it cannot permit a repair."""

from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.clock import parse_time
from sre_agent.models import Diagnosis, Evidence, ProposedAction, ResourceRecord

POISON_MARKERS = ("permanent_failure", "schema validation", "validation failed", "malformed", "poison", "invalid message")
TRANSIENT_MARKERS = ("transient_failure", "timed out", "timeout", "throttl", "connection reset", "status code 503", "status code 502", "status code 429")
REDRIVE = "sqs.redrive_dlq"


def classify_order(order_id: str, lines: list[str]) -> str:
    """Classify one order from application logs. Message attributes are not evidence."""
    if not order_id:
        return "malformed"
    seen: list[str] = []
    for line in lines:
        if order_id not in line:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in ("permanent_failure", "schema validation", "malformed", "poison", "validation failed")):
            seen.append("poison")
        elif any(marker in lowered for marker in ("transient_failure", "timed out", "timeout", "connection reset", "throttl")):
            seen.append("transient")
        else:
            seen.append("unknown")
    if not seen:
        return "uncorrelated"
    if "poison" in seen:
        return "poison"
    if "transient" in seen:
        return "transient"
    return "unknown"


def order_outcomes(messages: list[dict], lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Eligible orders are those logs mark transient. Sender-supplied failure labels are ignored."""
    eligible: list[str] = []
    rejected: list[str] = []
    notes: list[str] = []
    if not messages:
        return [], [], ["DLQ messages were not sampled, so the queue cannot be replayed."]
    for item in messages:
        order_id = str(item.get("order_id") or "")
        kind = classify_order(order_id, lines)
        label = order_id or "(missing order_id)"
        if kind == "transient" and order_id not in eligible:
            eligible.append(order_id)
        else:
            rejected.append(label)
            notes.append(f"Order {label} is {kind} and will not be replayed.")
    return eligible, rejected, notes


def _correlate_messages(snapshot, consumers) -> tuple[str, list[str]]:
    messages = list(getattr(snapshot, "dlq_messages", []) or [])
    lines: list[str] = []
    for consumer in consumers:
        read = snapshot.logs.get(consumer.resource_id)
        if read is not None:
            lines.extend(read.lines)
    eligible, rejected, notes = order_outcomes(messages, lines)
    if not messages:
        return "empty", notes
    if eligible and not rejected:
        return "correlated", notes
    if eligible:
        return "partial", notes
    if rejected and all("poison" in note or "malformed" in note for note in notes):
        return "poison", notes
    return "uncorrelated", notes


def classify_logs(lines: list[str], status: str) -> str:
    if status in {"denied", "partial"}:
        return "unavailable"
    text = "\n".join(lines).lower()
    poison = any(marker in text for marker in POISON_MARKERS)
    transient = any(marker in text for marker in TRANSIENT_MARKERS)
    if poison and transient:
        return "mixed"
    if poison:
        return "poison"
    if transient:
        return "transient"
    return "unknown"


def diagnose(snapshot, now: datetime) -> Diagnosis:
    record = snapshot.record
    if record is None:
        return _diagnosis(
            snapshot,
            problem="The resource is not in the registry.",
            code="registry.missing",
            health="unknown",
            status="unresolved",
        )
    if record.presence != "present":
        return _diagnosis(
            snapshot,
            problem="The resource is not confirmed present. A scan that cannot see it is not proof of deletion.",
            code="presence.uncertain",
            health="unknown",
            status="unresolved",
            uncertainty=["Presence stays uncertain until a direct lookup confirms deletion."],
        )
    assessment = snapshot.assessment
    if assessment.state == "healthy":
        return _diagnosis(
            snapshot,
            problem="No fault is currently observed.",
            code="healthy",
            health="healthy",
            status="resolved",
            evidence=_signal_evidence(assessment),
        )
    if assessment.state == "unknown" or record.monitoring_status == "unsupported":
        return _diagnosis(
            snapshot,
            problem="Required telemetry is missing, so health is unknown.",
            code="telemetry.missing",
            health="unknown",
            status="unresolved",
            uncertainty=list(assessment.missing_signals),
            evidence=_signal_evidence(assessment),
        )
    if record.resource_type == "sqs:queue":
        return _diagnose_sqs(snapshot, now)
    if record.resource_type == "lambda:function":
        return _diagnose_lambda(snapshot, now)
    if record.resource_type == "ec2:instance":
        return _diagnose_ec2(snapshot)
    return _diagnosis(
        snapshot,
        problem="Monitoring is not supported for this resource type.",
        code="monitoring.unsupported",
        health=assessment.state,
        status="unresolved",
    )


def _diagnose_sqs(snapshot, now: datetime) -> Diagnosis:
    record = snapshot.record
    assessment = snapshot.assessment
    evidence = _signal_evidence(assessment)
    consumers = _consumers(snapshot)
    failure_class, log_evidence = _failure_evidence(snapshot, consumers)
    evidence.extend(log_evidence)
    dlq_id = _dlq_id(record)
    dlq_visible = assessment.observed_signals.get("dlq.ApproximateNumberOfMessagesVisible")
    incoming = [item for item in snapshot.dependencies if item.kind == "dead_letters_to" and item.target_id == record.resource_id]
    if incoming and not dlq_id:
        evidence.append(_relationship_evidence(incoming[0]))
        return _diagnosis(
            snapshot,
            problem="This dead-letter queue has messages. Redrive is evaluated from the source queue, where the consumer relationship is known.",
            code="sqs.dlq_waiting",
            health=assessment.state,
            status="unresolved",
            evidence=evidence,
            causes=["Messages were redirected here by a redrive policy."],
        )
    if any(_health(snapshot, item) == "unhealthy" for item in consumers):
        evidence.append(_consumer_evidence(snapshot, consumers))
        return _diagnosis(
            snapshot,
            problem="Queue backlog because its consumer is failing.",
            code="sqs.backlog_consumer_failing",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            causes=["A consumer is unhealthy. Redriving messages would send work back to that consumer."],
        )
    if not consumers:
        return _diagnosis(
            snapshot,
            problem="Queue backlog and no consumer relationship is recorded.",
            code="sqs.no_consumer",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            uncertainty=["The agent cannot tell a slow consumer from a stuck queue without a relationship."],
        )
    evidence.append(_consumer_evidence(snapshot, consumers))
    if not dlq_id:
        return _diagnosis(
            snapshot,
            problem="Queue backlog with no dead-letter queue configured.",
            code="sqs.no_dlq",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            causes=["There is no dead-letter queue to redrive."],
        )
    evidence.append(
        Evidence(
            id="ev-dlq",
            source="cloudwatch",
            summary=f"Dead-letter queue {dlq_id} has {dlq_visible} visible messages.",
            data={"dlq_resource_id": dlq_id, "visible": dlq_visible, "growth": assessment.context.get("dlq_growth", "unknown")},
        )
    )
    if dlq_visible is None or dlq_visible <= 0:
        return _diagnosis(
            snapshot,
            problem="Queue backlog with nothing waiting on the dead-letter queue.",
            code="sqs.backlog",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
        )
    consumer_states = [_health(snapshot, item) for item in consumers]
    correlation, correlation_notes = _correlate_messages(snapshot, consumers)
    if correlation == "poison":
        return _diagnosis(
            snapshot,
            problem="A sampled dead-letter message is not transient.",
            code="sqs.dlq_poison",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            causes=["Application logs classify a sampled order as a permanent or malformed failure. Sender labels are not used."],
        )
    logs_complete = not any(getattr(read, "status", "") == "partial" for read in snapshot.logs.values())
    confirmed = (
        correlation in {"correlated", "partial"}
        and logs_complete
        and all(state == "healthy" for state in consumer_states)
    )
    uncertainty = _lesson_notes(snapshot, "sqs.dlq_transient")
    uncertainty.extend(correlation_notes)
    if not logs_complete:
        uncertainty.append("Log retrieval stopped before the last page, so coverage is incomplete.")
    if assessment.context.get("dlq_growth", "unknown") == "unknown":
        uncertainty.append("Dead-letter growth over the window is unknown.")
    if failure_class in {"unknown", "unavailable"}:
        uncertainty.append("Logs do not confirm that the failures were transient.")
    if any(state != "healthy" for state in consumer_states):
        uncertainty.append("A consumer has no fresh successful invocations, so it is not ready for replay.")
    action = ProposedAction(
        runbook_id=REDRIVE,
        target_resource_id=record.resource_id,
        rationale="Move messages from the dead-letter queue back to the source queue."
        if confirmed
        else "Redrive is a candidate, but the failure mode is not confirmed as transient.",
        evidence_ids=["ev-signals", "ev-logs", "ev-consumer", "ev-dlq"],
        automatic_eligible=confirmed,
    )
    return _diagnosis(
        snapshot,
        problem="Dead-letter queue is holding messages after transient consumer errors."
        if confirmed
        else "Dead-letter queue has messages, but automatic redrive is not confirmed.",
        code="sqs.dlq_transient" if confirmed else "sqs.dlq_unclassified",
        health=assessment.state,
        status="open",
        evidence=evidence,
        causes=["Consumer logs show timeouts or connection failures, and the consumer is currently healthy."]
        if confirmed
        else ["Messages are waiting on the dead-letter queue."],
        uncertainty=uncertainty,
        action=action,
    )


def _diagnose_lambda(snapshot, now: datetime) -> Diagnosis:
    record = snapshot.record
    assessment = snapshot.assessment
    evidence = _signal_evidence(assessment)
    deploy = _recent_deploy(record, snapshot.changes, now)
    uncertainty = []
    if record.managed_by == "terraform":
        uncertainty.append(
            "Terraform owns this function. A persistent change has to be made in configuration so the next apply does not undo it."
        )
    if deploy:
        evidence.append(Evidence(id="ev-change", source="cloudtrail", summary=deploy, data={"last_modified": str(record.config.get("last_modified", ""))}))
        return _diagnosis(
            snapshot,
            problem="Lambda errors rose around a recent deployment.",
            code="lambda.errors_after_deploy",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            causes=["A recent change is the leading explanation. This version does not roll back code."],
            uncertainty=uncertainty,
        )
    throttles = assessment.observed_signals.get("Throttles", 0)
    errors = assessment.observed_signals.get("Errors", 0)
    if throttles > 0 and errors == 0:
        code = "lambda.throttled"
        problem = "Lambda is throttling."
    else:
        code = "lambda.errors"
        problem = "Lambda is returning errors."
    return _diagnosis(
        snapshot,
        problem=problem,
        code=code,
        health=assessment.state,
        status="escalated",
        evidence=evidence,
        uncertainty=uncertainty,
        causes=["No validated repair runbook applies to this Lambda fault."],
    )


def _diagnose_ec2(snapshot) -> Diagnosis:
    assessment = snapshot.assessment
    evidence = _signal_evidence(assessment)
    if assessment.observed_signals.get("StatusCheckFailed", 0) > 0:
        return _diagnosis(
            snapshot,
            problem="Instance status checks are failing.",
            code="ec2.status_check_failed",
            health=assessment.state,
            status="escalated",
            evidence=evidence,
            uncertainty=["Reboot is not a validated runbook in this version."],
            causes=["The instance or its system status check is impaired."],
        )
    return _diagnosis(
        snapshot,
        problem="Instance CPU is elevated.",
        code="ec2.cpu",
        health=assessment.state,
        status="unresolved",
        evidence=evidence,
        uncertainty=["No validated repair runbook applies to CPU pressure."],
    )


def _diagnosis(snapshot, problem, code, health, status, evidence=None, causes=None, uncertainty=None, action=None) -> Diagnosis:
    uncertainty = list(uncertainty or [])
    uncertainty.extend(_lesson_notes(snapshot, code))
    return Diagnosis(
        incident_id=snapshot.incident_id,
        resource_id=snapshot.record.resource_id if snapshot.record else snapshot.incident_id,
        observed_problem=problem,
        problem_code=code,
        health=health,
        evidence=evidence or [],
        likely_causes=causes or [],
        uncertainty=_unique(uncertainty),
        proposed_action=action,
        status=status,
    )


def _signal_evidence(assessment) -> list[Evidence]:
    return [
        Evidence(
            id="ev-signals",
            source="cloudwatch",
            summary=f"Health is {assessment.state}. " + " ".join(assessment.reasons),
            data={
                "state": assessment.state,
                "signals": assessment.observed_signals,
                "missing": assessment.missing_signals,
                "reasons": assessment.reasons,
            },
        )
    ]


def _consumers(snapshot) -> list[ResourceRecord]:
    found = []
    for item in snapshot.dependencies:
        if item.kind == "consumes" and item.target_id == snapshot.record.resource_id:
            neighbor = snapshot.neighbors.get(item.source_id)
            if neighbor is not None:
                found.append(neighbor)
    return found


def _health(snapshot, record: ResourceRecord) -> str:
    assessment = snapshot.assessments.get(record.resource_id)
    if assessment is not None:
        return assessment.state
    return record.health


def _failure_evidence(snapshot, consumers):
    lines: list[str] = []
    statuses: list[str] = []
    for resource_id in [snapshot.record.resource_id, *[item.resource_id for item in consumers]]:
        read = snapshot.logs.get(resource_id)
        if read is None:
            continue
        lines.extend(read.lines)
        statuses.append(read.status)
    if any(status == "denied" for status in statuses) and not lines:
        failure = "unavailable"
    else:
        failure = classify_logs(lines, "ok" if lines or statuses else "ok")
        if not lines and not any(status == "denied" for status in statuses):
            failure = "unknown"
        if any(status == "denied" for status in statuses) and not lines:
            failure = "unavailable"
    evidence = [
        Evidence(
            id="ev-logs",
            source="logs",
            summary=f"Failure class from logs is {failure}.",
            data={"failure_class": failure, "lines": lines[:20]},
        )
    ]
    return failure, evidence


def _consumer_evidence(snapshot, consumers) -> Evidence:
    described = [
        {"resource_id": item.resource_id, "name": item.name, "health": _health(snapshot, item)}
        for item in consumers
    ]
    summary = ", ".join(f"{item['name']} is {item['health']}" for item in described) or "No consumer was recorded."
    return Evidence(id="ev-consumer", source="relationship", summary=summary, data={"consumers": described})


def _relationship_evidence(item) -> Evidence:
    return Evidence(
        id="ev-relationship",
        source=item.evidence_source,
        summary=f"{item.kind} from {item.source_id} to {item.target_id}.",
        data=dict(item.evidence),
    )


def _dlq_id(record: ResourceRecord) -> str:
    policy = record.config.get("redrive_policy") or {}
    if isinstance(policy, dict):
        return str(policy.get("deadLetterTargetArn") or "")
    return ""


def _recent_deploy(record, changes, now: datetime) -> str | None:
    cutoff = now - timedelta(minutes=30)
    for change in changes:
        try:
            stamp = parse_time(change.get("at", ""))
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        name = change.get("event_name", "").lower()
        summary = change.get("summary", "")
        if "function" in name or "deploy" in summary.lower():
            return summary or change.get("event_name", "recent change")
    modified = record.config.get("last_modified")
    if not modified:
        return None
    try:
        stamp = parse_time(str(modified))
    except ValueError:
        return None
    if stamp >= cutoff:
        return f"Configuration last modified at {modified}."
    return None


def _lesson_notes(snapshot, code: str) -> list[str]:
    failed = [item for item in snapshot.lessons if item.symptom_key == code and item.outcome == "failed"]
    if not failed:
        return []
    return [
        "A previous lesson recorded a failed repair for this symptom. Lessons inform the explanation and do not permit an action."
    ]


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen
