"""Monitoring packs. A discovered resource stays visible even when no pack applies."""

from __future__ import annotations

from sre_agent.models import HealthAssessment, ResourceRecord

LAMBDA_REQUIRED = ["Errors", "Invocations", "Throttles", "Duration"]
SQS_REQUIRED = ["ApproximateAgeOfOldestMessage", "ApproximateNumberOfMessagesVisible"]
EC2_REQUIRED = ["StatusCheckFailed", "CPUUtilization"]
EC2_OPTIONAL = ["mem_used_percent", "disk_used_percent"]
DLQ_SIGNAL = "dlq.ApproximateNumberOfMessagesVisible"
DYNAMODB_REQUIRED = ["ReadThrottleEvents", "WriteThrottleEvents", "UserErrors"]
API_REQUIRED = ["5XXError", "Latency", "Count"]
REDRIVE = "sqs.redrive_dlq"
SUPPORTED = {"lambda:function", "sqs:queue", "ec2:instance", "dynamodb:table", "apigateway:restapi"}


def required_signals(record: ResourceRecord) -> list[str]:
    if record.resource_type == "lambda:function":
        return list(LAMBDA_REQUIRED)
    if record.resource_type == "sqs:queue":
        names = list(SQS_REQUIRED)
        if _dlq_arn(record):
            names.append(DLQ_SIGNAL)
        return names
    if record.resource_type == "ec2:instance":
        names = list(EC2_REQUIRED)
        if record.tags.get("sre:health-probe"):
            names.append("ApplicationHealth")
        return names
    if record.resource_type == "dynamodb:table":
        return list(DYNAMODB_REQUIRED)
    if record.resource_type == "apigateway:restapi":
        return list(API_REQUIRED)
    return []


def optional_signals(record: ResourceRecord) -> list[str]:
    if record.resource_type == "ec2:instance":
        return list(EC2_OPTIONAL)
    return []


def runbooks_for(record: ResourceRecord) -> list[str]:
    if record.resource_type == "sqs:queue" and _dlq_arn(record):
        return [REDRIVE]
    return []


def assess(record: ResourceRecord, signals: dict[str, float | None], context: dict[str, str]) -> HealthAssessment:
    missing = [name for name in required_signals(record) if signals.get(name) is None]
    observed = {name: float(value) for name, value in signals.items() if value is not None and name in required_signals(record)}
    if record.resource_type == "lambda:function":
        return _assess_lambda(record, observed, missing, context)
    if record.resource_type == "sqs:queue":
        return _assess_sqs(record, observed, missing, context)
    if record.resource_type == "ec2:instance":
        return _assess_ec2(record, signals, observed, missing, context)
    if record.resource_type == "dynamodb:table":
        return _assess_threshold(record, observed, missing, context, DYNAMODB_REQUIRED, "Table throttles and user errors are within bounds.")
    if record.resource_type == "apigateway:restapi":
        return _assess_api(record, observed, missing, context)
    return HealthAssessment(
        resource_id=record.resource_id,
        state="unknown",
        reasons=["No monitoring pack is registered for this resource type."],
        missing_signals=missing,
    )


def _assess_lambda(record, observed, missing, context) -> HealthAssessment:
    core_missing = [name for name in ("Errors", "Invocations") if name in missing]
    if core_missing:
        return _unknown(record, core_missing, context)
    errors = float(context["latest_errors"]) if "latest_errors" in context else float(observed["Errors"])
    invocations = float(context["latest_invocations"]) if "latest_invocations" in context else float(observed["Invocations"])
    throttles = float(context["latest_throttles"]) if "latest_throttles" in context else float(observed.get("Throttles", 0))
    duration = float(observed["Duration"]) if "Duration" in observed else None
    reasons: list[str] = []
    state = "healthy"
    rate = 1.0 if invocations == 0 and errors > 0 else (errors / invocations if invocations else 0.0)
    if invocations == 0 and errors == 0:
        state = "degraded"
        reasons.append("The latest period has no invocations, so the consumer is not confirmed to be processing.")
    elif rate >= 0.05:
        state = "unhealthy"
        reasons.append(f"Error rate is {rate:.0%}.")
    elif rate >= 0.01:
        state = "degraded"
        reasons.append(f"Error rate is {rate:.0%}.")
    if throttles >= 10:
        state = _worse(state, "unhealthy")
        reasons.append(f"Throttles are {throttles:.0f}.")
    elif throttles > 0:
        state = _worse(state, "degraded")
        reasons.append(f"Throttles are {throttles:.0f}.")
    timeout = float(record.config.get("timeout") or 0)
    if duration is not None and timeout > 0:
        limit = timeout * 1000
        if duration >= limit:
            state = _worse(state, "unhealthy")
            reasons.append("Duration reached the configured timeout.")
        elif duration >= 0.9 * limit:
            state = _worse(state, "degraded")
            reasons.append("Duration is close to the configured timeout.")
    historical = observed.get("Errors")
    if "latest_errors" in context and historical is not None and float(historical) > errors and rate < 0.01 and invocations > 0:
        reasons.append(f"Older errors ({float(historical):.0f}) remain in the lookback. The latest invocations succeeded.")
    ready = "yes" if state == "healthy" else "no"
    age = context.get("latest_metric_age_seconds")
    if age is not None and float(age) > 300:
        state = _worse(state, "degraded")
        reasons.append("The newest Lambda sample is older than one period, so processing is not confirmed.")
        ready = "stale"
    sparse = [name for name in ("Throttles", "Duration") if name in missing]
    if sparse:
        if state == "healthy":
            state = "degraded"
        reasons.append(f"Sparse metrics omit {', '.join(sparse)}. The consumer is not marked healthy.")
        if ready == "yes":
            ready = "sparse"
    if not reasons:
        reasons.append("The latest invocations succeeded without errors, throttles, or timeout.")
    context = {**context, "error_rate": f"{rate:.4f}", "timeout_seconds": str(timeout), "consumer_ready": ready}
    return HealthAssessment(record.resource_id, state, reasons, sparse, observed, context)


def _assess_sqs(record, observed, missing, context) -> HealthAssessment:
    if missing:
        return _unknown(record, missing, context)
    age = observed["ApproximateAgeOfOldestMessage"]
    visible = observed["ApproximateNumberOfMessagesVisible"]
    reasons: list[str] = []
    state = "healthy"
    if age >= 300 and visible > 0:
        state = "unhealthy"
        reasons.append(f"Oldest message is {age:.0f}s with {visible:.0f} visible messages.")
    elif age >= 60 and visible > 0:
        state = "degraded"
        reasons.append(f"Oldest message is {age:.0f}s with {visible:.0f} visible messages.")
    elif age >= 300:
        state = "degraded"
        reasons.append(f"Oldest message age is {age:.0f}s.")
    dlq_visible = observed.get(DLQ_SIGNAL)
    growth = context.get("dlq_growth", "unknown")
    if dlq_visible is not None and dlq_visible > 0:
        if growth not in {"unknown", ""} and float(growth) > 0:
            state = _worse(state, "unhealthy")
            reasons.append(f"Dead-letter queue grew by {growth} and holds {dlq_visible:.0f} messages.")
        else:
            state = _worse(state, "degraded")
            reasons.append(f"Dead-letter queue holds {dlq_visible:.0f} messages.")
    if not reasons:
        reasons.append("Queue age and depth are within bounds.")
    return HealthAssessment(record.resource_id, state, reasons, [], observed, context)


def _assess_ec2(record, signals, observed, missing, context) -> HealthAssessment:
    if missing:
        return _unknown(record, missing, context)
    reasons: list[str] = []
    state = "healthy"
    status = observed["StatusCheckFailed"]
    cpu = observed["CPUUtilization"]
    if status > 0:
        state = "unhealthy"
        reasons.append("Status checks are failing.")
    if cpu >= 98:
        state = _worse(state, "unhealthy")
        reasons.append(f"CPU is {cpu:.0f}%.")
    elif cpu >= 90:
        state = _worse(state, "degraded")
        reasons.append(f"CPU is {cpu:.0f}%.")
    notes = dict(context)
    for name in EC2_OPTIONAL:
        if signals.get(name) is None:
            notes[name] = "not observed; an agent is required"
        else:
            observed[name] = float(signals[name])
    if any(value == "not observed; an agent is required" for value in notes.values()):
        reasons.append("Memory and disk were not observed. An agent is required for those signals.")
    probe = signals.get("ApplicationHealth")
    if record.tags.get("sre:health-probe") and probe == 0:
        state = _worse(state, "unhealthy")
        reasons.append("Application health probe failed.")
    if not any(reason.startswith("Status") or reason.startswith("CPU") or reason.startswith("Application") for reason in reasons):
        reasons.insert(0, "Status checks are passing.")
    return HealthAssessment(record.resource_id, state, reasons, [], observed, notes)


def _assess_threshold(record, observed, missing, context, names, healthy_reason) -> HealthAssessment:
    if missing:
        return _unknown(record, missing, context)
    reasons = []
    state = "healthy"
    for name in names:
        value = observed[name]
        if value >= 10:
            state = "unhealthy"
            reasons.append(f"{name} is {value:.0f}.")
        elif value > 0:
            state = _worse(state, "degraded")
            reasons.append(f"{name} is {value:.0f}.")
    if not reasons:
        reasons.append(healthy_reason)
    return HealthAssessment(record.resource_id, state, reasons, [], observed, context)


def _assess_api(record, observed, missing, context) -> HealthAssessment:
    if missing:
        return _unknown(record, missing, context)
    reasons = []
    state = "healthy"
    errors = observed["5XXError"]
    latency = observed["Latency"]
    if errors > 0:
        state = "unhealthy"
        reasons.append(f"API 5XX count is {errors:.0f}.")
    if latency >= 2000:
        state = _worse(state, "degraded")
        reasons.append(f"API latency is {latency:.0f} ms.")
    if not reasons:
        reasons.append("API errors and latency are within bounds.")
    return HealthAssessment(record.resource_id, state, reasons, [], observed, context)


def _unknown(record, missing, context) -> HealthAssessment:
    return HealthAssessment(
        resource_id=record.resource_id,
        state="unknown",
        reasons=["Required telemetry is missing. Missing telemetry is unknown, not healthy."],
        missing_signals=missing,
        context=context,
    )


def _worse(current: str, new: str) -> str:
    rank = {"healthy": 0, "degraded": 1, "unhealthy": 2}
    return new if rank[new] > rank[current] else current


def _dlq_arn(record: ResourceRecord) -> str:
    policy = record.config.get("redrive_policy") or {}
    if isinstance(policy, dict):
        return str(policy.get("deadLetterTargetArn") or "")
    return ""
